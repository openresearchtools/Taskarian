#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Add native Arch packages from the publisher's already verified Linux DEBs.

No network access, application rebuilding, Debian script execution, release
creation, or remote uploads. The normal publisher uploads the resulting assets
alongside its original installers to the same release.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.parse import quote


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checksums(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if not match:
            raise ValueError("Malformed publisher SHA256SUMS entry")
        checksum, name = match.groups()
        if Path(name).name != name or name.startswith(".") or name in result:
            raise ValueError("Unsafe or repeated publisher checksum filename")
        result[name] = checksum
    return result


def prepare(input_directory: Path, output: Path, tag: str, repository: str,
            pkgrel: str = "1", architectures: list[str] | None = None,
            omarchy_skill: Path | None = None) -> list[Path]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Specify the GitHub owner/repository")
    sums = checksums(input_directory / "SHA256SUMS")
    manifest_path = input_directory / "release.json"
    manifest_digest = digest(manifest_path)
    if sums.get("release.json") != manifest_digest:
        raise ValueError("Publisher release.json checksum mismatch")
    manifest = json.loads(manifest_path.read_text())
    if not tag or manifest.get("tag") != tag:
        raise ValueError("Selected release tag differs from publisher release.json")
    requested = set(architectures or ("amd64", "arm64"))
    selected = {}
    for record in manifest["packages"]:
        architecture = record.get("Architecture")
        if architecture not in requested:
            continue
        if architecture in selected or record.get("Package") != "taskarian":
            raise ValueError("Expected exactly one native Taskarian package per Linux architecture")
        name = record["asset"]
        if Path(name).name != name or not re.fullmatch(r"taskarian_[A-Za-z0-9.+~_-]+_" + architecture + r"\.deb", name):
            raise ValueError("Unsafe or unexpected Linux package asset name")
        source = input_directory / name
        actual = digest(source)
        if actual != record["sha256"] or actual != sums.get(name) or source.stat().st_size != record["size"]:
            raise ValueError("Publisher DEB digest/size mismatch: " + name)
        build = record.get("build", {})
        if build.get("platform") != "linux" or build.get("architecture") != architecture:
            raise ValueError("Refusing a non-Linux package: " + name)
        selected[architecture] = (record, source)
    if set(selected) != requested:
        raise ValueError("Publisher is missing requested Linux amd64/arm64 packages")

    spec = importlib.util.spec_from_file_location("arch_repackage", Path(__file__).with_name("repackage.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rules_path = Path(__file__).with_name("rules.json")
    rules = json.loads(rules_path.read_text())
    created = []
    for architecture, (record, source) in sorted(selected.items()):
        control, _ = module.read_control(source, module.ar_members(source))
        if (control["Architecture"] != architecture or control["Package"] != record["Package"]
                or control["Version"] != record["Version"]):
            raise ValueError("Debian control differs from publisher manifest: " + source.name)
        package, provenance = module.build_package(source, rules, output, pkgrel, "zst", omarchy_skill)
        provenance.update(source_repository=repository, source_release_tag=tag,
                          source_release_asset_url=f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{source.name}",
                          source_release_manifest_sha256=manifest_digest,
                          source_commit=manifest["sourceCommit"])
        provenance['packaging_source_commit'] = subprocess.check_output(
            ['git', '-C', str(Path(__file__).resolve().parents[3]), 'rev-parse', 'HEAD'], text=True).strip()
        directory = Path(__file__).parent
        provenance["packaging_sources_sha256"] = {str(path.relative_to(directory)): digest(path)
            for path in sorted(directory.rglob("*")) if path.is_file() and path.suffix != ".pyc"}
        provenance_path = package.with_name(package.name + ".provenance.json")
        provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
        created.extend((package, provenance_path))
        print(f"Prepared {package.name} from {source.name} (embedded version {provenance['debian_version']})", flush=True)
    checksum_path = output / "SHA256SUMS.arch.txt"
    checksum_path.write_text("".join(f"{digest(path)}  {path.name}\n" for path in sorted(created)))
    created.append(checksum_path)
    return created


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "openresearchtools/Taskarian"))
    parser.add_argument("--input-directory", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--pkgrel", default="1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), action="append")
    parser.add_argument("--omarchy-skill-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        created = prepare(args.input_directory, args.output, args.release_tag,
                          args.repository, args.pkgrel, args.architecture, args.omarchy_skill_directory)
        print(f"Prepared {len(created)} Arch release assets; normal publisher owns upload")
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print("Arch release packaging failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
