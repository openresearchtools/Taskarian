#!/usr/bin/env python3
# Adapted from SimpleHF (MIT), copyright (c) 2026 openresearchtools.
# See LICENSE.simplehf. Taskarian additions: SPDX-License-Identifier: AGPL-3.0-only
"""Convert a reviewed Linux .deb payload into a native Arch package.

This never executes Debian maintainer scripts or extracts upstream paths onto
the build host. Dependency names and any skipped scripts require explicit rules.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import gzip
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile


ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}
SCRIPT_NAMES = {"preinst", "postinst", "prerm", "postrm", "config", "triggers"}
RESERVED = {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL"}


class RepackageError(ValueError):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def line_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RepackageError(f"Invalid {field}")
    return value


def safe_path(value: str) -> str:
    if value.startswith("/") or ".." in PurePosixPath(value).parts or "\x00" in value:
        raise RepackageError(f"Unsafe archive path: {value!r}")
    normalized = posixpath.normpath(value)
    return "" if normalized == "." else normalized


def ar_members(path: Path) -> dict[str, tuple[int, int]]:
    members = {}
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise RepackageError("Input is not a Debian ar archive")
        while header := stream.read(60):
            if len(header) != 60 or header[58:] != b"`\n":
                raise RepackageError("Invalid Debian archive header")
            name = header[:16].decode("ascii").strip().removesuffix("/")
            if not name or name.startswith(("/", "#")) or name in members:
                raise RepackageError("Unsupported or duplicate Debian archive member")
            try:
                size = int(header[48:58].strip())
            except ValueError as error:
                raise RepackageError("Invalid Debian archive member size") from error
            offset = stream.tell()
            if size < 0 or offset + size > path.stat().st_size:
                raise RepackageError("Truncated Debian archive member")
            members[name] = offset, size
            stream.seek(size + size % 2, 1)
    if "debian-binary" not in members:
        raise RepackageError("Missing Debian format marker")
    return members


class SliceReader(io.RawIOBase):
    def __init__(self, source, size):
        self.source, self.remaining = source, size

    def readable(self):
        return True

    def read(self, size=-1):
        size = self.remaining if size < 0 else min(size, self.remaining)
        result = self.source.read(size)
        self.remaining -= len(result)
        return result

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


@contextlib.contextmanager
def deb_tar(path: Path, members: dict, prefix: str):
    matching = [name for name in members if name == prefix + ".tar" or name.startswith(prefix + ".tar.")]
    if len(matching) != 1:
        raise RepackageError(f"Expected one {prefix} tar member")
    name = matching[0]
    offset, size = members[name]
    with path.open("rb") as raw:
        raw.seek(offset)
        limited = SliceReader(raw, size)
        if name.endswith(".zst"):
            try:
                from compression import zstd
                decompressor = zstd.ZstdFile(limited, "rb")
            except ImportError:
                try:
                    import zstandard
                except ImportError as error:
                    raise RepackageError("Install python3-zstandard for zstd-compressed .deb files") from error
                decompressor = zstandard.ZstdDecompressor().stream_reader(limited, closefd=False)
            with decompressor as decompressed:
                with tarfile.open(fileobj=decompressed, mode="r|") as archive:
                    yield archive
        else:
            with tarfile.open(fileobj=limited, mode="r|*") as archive:
                yield archive


def read_control(path: Path, members: dict) -> tuple[dict[str, str], dict[str, bytes]]:
    files = {}
    with deb_tar(path, members, "control") as archive:
        for member in archive:
            name = safe_path(member.name)
            if not name or member.isdir():
                continue
            if not member.isfile() or "/" in name or member.size > 4 * 1024 * 1024:
                raise RepackageError("Unsupported Debian control archive entry")
            if name in files:
                raise RepackageError("Duplicate Debian control file")
            files[name] = archive.extractfile(member).read()
    if "control" not in files:
        raise RepackageError("Missing Debian control metadata")
    control, current = {}, None
    for line in files["control"].decode("utf-8").splitlines():
        if line.startswith((" ", "\t")) and current:
            control[current] += "\n" + line.strip()
            continue
        key, separator, value = line.partition(":")
        if not separator or key in control:
            raise RepackageError("Malformed or repeated Debian control field")
        current = key
        control[key] = value.strip()
    for key in ("Package", "Version", "Architecture"):
        line_value(control.get(key), "Debian " + key)
    return control, files


def architecture_rules(rules: dict, architecture: str) -> dict:
    combined = {key: value for key, value in rules.items() if key != "architectures"}
    override = rules.get("architectures", {}).get(architecture, {})
    combined.update(override)
    if "depends" not in combined or not isinstance(combined["depends"], list):
        raise RepackageError("Rules must explicitly declare the complete Arch depends list")
    return combined


def review_scripts(files: dict[str, bytes], rules: dict, payload_metadata: dict | None = None) -> dict[str, str]:
    reviewed = rules.get("reviewed_script_omissions", {})
    found = {}
    for name in sorted(SCRIPT_NAMES & files.keys()):
        digest = hashlib.sha256(files[name]).hexdigest()
        if rules.get("maintainer_adapter") == "taskarian-linux-v1":
            if name not in {"postinst", "postrm"} or payload_metadata is None:
                raise RepackageError(f"Unreviewed Debian maintainer script: {name}")
            canonical = files[name].decode("utf-8")
            if name == "postinst":
                runtime, managed = runtime_values(payload_metadata)
                canonical = canonical.replace("runtime=" + shlex.quote(runtime), "runtime=@RUNTIME@")
                # The Debian producer always single-quotes both values, even
                # when shlex.quote would consider the runtime path unambiguous.
                canonical = canonical.replace("runtime='" + runtime + "'", "runtime=@RUNTIME@")
                canonical = canonical.replace("'" + managed + "'", "@MANAGED@")
            reviewed_path = Path(__file__).with_name("reviewed-scripts") / name
            if canonical != reviewed_path.read_text():
                raise RepackageError(f"Debian {name} differs from its reviewed native adapter (sha256 {digest})")
            found[name] = digest
            continue
        entry = reviewed.get(name, {})
        if not isinstance(entry, dict) or entry.get("sha256") != digest or not entry.get("reason"):
            raise RepackageError(f"Debian {name} requires explicit review (sha256 {digest}); it will never be executed")
        found[name] = digest
    return found


def runtime_values(metadata: dict) -> tuple[str, str]:
    stamp = metadata["usr/lib/taskarian/build-platform.json"]
    default = metadata["usr/lib/taskarian/runtime-default.json"]
    version, lock = stamp.get("piVersion", ""), stamp.get("lockSha256", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+_-]*)", version) or not re.fullmatch(r"[0-9a-f]{64}", lock):
        raise RepackageError("Invalid packaged Pi runtime identity")
    runtime = "/var/lib/Taskarian/runtimes/" + version + "-" + lock[:12]
    if default != {"root": runtime, "version": version}:
        raise RepackageError("Packaged runtime default differs from its build-platform identity")
    return runtime, json.dumps({"owner": "taskarian", "version": version, "lockSha256": lock})


def native_install(metadata: dict) -> bytes:
    runtime, managed = runtime_values(metadata)
    template = Path(__file__).with_name("taskarian.install").read_text()
    return template.replace("@RUNTIME@", shlex.quote(runtime)).replace("@MANAGED@", shlex.quote(managed)).encode()


def reviewed_dependencies(control: dict, rules: dict) -> list[str]:
    if "debian_dependency_map" not in rules:
        return rules["depends"]
    result = list(rules["depends"])
    for dependency in control.get("Depends", "").split(","):
        dependency = dependency.strip()
        if dependency not in rules["debian_dependency_map"]:
            raise RepackageError(f"Debian dependency needs an explicit Arch mapping: {dependency}")
        for native in rules["debian_dependency_map"][dependency]:
            if native not in result:
                result.append(native)
    return result


def mapped_path(name: str, rules: dict) -> str:
    name = safe_path(name)
    mappings = rules.get("path_mappings", {})
    for source in sorted(mappings, key=len, reverse=True):
        target = mappings[source]
        source, target = safe_path(source).rstrip("/"), safe_path(target).rstrip("/")
        if not source or not target:
            raise RepackageError("Path mappings must have nonempty safe source and destination")
        if name == source or name.startswith(source + "/"):
            return target + name[len(source):]
    if (name in {"bin", "sbin", "lib", "lib32", "lib64", "usr/sbin"}
            or name.startswith(("bin/", "sbin/", "lib/", "lib32/", "lib64/", "usr/sbin/"))
            or re.match(r"usr/lib/[^/]+-linux-gnu(?:/|$)", name)):
        raise RepackageError(f"Debian-specific path {name!r} needs an explicit reviewed path_mappings entry")
    return name


def normalized_member(member: tarfile.TarInfo, rules: dict, epoch: int) -> tarfile.TarInfo:
    name = mapped_path(member.name, rules)
    if name in RESERVED:
        raise RepackageError(f"Payload attempts to overwrite reserved package metadata {name}")
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise RepackageError(f"Unsupported special payload entry {name}")
    if member.mode & 0o6000:
        raise RepackageError(f"Setuid/setgid payload needs a native reviewed recipe: {name}")
    result = copy.copy(member)
    result.name, result.uid, result.gid = name, 0, 0
    result.uname = result.gname = ""
    result.mtime, result.pax_headers = epoch, {}
    if result.issym():
        original = posixpath.normpath(posixpath.join(posixpath.dirname(safe_path(member.name)), member.linkname))
        if original.startswith("/"):
            target = mapped_path(original.lstrip("/"), rules)
            result.linkname = "/" + target
        else:
            target = mapped_path(original, rules)
            result.linkname = posixpath.relpath(target, posixpath.dirname(name) or ".")
    elif result.islnk():
        result.linkname = mapped_path(member.linkname, rules)
    return result


def build_package(deb: Path, raw_rules: dict, output: Path, pkgrel: str = "1", compression: str = "zst",
                  omarchy_skill: Path | None = None) -> tuple[Path, dict]:
    members = ar_members(deb)
    control, control_files = read_control(deb, members)
    deb_arch = control["Architecture"]
    if deb_arch not in ARCHITECTURES:
        raise RepackageError(f"Only Linux Debian amd64/arm64 are accepted; {deb_arch!r} may be Termux")
    arch = ARCHITECTURES[deb_arch]
    if "packages" in raw_rules:
        try:
            raw_rules = raw_rules["packages"][control["Package"]]
        except (KeyError, TypeError) as error:
            raise RepackageError(f"No reviewed rules for Debian package {control['Package']}") from error
    rules = architecture_rules(raw_rules, arch)
    pkgname = rules.get("pkgname", control["Package"])
    if not re.fullmatch(r"[a-z0-9@_+][a-z0-9@._+\-]*", pkgname):
        raise RepackageError("Invalid Arch package name")
    pkgver = rules.get("pkgver", control["Version"])
    if not re.fullmatch(r"[A-Za-z0-9._+]+", pkgver):
        raise RepackageError("Set an explicit Arch pkgver rule for Debian revisions, epochs, or '~' prereleases")
    if not re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)?", str(pkgrel)):
        raise RepackageError("Invalid Arch pkgrel")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    if epoch < 0:
        raise RepackageError("SOURCE_DATE_EPOCH must be nonnegative")
    rules["depends"] = reviewed_dependencies(control, rules)
    installed_size, paths, payload_metadata = 0, {}, {}
    skill_owner, overlay, skill_pin, payload_files = None, {}, None, {}
    if rules.get('omarchy_skill'):
        if omarchy_skill is None:
            raise RepackageError('Taskarian Arch packages require a verified stable Omarchy skill snapshot')
        spec = importlib.util.spec_from_file_location('arch_omarchy_skill', Path(__file__).with_name('omarchy-skill.py'))
        skill_owner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(skill_owner)
    metadata_paths = {"usr/lib/taskarian/" + name for name in ("build-platform.json", "runtime-default.json")}
    with deb_tar(deb, members, "data") as archive:
        for member in archive:
            normalized = normalized_member(member, rules, epoch)
            if not normalized.name:
                continue
            if normalized.name in paths and not (normalized.isdir() and paths[normalized.name] == "dir"):
                raise RepackageError(f"Duplicate mapped payload path {normalized.name}")
            paths[normalized.name] = "dir" if normalized.isdir() else "file"
            if normalized.isfile():
                installed_size += normalized.size
                if normalized.name in metadata_paths:
                    payload_metadata[normalized.name] = json.load(archive.extractfile(member))
                if skill_owner and normalized.name in skill_owner.REQUIRED:
                    if member.size > 256 * 1024 ** 2:
                        raise RepackageError('Unexpectedly large Pi/license metadata')
                    payload_files[normalized.name] = archive.extractfile(member).read()
    if skill_owner:
        overlay, skill_pin = skill_owner.overlay(omarchy_skill, payload_files, control['Version'])
        for name, data in overlay.items():
            safe_path(name)
            if name in paths and name not in payload_files:
                raise RepackageError('Omarchy overlay would replace an unreviewed Debian payload: ' + name)
            installed_size += len(data) - len(payload_files.get(name, b''))
    install = None
    if rules.get("maintainer_adapter") == "taskarian-linux-v1":
        if control["Package"] != "taskarian" or set(payload_metadata) != metadata_paths:
            raise RepackageError("Native adapter requires the complete Taskarian Linux package")
        stamp = payload_metadata["usr/lib/taskarian/build-platform.json"]
        if stamp.get("platform") != "linux" or stamp.get("architecture") != deb_arch:
            raise RepackageError("Taskarian payload platform/architecture differs from Debian control")
        if stamp.get("packageVersion") != control["Version"]:
            raise RepackageError("Taskarian payload version differs from Debian control")
        if control_files.get("conffiles") != b"/etc/apparmor.d/taskarian-podman\n":
            raise RepackageError("Debian conffiles needs native backup review")
        if SCRIPT_NAMES & control_files.keys() != {"postinst", "postrm"}:
            raise RepackageError("Native adapter requires reviewed postinst and postrm")
        install = native_install(payload_metadata)
    omissions = review_scripts(control_files, rules, payload_metadata)
    metadata = {"pkgname": [pkgname], "pkgbase": [pkgname], "pkgver": [pkgver + "-" + str(pkgrel)],
                "pkgdesc": [rules.get("description", control.get("Description", pkgname).splitlines()[0])],
                "url": [rules.get("url", control.get("Homepage", "https://github.com/openresearchtools"))],
                "builddate": [str(epoch)], "packager": ["Open Research Tools"], "size": [str(installed_size)],
                "arch": [arch], "license": rules.get("license", ["custom"]), "depend": rules["depends"],
                "xdata": ["pkgtype=pkg"]}
    for source, target in (("optdepends", "optdepend"), ("provides", "provides"), ("conflicts", "conflict"), ("replaces", "replaces"), ("backup", "backup")):
        if source in rules:
            metadata[target] = rules[source]
    pkginfo = "".join(f"{key} = {line_value(value, key)}\n" for key, values in metadata.items() for value in values).encode()
    output.mkdir(parents=True, exist_ok=True)
    result = output / f"{pkgname}-{pkgver}-{pkgrel}-{arch}.pkg.tar.{compression}"
    temporary = result.with_name(result.name + ".tmp")
    process = None
    try:
        if compression == "zst":
            process = subprocess.Popen(["zstd", "--quiet", "--threads=0", "-6", "-f", "-o", str(temporary)], stdin=subprocess.PIPE)
            stream = process.stdin
        elif compression == "gz":
            process = None
            stream = gzip.GzipFile(filename=str(temporary), mode="wb", mtime=epoch)
        else:
            raise RepackageError("Compression must be zst or gz")
        try:
            with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as target:
                item = tarfile.TarInfo(".PKGINFO"); item.size = len(pkginfo); item.mode = 0o644; item.mtime = epoch
                target.addfile(item, io.BytesIO(pkginfo))
                if install is not None:
                    item = tarfile.TarInfo(".INSTALL"); item.size = len(install); item.mode = 0o644; item.mtime = epoch
                    target.addfile(item, io.BytesIO(install))
                with deb_tar(deb, members, "data") as source:
                    emitted = set()
                    for member in source:
                        normalized = normalized_member(member, rules, epoch)
                        if not normalized.name or normalized.name in emitted:
                            continue
                        emitted.add(normalized.name)
                        if normalized.name in overlay:
                            normalized.size = len(overlay[normalized.name])
                            target.addfile(normalized, io.BytesIO(overlay[normalized.name]))
                        else:
                            target.addfile(normalized, source.extractfile(member) if member.isfile() else None)
                for name in sorted(overlay):
                    if name in emitted:
                        continue
                    for parent in reversed(PurePosixPath(name).parents):
                        value = str(parent)
                        if value == '.' or value in emitted:
                            continue
                        if value in paths and paths[value] != 'dir':
                            raise RepackageError('Omarchy overlay parent is not a directory: ' + value)
                        item = tarfile.TarInfo(value); item.type = tarfile.DIRTYPE
                        item.mode, item.mtime = 0o755, epoch
                        target.addfile(item); emitted.add(value)
                    data = overlay[name]
                    item = tarfile.TarInfo(name); item.size = len(data)
                    item.mode, item.mtime = 0o644, epoch
                    target.addfile(item, io.BytesIO(data)); emitted.add(name)
        finally:
            stream.close()
        if process and process.wait() != 0:
            raise RepackageError("zstd compression failed")
        if result.exists() and sha256(result) != sha256(temporary):
            raise RepackageError(f"Refusing to replace different bytes at {result}; increment pkgrel")
        temporary.replace(result)
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait()
        if temporary.exists():
            temporary.unlink()
    provenance = {"schema": 1, "source_deb": deb.name, "source_sha256": sha256(deb), "debian_package": control["Package"],
                  "debian_version": control["Version"], "debian_architecture": deb_arch,
                  "arch_package": result.name, "arch_sha256": sha256(result), "pkgname": pkgname,
                  "pkgver": pkgver, "pkgrel": str(pkgrel), "architecture": arch,
                  "depends": rules["depends"], "reviewed_script_omissions": omissions,
                  "maintainer_adapter": rules.get("maintainer_adapter"),
                  "native_install_sha256": hashlib.sha256(install).hexdigest() if install else None,
                  "rules_sha256": hashlib.sha256(json.dumps(raw_rules, sort_keys=True).encode()).hexdigest(),
                  "path_mappings": rules.get("path_mappings", {})}
    if skill_pin:
        provenance['omarchy_skill'] = skill_pin
        provenance['arch_overlay_sha256'] = {name: hashlib.sha256(data).hexdigest()
                                            for name, data in sorted(overlay.items())}
    return result, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pkgrel", default="1")
    parser.add_argument("--compression", choices=("zst", "gz"), default="zst")
    parser.add_argument("--omarchy-skill-directory", type=Path)
    args = parser.parse_args(argv)
    try:
        result, provenance = build_package(args.deb, json.loads(args.rules.read_text()), args.output,
                                           args.pkgrel, args.compression, args.omarchy_skill_directory)
        result.with_name(result.name + ".provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
        print(result)
        return 0
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print("Arch repackaging failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
