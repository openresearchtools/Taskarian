#!/usr/bin/env python3
"""Reuse verified browser artifacts without consuming the compiler-cache quota."""
import argparse
import ast
import base64
from datetime import datetime
from functools import cache
import hashlib
import fnmatch
import importlib.util
import io
import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tarfile
import zipfile

from release import validate_termux_update, TERMUX_UPDATE_ASSET, TERMUX_BOOTSTRAP_ASSET, TERMUX_PREPARER_ASSET

ROOT = Path(__file__).resolve().parents[2]
VERIFIER = 'agent/packaging/browser-component.py'
EQUIVALENCE = 'browser-input-equivalence.json'
NATIVE_ENGINE_RECEIPT = 'browser-native-engine.json'
NATIVE_POLICY_FILES = (
    'browser/build.gradle',
    'browser/taskarian/scripts/build-linux-external.sh',
    'browser/taskarian/ci/build-browser-artifact.sh',
    'browser/python/mozbuild/mozbuild/artifacts.py',
    'browser/toolkit/mozapps/extensions/moz.build',
)
PRODUCT_VERSION = 'browser/taskarian/config/version.txt'
RELEASE_RUNTIME = 'agent/packaging/release.py'
RESOURCE_GENERATORS = (
    'browser/AGENTS.md',
    'browser/taskarian/README.md',
    'browser/taskarian/UPDATING-FIREFOX.md',
    'browser/taskarian/display/README.md',
    'browser/LICENSE',
    'browser/COPYING',
    'browser/toolkit/content/license.html',
    'browser/taskarian/third_party/tor.toml',
    'browser/taskarian/scripts/application_ini.py',
    'browser/taskarian/scripts/generate-third-party-notices.py',
    'browser/taskarian/scripts/verify_browser_legal_payload.py',
    'browser/taskarian/packaging/licenses.json',
    'browser/taskarian/packaging/copyright',
    'browser/taskarian/packaging/app-license-body.html',
    'browser/browser/base/content/overrides/app-license-body.html',
)
ANDROID_ONLY_MODULE = 'browser/mobile/shared/modules/geckoview/TaskarianAndroid.sys.mjs'
# Desktop DIST_SUBDIR=browser resources are not native/JNI input. The Android
# packager does not include that subtree; any optional copy still must be fresh.
ANDROID_DESKTOP_RESOURCES = (
    'browser/taskarian/browser/components/agent/NativeSidecar.sys.mjs',
    'browser/taskarian/browser/components/agent/TaskarianAgent.sys.mjs',
    'browser/taskarian/browser/components/agent/agent.css',
)

# Explicit disabled-test metadata: never compiler, runtime or generated JNI inputs.
FIXTURE_METADATA = (
    'browser/devtools/client/aboutdebugging/test/node/yarn.lock',
    'browser/devtools/client/accessibility/test/node/yarn.lock',
    'browser/devtools/client/application/test/node/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/react/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/parcel/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/webpack3-babel6/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/webpack3-babel7/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/webpack3/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/webpack4-babel6/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/webpack4-babel7/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/builds/webpack4/yarn.lock',
    'browser/devtools/client/debugger/test/mochitest/examples/sourcemapped/yarn.lock',
    'browser/devtools/client/inspector/compatibility/test/node/yarn.lock',
    'browser/devtools/client/netmonitor/test/node/yarn.lock',
    'browser/testing/talos/talos/tests/devtools/addon/content/pages/custom/debugger/app/package.json',
    'browser/testing/talos/talos/tests/devtools/addon/content/pages/custom/debugger/app/yarn.lock',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/angular/package-lock.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/emberjs/package-lock.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/preact/package-lock.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/react-redux/package-lock.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/react-redux/package.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/react/package-lock.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/vuejs-cli/package-lock.json',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/architecture-examples/vuejs-cli/yarn.lock',
    'browser/third_party/webkit/PerformanceTests/Speedometer/resources/todomvc/vanilla-examples/es2015-babel-webpack/package-lock.json',
)


class NetworkUnavailable(RuntimeError):
    """Retry the reuse check later instead of starting duplicate compilation."""


def network_command(command, binary=False):
    for attempt in range(3):
        result = subprocess.run(command, cwd=ROOT, text=not binary, capture_output=True)
        if result.returncode == 0:
            return result.stdout
        diagnostic = result.stderr + result.stdout
        if binary:
            diagnostic = diagnostic.decode(errors='replace')
        if re.search(r'HTTP 404|"status"\s*:\s*"?404|does not exist in|not our ref|couldn.t find remote ref', diagnostic):
            raise FileNotFoundError('Published producer metadata/artifact is no longer available')
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    raise NetworkUnavailable('Artifact/producer metadata could not be fetched after three attempts; retry this job without rebuilding.')


def download(repository, run, name, directory):
    network_command(['gh', 'run', 'download', str(run), '--repo', repository,
                     '--name', name, '--dir', str(directory)])


def input_paths(target, include_verifier=False):
    # Cache policy is not compiler input. Preserve the old calculation only to
    # prove equivalence of already-published artifacts; no producer is relabelled.
    paths = ['browser', 'references/connector', 'references/vendor/translators', 'references/legal',
             'references/web/theme.css',
             'agent/packaging/references-component.py',
             'agent/packaging/source_cache.py'] + ([VERIFIER] if include_verifier else [])
    if target.startswith('android'):
        paths += ['.github/builders/android.yml', 'auth', 'agent/native/remote', 'agent/native/phone-control',
                  'agent/packaging/android-notices.py', 'agent/packaging/build.py',
                  'agent/packaging/photon', 'agent/packaging/machines/build.py',
                  'agent/machines/rust-toolchain',
                  'agent/packaging/applications.py', 'references/runtime-payload.json',
                  'references/package.json', 'references/package-lock.json',
                  'references/THIRD_PARTY_NOTICES.md', 'references/vendor/*/source.json',
                  'references/vendor/npm/sources.json', 'office/vendor/npm/sources.json',
                  ':(glob)references/vendor/**/COPYING*', ':(glob)references/vendor/**/LICENSE*',
                  ':(glob)references/vendor/**/README*',
                  'office/package.json', 'office/package-lock.json',
                  'agent/systematic-reviewer/package.json', 'agent/systematic-reviewer/LICENSE', 'agent/systematic-reviewer/NOTICE',
                  'agent/packaging/release_version.py', 'agent/packaging/release.py',
                  'agent/packaging/licenses.mjs', 'agent/packaging/node', 'agent/packaging/termux/search',
                  'agent/src/server/licenses.mjs',
                  'agent/package.json', 'agent/package-lock.json', 'agent/LICENSE',
                  'agent/PI_UPSTREAM.md', 'agent/THIRD_PARTY_NOTICES.md',
                  'agent/licenses', 'agent/pi/package.json', 'agent/pi/LICENSE',
                  'agent/pi/NOTICE', 'agent/search',
                  'agent/pi/vendor/pillama/package.json', 'agent/pi/vendor/pillama/LICENSE',
                  'agent/pi/vendor/pillama/NOTICE', 'agent/pi/vendor/pillama.upstream.json',
                  'agent/pi/vendor/pi-codex-usage/LICENSE', 'agent/pi/vendor/pi-codex-usage.upstream.json',
                  'os/NOTICE', 'os/licenses', 'os/upstream/sources.json', 'os/packaging',
                  ':(glob)os/upstream/**/COPYING*', ':(glob)os/upstream/**/LICENSE*',
                  ':(glob)os/upstream/**/NOTICE*', ':(glob)os/upstream/**/THIRD_PARTY*',
                  'agent/packaging/termux/os.py', '.github/workflows/machines-native.yml',
                  'agent/src/server/access/NOTICE', 'agent/src/server/access/TORKITTEN-LICENSE',
                  'agent/src/server/models/third_party', 'agent/src/server/updates/platform-packages.mjs',
                  'agent/src/server/platform/termux/updates-bootstrap.mjs',
                  'agent/src/server/platform/termux/updates-prepare.py',
                  'agent/packaging/termux/install.sh',
                  '.github/workflows/auth-native.yml', '.github/workflows/search-native.yml']
    else:
        paths += ['.github/builders/linux.yml', 'agent/packaging/linux/prepare-runner.sh',
                  ':(exclude)browser/mobile/android', ':(exclude)browser/taskarian/android']
    return paths


@cache
def input_listing(target, include_verifier):
    return subprocess.check_output(['git', 'ls-files', '--stage', '-z', '--',
                                    *input_paths(target, include_verifier)], cwd=ROOT)


def fingerprint(target, legacy_verifier=None, legacy_android_module=None,
                legacy_linux_workflow=None, legacy_fixture_blobs=None, legacy_release_module=None):
    if legacy_android_module and target.startswith('android'):
        raise ValueError('Android APK resources cannot use Linux input equivalence')
    substitutions = {path: blob for path, blob in (
        (VERIFIER, legacy_verifier), (ANDROID_ONLY_MODULE, legacy_android_module),
        ('.github/builders/linux.yml', legacy_linux_workflow),
        (RELEASE_RUNTIME, legacy_release_module)) if blob}
    if legacy_fixture_blobs:
        if not isinstance(legacy_fixture_blobs, dict) or set(legacy_fixture_blobs) - set(FIXTURE_METADATA):
            raise ValueError('Only declared fixture metadata can use historical lock identities')
        substitutions.update(legacy_fixture_blobs)
    for blob in substitutions.values():
        if not isinstance(blob, str) or not re.fullmatch('[0-9a-f]{40}', blob):
            raise ValueError('Invalid historical source identity')
    encoded = {path.encode(): blob for path, blob in substitutions.items()}
    found = set()
    entries = []
    for entry in input_listing(target, bool(legacy_verifier)).split(b'\0'):
        if b'\t' in entry:
            path = entry.split(b'\t', 1)[1]
            if path in encoded:
                entry = f'100644 {encoded[path]} 0\t'.encode() + path
                found.add(path)
            elif target.startswith('android') and path.decode() == RELEASE_RUNTIME:
                mode, blob, stage = entry.split(b'\t', 1)[0].split()
                entry = b' '.join((mode, release_runtime_blob(blob.decode()).encode(), stage)) + b'\t' + path
        entries.append(entry)
    if found != encoded.keys():
        raise ValueError('Missing tracked historical input')
    return hashlib.sha256(target.encode() + b'\0' + b'\0'.join(entries)).hexdigest()


@cache
def ensure_source(source):
    if not isinstance(source, str) or not re.fullmatch('[0-9a-f]{40}', source):
        raise ValueError('Invalid browser producer source')
    if subprocess.run(['git', 'cat-file', '-e', source + '^{commit}'], cwd=ROOT,
                      stderr=subprocess.DEVNULL).returncode:
        network_command(['git', 'fetch', '--filter=blob:none', '--no-tags', '--depth=1', 'origin', source])


def linux_native_recipe(source):
    # Scheduling, restore and upload policy cannot change compiled browser bits.
    # Keep the actual toolchain environment and native commands in the proof.
    recipe = {}
    for name in ('Select the native target', 'Prepare runner', 'Install the compatible Rust toolchain',
                 'Bootstrap pinned Mozilla toolchain', 'Optional native Rust cache warm-up',
                 'Build the complete browser archive'):
        block = re.search(r'^    - (?:id: .+\n      )?name: ' + re.escape(name) +
                          r'\n((?:      .*\n|\n)+)', source, re.M)
        if not block:
            raise ValueError('Linux native recipe is missing ' + name)
        recipe[name] = [line for line in block[1].splitlines()
                        if not re.match(r'      (?:if|timeout-minutes):', line)]
    environment = re.search(r'^    env:\n((?:      .+\n)+)', source, re.M)
    runner = re.search(r'^    runs-on: (.+)$', source, re.M)
    if not environment or not runner:
        raise ValueError('Linux native recipe is missing its environment')
    recipe['runner'] = runner[1]
    recipe['environment'] = '\n'.join(line for line in environment[1].splitlines()
                                      if not re.match(r'      (?:SCCACHE_[A-Z_]+|GH_TOKEN):', line))
    for action in ('actions/setup-node',):
        block = re.search(r'^    - uses: ' + re.escape(action) + r'@(.+\n)((?:      .*\n|\n)*)', source, re.M)
        if not block:
            raise ValueError('Linux native recipe is missing ' + action)
        recipe[action] = block[1] + '\n'.join(line for line in block[2].splitlines()
                                             if not re.match(r'      if:', line))
    return recipe


@cache
def equivalent_source(source, target):
    ensure_source(source)
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if target.startswith('android'):
        return source == current  # Complete APK resources are revision-specific.
    workflow = '.github/builders/linux.yml'
    producer_recipe = network_command(['git', 'show', source + ':' + workflow])
    requested_recipe = (ROOT / workflow).read_text()
    if linux_native_recipe(producer_recipe) != linux_native_recipe(requested_recipe):
        return False
    paths = [*input_paths(target), ':(exclude)' + workflow, ':(exclude)' + ANDROID_ONLY_MODULE,
             *(':(exclude)' + path for path in FIXTURE_METADATA)]
    return not network_command(['git', 'diff-tree', '--no-commit-id', '--raw', '-r', '--no-renames',
                                source, current, '--', *paths])


def artifact_policy_base(data):
    """Remove only explicitly delimited artifact-only policy additions."""
    output, inside = [], False
    for line in data.splitlines(keepends=True):
        marker = line.strip()
        if marker == b'# TASKARIAN_ARTIFACT_ONLY_BEGIN':
            if inside:
                raise ValueError('Nested artifact-only policy block')
            inside = True
        elif marker == b'# TASKARIAN_ARTIFACT_ONLY_END':
            if not inside:
                raise ValueError('Unmatched artifact-only policy block')
            inside = False
        elif not inside:
            output.append(line)
    if inside:
        raise ValueError('Unclosed artifact-only policy block')
    return native_gradle_base(b''.join(output))


def native_gradle_base(data):
    """Strip only the reviewed, inactive-in-native-builds Maven consumer block."""
    start = b'// Taskarian prebuilt GeckoView Maven dependency BEGIN\n'
    end = b'// Taskarian prebuilt GeckoView Maven dependency END\n'
    if start not in data and end not in data:
        return data
    if data.count(start) != 1 or data.count(end) != 1:
        raise ValueError('Invalid GeckoView Maven consumer boundaries')
    first, last = data.index(start), data.index(end) + len(end)
    if (last <= first or data[last:last + 1] != b'\n' or
            hashlib.sha256(data[first:last]).hexdigest() !=
            '1b1213107ae751eb87831e28495153fb0b2d285defe04dcda5a6dc0abd6dfbf8'):
        raise ValueError('GeckoView Maven consumer changed; review its native input scope')
    return data[:first] + data[last + 1:]


def blob_identity(data):
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


@cache
def release_runtime_blob(blob):
    """Hash the existing companion API, independently of release-note prose."""
    tree = ast.parse(network_command(['git', 'cat-file', 'blob', blob]))
    wanted = {'validate_termux_update', 'termux_os_identity', 'TERMUX_UPDATE_ASSET',
              'TERMUX_BOOTSTRAP_ASSET', 'TERMUX_PREPARER_ASSET', 'TERMUX_OS_FILES'}
    declarations = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            declarations[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    declarations[target.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                declarations[alias.asname or alias.name.split('.')[0]] = node
    if not wanted <= declarations.keys():
        raise ValueError('Android companion API declarations are missing')
    selected = {}
    pending = list(wanted)
    while pending:
        name = pending.pop()
        if name in selected or name not in declarations:
            continue
        node = declarations[name]
        selected[name] = node
        pending.extend(value.id for value in ast.walk(node)
                       if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load))
    nodes = {node.lineno: node for node in selected.values()}
    payload = ast.dump(ast.Module(body=[nodes[line] for line in sorted(nodes)], type_ignores=[]),
                       include_attributes=False).encode()
    return blob_identity(b'taskarian-android-companion-api-1\0' + payload)


@cache
def packaged_resources(source):
    """Resolve only resources declared by Taskarian's native build owners."""
    ensure_source(source)
    paths = network_command(['git', 'ls-tree', '-r', '--name-only', source, '--',
                             'browser/taskarian', 'browser/browser'], binary=True).decode().splitlines()
    existing, resources = set(paths), set()
    resource_owners = {'EXTRA_JS_MODULES', 'JS_PREFERENCE_FILES', 'JS_PREFERENCE_PP_FILES',
                       'FINAL_TARGET_FILES', 'FINAL_TARGET_PP_FILES', 'JAR_MANIFESTS'}

    def add(owner, value):
        if value.startswith(('!', '%')):
            return
        path = posixpath.normpath('browser' + value if value.startswith('/') else
                                  posixpath.join(posixpath.dirname(owner), value))
        if not path.startswith('browser/taskarian/'):
            return
        if not any(character in path for character in '*?['):
            if path in existing:
                resources.add(path)
        else:
            resources.update(candidate for candidate in existing if fnmatch.fnmatchcase(candidate, path))

    for owner in paths:
        if owner.startswith('browser/taskarian/') and owner.endswith('/moz.build'):
            body = network_command(['git', 'show', source + ':' + owner])
            for node in ast.walk(ast.parse(body)):
                if not isinstance(node, ast.AugAssign):
                    continue
                target = node.target
                while isinstance(target, (ast.Attribute, ast.Subscript)):
                    target = target.value
                if (not isinstance(target, ast.Name) or target.id not in resource_owners or
                        not isinstance(node.value, (ast.List, ast.Tuple))):
                    continue
                for value in node.value.elts:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        add(owner, value.value)
        elif owner.endswith('/jar.mn'):
            # Jar manifests only map packaged data; never infer resource status
            # from a filename suffix or from arbitrary JavaScript in Gecko.
            resources.add(owner)
            body = network_command(['git', 'show', source + ':' + owner])
            for value in re.findall(r'^\s*[*+]?\s*[^#%\s][^\n]*?\(([^)]+)\)', body, re.M):
                add(owner, value)
    return frozenset(resources)


@cache
def runtime_product_version(source):
    # Historical engines compiled the product number. Only the native launcher
    # that reads the packaged XRE app data can treat it as a resource input.
    launcher = network_command(['git', 'show', source + ':browser/browser/app/nsBrowserApp.cpp'])
    configure = network_command(['git', 'show', source + ':browser/build/moz.configure/init.configure'])
    return ('config.appDataPath = taskarianAppData;' in launcher and
            re.search(r'"--with-version-file-path",\n(?:[^\n]*\n){1,4}\s*default=None,', configure) is not None)


@cache
def linux_engine_inputs(source, target):
    if target not in ('linux-amd64', 'linux-arm64'):
        raise ValueError('Linux native engine target required')
    ensure_source(source)
    workflow = '.github/builders/linux.yml'
    paths = ['browser', workflow, 'agent/packaging/linux/prepare-runner.sh']
    entries = network_command(['git', 'ls-tree', '-r', '-z', source, '--', *paths], binary=True)
    excluded = {*FIXTURE_METADATA, *packaged_resources(source), *RESOURCE_GENERATORS,
                ANDROID_ONLY_MODULE}
    if runtime_product_version(source):
        excluded.add(PRODUCT_VERSION)
    canonical = []
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        header, raw_path = entry.split(b'\t', 1)
        path = raw_path.decode()
        if (path in excluded or path.startswith(('browser/mobile/android/', 'browser/taskarian/android/',
                'browser/third_party/tor/', 'browser/taskarian/third_party/tor-notices/',
                'browser/taskarian/vendor/termux-x11/', 'browser/taskarian/vendor/display-dependencies/',
                'browser/taskarian/display/linux/', 'browser/taskarian/display/vendor/',
                'browser/taskarian/display/patches/')) or path == 'browser/taskarian/display/build.py'):
            continue
        mode, kind, identity = header.decode().split()
        if path == workflow:
            text = network_command(['git', 'show', source + ':' + path])
            identity = blob_identity(json.dumps(linux_native_recipe(text), sort_keys=True).encode())
        elif path in NATIVE_POLICY_FILES:
            text = network_command(['git', 'show', source + ':' + path]).encode()
            identity = blob_identity(artifact_policy_base(text))
        canonical.append(f'{mode} {kind} {identity}\t'.encode() + raw_path)
    return hashlib.sha256(target.encode() + b'\0' + b'\0'.join(canonical)).hexdigest()


def native_elf_inventory(archive):
    """Read actual native payloads from the verified custom browser package."""
    result = {}
    with tarfile.open(archive, 'r:*') as package:
        names = set()
        for member in package:
            if member.name in names:
                raise ValueError('Duplicate native archive member')
            names.add(member.name)
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'taskarian':
                raise ValueError('Native browser archive leaves its product directory')
            if not member.isfile() and not member.islnk():
                continue
            with package.extractfile(member) as stream:
                header = stream.read(4)
                if header == b'\x7fELF':
                    checksum = hashlib.sha256(header)
                    while chunk := stream.read(1024 * 1024):
                        checksum.update(chunk)
                    result['/'.join(path.parts[1:])] = checksum.hexdigest()
    if 'libxul.so' not in result or 'taskarian' not in result:
        raise ValueError('Native browser archive lacks its engine or launcher')
    return result


def verify_linux_engine(directory, target, manifest):
    receipt = Path(directory) / NATIVE_ENGINE_RECEIPT
    if not receipt.exists():
        if 'native_engine_source' in manifest or 'native_engine_receipt_sha256' in manifest:
            raise ValueError('Linux native engine manifest is missing its receipt')
        return
    record = json.loads(receipt.read_text())
    if (not isinstance(record, dict) or
            manifest.get('native_engine_source') != record.get('source') or
            manifest.get('native_engine_receipt_sha256') != hashlib.sha256(receipt.read_bytes()).hexdigest()):
        raise ValueError('Linux native engine manifest differs from its receipt')
    source = manifest['taskarian_commit']
    if (record.get('schema') != 1 or record.get('target') != target or
            record.get('resourceSource') != source or
            linux_engine_inputs(record.get('source', ''), target) != linux_engine_inputs(source, target) or
            record.get('inputSha256') != linux_engine_inputs(source, target)):
        raise ValueError('Linux native engine provenance or effective compiler inputs differ')
    archives = list(Path(directory).glob('taskarian*.tar.*'))
    if len(archives) != 1 or native_elf_inventory(archives[0]) != record.get('nativeFiles'):
        raise ValueError('Reassembled browser changed its borrowed native ELF payload')
    producer = record.get('producer', {})
    if (producer.get('source') != record.get('source') or
            producer.get('runSource') != record.get('source') or
            producer.get('repository') != 'openresearchtools/taskarian-build-' + target.removeprefix('linux-') or
            not re.fullmatch('[0-9a-f]{64}', producer.get('archiveSha256', '')) or
            not re.fullmatch('[0-9a-f]{64}', producer.get('manifestSha256', '')) or
            not producer.get('steps', {}).get('build') or
            any(step.get('conclusion') != 'success' for step in producer['steps']['build']) or
            producer.get('steps', {}).get('verify', {}).get('conclusion') != 'success' or
            producer.get('steps', {}).get('upload', {}).get('conclusion') != 'success'):
        raise ValueError('Linux native engine lacks its original verified producer proof')


@cache
def revision_browser_fingerprints(source, target):
    """Recover original archive identities from Git metadata, not source downloads."""
    ensure_source(source)
    results = set()
    for include_verifier in (False, True):
        paths = [path for path in input_paths(target, include_verifier) if not path.startswith(':')]
        listing = network_command(['git', 'ls-tree', '-r', '-z', source, '--', *paths], binary=True)
        entries = []
        for entry in listing.split(b'\0'):
            if not entry:
                continue
            header, path = entry.split(b'\t', 1)
            if path.startswith((b'browser/mobile/android/', b'browser/taskarian/android/')):
                continue
            mode, _kind, identity = header.split()
            entries.append(mode + b' ' + identity + b' 0\t' + path)
        results.add(hashlib.sha256(target.encode() + b'\0' + b'\0'.join(entries) + b'\0').hexdigest())
    return results


def restore_native_available(directory, target, digest):
    repository = os.environ['GITHUB_REPOSITORY']
    page = 1
    while True:
        artifacts = api(f'repos/{repository}/actions/artifacts?per_page=100&page={page}')['artifacts']
        for artifact in artifacts:
            if artifact['expired'] or not artifact['name'].startswith(f'taskarian-browser-{target}-'):
                continue
            try:
                run = api(f'repos/{repository}/actions/runs/{artifact["workflow_run"]["id"]}')
                source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
                evidence = completed_producer(repository, run, target, artifact)
                if not source or not evidence or linux_engine_inputs(source[1], target) != digest:
                    continue
                if artifact['name'].rsplit('-', 1)[1] not in revision_browser_fingerprints(source[1], target):
                    continue
                with tempfile.TemporaryDirectory(prefix='linux-engine-', dir=directory.parent) as temporary:
                    candidate = Path(temporary)
                    download(repository, run['id'], artifact['name'], candidate)
                    verify_checksums(candidate)
                    manifest_file = candidate / 'browser-artifact-manifest.txt'
                    manifest = dict(line.split('=', 1) for line in manifest_file.read_text().splitlines())
                    resource_source = manifest.get('taskarian_commit', '')
                    if (manifest.get('architecture') != target.removeprefix('linux-') or
                            manifest.get('browser_input_sha256') not in revision_browser_fingerprints(resource_source, target)):
                        raise ValueError('Linux engine archive original source/target identity differs')
                    archives = list(candidate.glob('taskarian*.tar.*'))
                    if len(archives) != 1:
                        raise ValueError('Expected one original native browser package')
                    archive = archives[0]
                    proof = {'repository': repository, 'run': run['id'], 'artifact': artifact['id'],
                        'runSource': source[1], 'source': resource_source, 'archive': archive.name,
                        'archiveSha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                        'manifestSha256': hashlib.sha256(manifest_file.read_bytes()).hexdigest(), 'steps': evidence}
                    existing = candidate / NATIVE_ENGINE_RECEIPT
                    if existing.exists():
                        verify_linux_engine(candidate, target, manifest)
                        record = json.loads(existing.read_text())
                    else:
                        # Old whole-archive aliases did not always retain the
                        # first compiler producer. Continue to its actual build
                        # upload instead of relabeling a restore as compilation.
                        if not evidence.get('build') or resource_source != source[1]:
                            continue
                        record = {'schema': 1, 'target': target, 'source': resource_source,
                                  'nativeFiles': native_elf_inventory(archive), 'producer': proof}
                    if linux_engine_inputs(record['source'], target) != digest:
                        continue
                    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
                    record.update(inputSha256=digest, resourceSource=current, restoredFrom=proof)
                    # Retain the complete downloaded producer package/provenance.
                    xpts = list(candidate.glob('*.xpt_artifacts.zip'))
                    if len(xpts) > 1:
                        raise ValueError('Duplicate native XPT metadata archives')
                    producer = directory / 'producer'
                    shutil.move(str(candidate), producer)
                    receipt = directory / NATIVE_ENGINE_RECEIPT
                    receipt.write_text(json.dumps(record, indent=2) + '\n')
                    with Path(os.environ['GITHUB_ENV']).open('a') as environment:
                        environment.write(f'MOZ_ARTIFACT_FILE={(producer / archive.name).resolve()}\n'
                                          f'TASKARIAN_LINUX_NATIVE_RECEIPT={receipt.resolve()}\n')
                        if xpts:
                            environment.write(f'TASKARIAN_LINUX_XPT_ARTIFACT={(producer / xpts[0].name).resolve()}\n')
                print(f'Reusing original Taskarian native engine {record["source"]} from {run["html_url"]}; '
                      'Mozilla will rebuild current resources.', flush=True)
                return True
            except (OSError, ValueError, KeyError, TypeError, UnicodeError, tarfile.TarError, subprocess.CalledProcessError) as error:
                for item in directory.iterdir():
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
                print(f'Skipping unusable native engine cache {artifact["id"]}: {error}', flush=True)
        if len(artifacts) < 100:
            return False
        page += 1


def restore_native(directory, target):
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'openresearchtools/taskarian-build-' + target.removeprefix('linux-'):
        raise ValueError('Native restoration must use the matching architecture builder')
    return browser_release().restore_engine(directory, target)


def record_native(directory, target):
    receipt = os.environ.get('TASKARIAN_LINUX_NATIVE_RECEIPT')
    if not receipt:
        if os.environ.get('MOZ_ARTIFACT_FILE'):
            raise ValueError('Release artifact builds require verified native producer provenance')
        return
    manifest_path = directory / 'browser-artifact-manifest.txt'
    manifest = dict(line.split('=', 1) for line in manifest_path.read_text().splitlines())
    record = json.loads(Path(receipt).read_text())
    record['resourceSource'] = manifest['taskarian_commit']
    destination = directory / NATIVE_ENGINE_RECEIPT
    destination.write_text(json.dumps(record, indent=2) + '\n')
    manifest.update(native_engine_source=record['source'],
                    native_engine_receipt_sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
    verify_linux_engine(directory, target, manifest)
    with manifest_path.open('a') as output:
        output.write(f'native_engine_source={manifest["native_engine_source"]}\n'
                     f'native_engine_receipt_sha256={manifest["native_engine_receipt_sha256"]}\n')


@cache
def browser_release():
    spec = importlib.util.spec_from_file_location('browser_release', ROOT / 'agent/packaging/browser-release.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def artifact_name(target, digest):
    return f'taskarian-browser-{target}-{digest}'


def api(endpoint):
    return json.loads(network_command(['gh', 'api', endpoint]))


def matching_input(directory, target, recorded, digest, producer, legacy_android_module=None):
    if not equivalent_source(producer, target):
        raise ValueError('Browser producer effective Git inputs differ from this source')
    if recorded == digest and not (Path(directory) / EQUIVALENCE).exists():
        return
    receipt = json.loads((Path(directory) / EQUIVALENCE).read_text())
    if not isinstance(receipt, dict):
        raise ValueError('Browser equivalence receipt must be an object')
    manifest = Path(directory) / ('build-manifest.json' if target.startswith('android') else 'browser-artifact-manifest.txt')
    fixture_blobs = receipt.get('legacyFixtureLockBlobs') or {path: source_blob(producer, path) for path in FIXTURE_METADATA}
    if not isinstance(fixture_blobs, dict) or set(fixture_blobs) != set(FIXTURE_METADATA):
        raise ValueError('Fixture metadata receipt must cover exactly the declared fixture files')
    if any(source_blob(producer, path) != blob for path, blob in fixture_blobs.items()):
        raise ValueError('Fixture metadata receipt differs from the actual producer Git objects')
    if (receipt.get('schema') != 1 or receipt.get('target') != target or
            receipt.get('producerSource') != producer or
            receipt.get('producerFingerprint') != recorded or
            receipt.get('compileFingerprint') != digest or
            receipt.get('producerManifestSha256') != hashlib.sha256(manifest.read_bytes()).hexdigest() or
            fingerprint(target, receipt.get('legacyVerifierBlob'),
                        receipt.get('legacyAndroidModuleBlob', legacy_android_module),
                        receipt.get('legacyLinuxWorkflowBlob'), fixture_blobs,
                        receipt.get('legacyReleaseModuleBlob')) != recorded):
        raise ValueError('Browser source equivalence does not match this source')
    if receipt.get('legacyReleaseModuleBlob') and source_blob(producer, RELEASE_RUNTIME) != receipt['legacyReleaseModuleBlob']:
        raise ValueError('Companion API receipt differs from its actual producer Git object')


def record_android_equivalence(directory, target):
    """Keep the original manifest when only the input projection was refined."""
    directory = Path(directory)
    manifest_file = directory / 'build-manifest.json'
    manifest = json.loads(manifest_file.read_text())
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if manifest.get('source') != current:
        raise ValueError('Android resource reuse requires the exact original source revision')
    digest = fingerprint(target)
    recorded = manifest.get('browser_input_sha256')
    if recorded == digest or (directory / EQUIVALENCE).exists():
        return
    legacy = source_blob(current, RELEASE_RUNTIME)
    if fingerprint(target, legacy_release_module=legacy) != recorded:
        raise ValueError('Android resource inputs differ beyond the companion API projection')
    receipt = {'schema': 1, 'target': target, 'producerSource': current,
               'producerFingerprint': recorded, 'compileFingerprint': digest,
               'producerManifestSha256': hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
               'legacyReleaseModuleBlob': legacy,
               'legacyFixtureLockBlobs': {path: source_blob(current, path) for path in FIXTURE_METADATA}}
    file = directory / EQUIVALENCE
    file.write_text(json.dumps(receipt, indent=2) + '\n')
    with (directory / 'SHA256SUMS').open('a') as output:
        output.write(hashlib.sha256(file.read_bytes()).hexdigest() + '  ' + file.name + '\n')


def verify_android_resources(apk):
    # EXTRA_JS_MODULES is copied into the current omnijar, never into Gecko ELF
    # libraries. Native reuse must not accidentally preserve the donor's module.
    with zipfile.ZipFile(apk) as archive:
        from android_native_sources import verify_android_tor
        verify_android_tor(archive, ROOT)
        names = archive.namelist()
        for directory, extension_id in [('references', 'references@taskarian.org'),
                                         ('references-host', 'references-host@taskarian.org')]:
            prefix = 'assets/' + directory + '/'
            manifest_path = prefix + 'manifest.json'
            if names.count(manifest_path) != 1:
                raise ValueError('APK References manifest is missing or duplicated: ' + directory)
            manifest = json.loads(archive.read(manifest_path))
            if manifest.get('browser_specific_settings', {}).get('gecko', {}).get('id') != extension_id:
                raise ValueError('APK References extension identity differs: ' + directory)
            locale = manifest.get('default_locale')
            if 'default_locale' in manifest and (not isinstance(locale, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', locale)
                                                or names.count(prefix + '_locales/' + locale + '/messages.json') != 1):
                raise ValueError('APK References default locale is missing: ' + directory)
        inventory_path = 'assets/references-runtime-sha256.json'
        if names.count(inventory_path) != 1:
            raise ValueError('APK References runtime inventory is missing or duplicated')
        inventory = json.loads(archive.read(inventory_path))
        prefix = 'assets/references/'
        payload = [name[len(prefix):] for name in names if name.startswith(prefix) and not name.endswith('/')]
        if (not isinstance(inventory, dict) or not inventory or len(payload) != len(set(payload))
                or set(payload) != inventory.keys()):
            raise ValueError('APK References capture inventory differs')
        for name, expected in inventory.items():
            if (not name or '\\' in name or any(part in {'', '.', '..'} for part in name.split('/'))
                    or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                    or hashlib.sha256(archive.read(prefix + name)).hexdigest() != expected):
                raise ValueError('APK References capture checksum mismatch: ' + name)
        with zipfile.ZipFile(io.BytesIO(archive.read('assets/omni.ja'))) as omni:
            if omni.read('modules/TaskarianAndroid.sys.mjs') != (ROOT / ANDROID_ONLY_MODULE).read_bytes():
                raise ValueError('Android omnijar module does not match the current source')
            for source in ANDROID_DESKTOP_RESOURCES:
                suffix = '/taskarian/agent/agent.css' if source.endswith('.css') else '/' + Path(source).name
                for name in omni.namelist():
                    if name.endswith(suffix):
                        expected = (ROOT / source).read_bytes() if (ROOT / source).is_file() else network_command(['git', 'show', 'HEAD:' + source]).encode()
                        if omni.read(name) != expected:
                            raise ValueError('Android optional desktop resource differs from the current source: ' + name)


def verify_checksums(directory):
    directory = Path(directory)
    checked = set()
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        if Path(name).name != name or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Invalid component checksum entry')
        with (directory / name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('Component checksum mismatch: ' + name)
        checked.add(name)
    payload = {item.name for item in directory.iterdir() if item.is_file() and item.name != 'SHA256SUMS'}
    if not checked or checked != payload:
        raise ValueError('Component checksums must cover the complete artifact')


def verify(directory, target, digest, legacy_android_module=None):
    directory = Path(directory)
    verify_checksums(directory)
    if target.startswith('android'):
        manifest = json.loads((directory / 'build-manifest.json').read_text())
        if not isinstance(manifest, dict):
            raise ValueError('Android component manifest must be an object')
        if manifest.get('architecture') != ('x86_64' if target == 'android-x86_64' else 'arm64-v8a'):
            raise ValueError('Android component inputs do not match this source')
        if not re.fullmatch('[0-9a-f]{40}', manifest.get('source', '')):
            raise ValueError('Android component has no producing source revision')
        if (manifest.get('product_version') != (ROOT / PRODUCT_VERSION).read_text().strip() or
                manifest.get('package_id') != 'com.taskarian' or not manifest.get('publisher_signed')):
            raise ValueError('Android component product version or publisher identity differs')
        apks = list(directory.glob('*.apk'))
        if len(apks) != 1 or manifest.get('apks', {}).get(apks[0].name) is None:
            raise ValueError('Expected one recorded production APK')
        with apks[0].open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['apks'][apks[0].name]:
                raise ValueError('Android component APK differs from its original build manifest')
        verify_android_resources(apks[0])
        with zipfile.ZipFile(apks[0]) as archive:
            pairing = validate_termux_update(json.loads(archive.read(TERMUX_UPDATE_ASSET)),
                                             version=(ROOT / PRODUCT_VERSION).read_text().strip(),
                                             abi=manifest['architecture'])
            if hashlib.sha256(archive.read(TERMUX_BOOTSTRAP_ASSET)).hexdigest() != pairing['bootstrapSha256']:
                raise ValueError('APK update bootstrap does not match its paired manifest')
            if archive.read(TERMUX_PREPARER_ASSET) != (ROOT / 'agent/src/server/platform/termux/updates-prepare.py').read_bytes():
                raise ValueError('APK bundled-runtime preparation does not match this source')
        expected_source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if pairing['sourceCommit'] != expected_source or pairing['sourceCommit'] != manifest['source']:
            raise ValueError('APK resources must be reassembled for this revision’s Termux packages')
        matching_input(directory, target, manifest.get('browser_input_sha256'), digest, manifest['source'])
        if manifest.get('native_gecko'):
            spec = importlib.util.spec_from_file_location('android_native_component', ROOT / 'agent/packaging/android-native-component.py')
            native_component = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(native_component)
            native_component.verify_native(apks[0], manifest['native_gecko'], target)
    else:
        manifest = dict(line.split('=', 1) for line in (directory / 'browser-artifact-manifest.txt').read_text().splitlines())
        if manifest.get('architecture') != target.removeprefix('linux-') or not re.fullmatch('[0-9a-f]{40}', manifest.get('taskarian_commit', '')):
            raise ValueError('Linux component inputs do not match this source')
        matching_input(directory, target, manifest.get('browser_input_sha256'), digest,
                       manifest['taskarian_commit'], legacy_android_module)
        verify_linux_engine(directory, target, manifest)


def verify_package(directory, target, source, digest):
    directory = Path(directory)
    packages = list(directory.glob('*.deb'))
    if len(packages) != 1:
        raise ValueError('Expected one complete Linux package')
    package = packages[0]
    manifest = json.loads(package.with_name(package.name + '.json').read_text())
    if manifest.get('revision') != source or manifest.get('architecture') != target.removeprefix('linux-'):
        raise ValueError('Package source or architecture mismatch')
    if manifest.get('components', {}).get('browser', {}).get('browser_input_sha256') != digest:
        raise ValueError('Package browser inputs do not match this source')
    with package.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest.get('sha256'):
            raise ValueError('Package checksum mismatch')


@cache
def workflow_steps(repository, revision, job_name):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Invalid builder workflow revision')
    path = '.github/workflows/build.yml'
    entry = api(f'repos/{repository}/contents/{path}?ref={revision}')
    source = base64.b64decode(entry['content']).decode()
    jobs = source.partition('\njobs:\n')[2]
    # The Android recipe has one job and ordinary top-level steps. Numbered
    # Actions results start with setup-job (1), then these steps (2 onwards).
    # Resolve commands from the immutable recipe: API display names can be
    # secret-redacted, including the word Android, and are not identities.
    if re.findall(r'^  ([\w-]+):$', jobs, re.M) != [job_name]:
        return None
    steps = re.split(r'^    - ', jobs.partition('    steps:\n')[2], flags=re.M)[1:]
    if not steps:
        return None
    return steps, {'revision': revision, 'path': path, 'blob': entry['sha']}


def completed_android_producer(repository, run, jobs):
    recipe = workflow_steps(repository, run['head_sha'], 'apk')
    if recipe is None or len(jobs) != 1:
        return None
    steps, workflow = recipe

    def command_number(command):
        matches = [index + 2 for index, step in enumerate(steps)
                   if re.search(r'^      run: ' + re.escape(command) + r'$', step, re.M)]
        return matches[0] if len(matches) == 1 else None

    build_numbers = [command_number(command) for command in (
        'set -o pipefail; ./mach build 2>&1 | tee ../artifacts/gecko-build.log',
        'set -o pipefail; ./mach gradle :fenix:assembleRelease --profile 2>&1 | tee ../artifacts/apk-build.log',
        'python3 browser/taskarian/android/scripts/collect-artifacts.py candidate')]
    verify_number = command_number('python3 agent/packaging/browser-component.py verify "$BROWSER_TARGET" --directory candidate')
    restore_number = command_number('python3 agent/packaging/browser-component.py restore "$BROWSER_TARGET" --directory candidate')
    if not verify_number or not all(build_numbers) or max(build_numbers) >= verify_number:
        return None
    upload_index = verify_number - 1  # zero-based recipe step immediately after verification
    if upload_index >= len(steps):
        return None
    upload_recipe = steps[upload_index]
    if (not re.match(r'uses: actions/upload-artifact@[0-9a-f]{40}\n', upload_recipe) or
            not re.search(r'^        path: candidate/$', upload_recipe, re.M) or
            not re.search(r'^        name: \$\{\{ steps\.component\.outputs\.artifact \}\}$', upload_recipe, re.M)):
        return None
    job = jobs[0]
    results = {step['number']: step for step in job.get('steps', [])}
    builds = [results.get(number, {}) for number in build_numbers]
    verification = results.get(verify_number, {})
    upload = results.get(verify_number + 1, {})
    if verification.get('conclusion') != 'success' or upload.get('conclusion') != 'success':
        return None
    evidence = {'job': job['id'], 'workflow': workflow, 'verify': verification, 'upload': upload}
    if all(step.get('conclusion') == 'success' for step in builds):
        return {**evidence, 'build': builds}
    restored = results.get(restore_number, {})
    if (restored.get('conclusion') == 'success' and
            all(step.get('conclusion') == 'skipped' for step in builds)):
        return {**evidence, 'restored': restored}
    return None


def producer_attempts(run):
    return ({**run, 'run_attempt': attempt} for attempt in range(run['run_attempt'], 0, -1))


def upload_matches(evidence, artifact):
    if artifact is None:
        return True
    upload = evidence['upload']
    parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parse(upload['started_at']) <= parse(artifact['created_at']) <= parse(upload['completed_at'])


def completed_producer(repository, run, target, artifact=None):
    for attempt in producer_attempts(run):
        evidence = producer_attempt(repository, attempt, target)
        if evidence and upload_matches(evidence, artifact):
            return {**evidence, 'attempt': attempt['run_attempt']}
    return None


def producer_attempt(repository, run, target):
    # Artifact upload is the publication boundary. Later assembly, cancellation,
    # or an unrelated target does not invalidate successful native proof.
    if (run['event'] != 'workflow_dispatch' or run['head_branch'] != 'main' or
            run['path'] != '.github/workflows/build.yml'):
        return None
    run_id, attempt = run.get('id'), run.get('run_attempt')
    if type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1:
        raise ValueError('Invalid browser producer run or attempt identity')
    jobs = api(f'repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')['jobs']
    for job in jobs:
        if (type(job.get('run_id')) is not int or type(job.get('run_attempt')) is not int or
                job['run_id'] != run_id or job['run_attempt'] != attempt):
            raise ValueError('Browser producer job belongs to another run or attempt')
    if target.startswith('android'):
        return completed_android_producer(repository, run, jobs)
    recipe = workflow_steps(repository, run['head_sha'], 'browser')
    source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
    if recipe is None or source is None:
        return None
    immutable = base64.b64decode(api(f'repos/{repository}/contents/.github/workflows/build.yml?ref={run["head_sha"]}')['content']).decode()
    ensure_source(source[1])
    original = network_command(['git', 'show', source[1] + ':.github/builders/linux.yml'])
    if linux_native_recipe(immutable) != linux_native_recipe(original):
        return None
    recipes, workflow = recipe
    def numbered(name):
        matches = [index + 2 for index, step in enumerate(recipes) if step.startswith('name: ' + name + '\n')]
        return matches[0] if len(matches) == 1 else None
    build_number = numbered('Build the complete browser archive')
    verify_number = numbered('Verify the browser archive and source inputs')
    if not build_number or not verify_number or build_number >= verify_number:
        return None
    upload_recipe = recipes[verify_number - 1]
    if (not re.match(r'uses: actions/upload-artifact@[0-9a-f]{40}\n', upload_recipe) or
            not re.search(r'^        path: \$\{\{ runner\.temp \}\}/browser/stage/$', upload_recipe, re.M) or
            not re.search(r'^        name: \$\{\{ steps\.component\.outputs\.artifact \}\}$', upload_recipe, re.M)):
        return None
    for job in jobs:
        steps = job.get('steps', [])
        verification = next((s for s in steps if s['number'] == verify_number and s['conclusion'] == 'success'), None)
        if not verification:
            continue
        upload = next((s for s in steps if s['number'] == verification['number'] + 1 and
                       'actions/upload-artifact@' in s['name'] and s['conclusion'] == 'success'), None)
        builds = [next((s for s in steps if s['number'] == build_number), {})]
        if not upload:
            continue
        if all(s.get('conclusion') == 'success' for s in builds):
            return {'job': job['id'], 'workflow': workflow, 'build': builds, 'verify': verification, 'upload': upload}
        # A successful workflow may itself have restored a verified producer.
        # A failed workflow is reusable only after an actual successful build.
        restored = next((s for s in steps if s['name'].startswith('Restore a matching completed ') and
                         s['conclusion'] == 'success'), None)
        if restored and all(s.get('conclusion') == 'skipped' for s in builds):
            return {'job': job['id'], 'workflow': workflow, 'restored': restored, 'verify': verification, 'upload': upload}
    return None


def source_blob(source, path):
    if not re.fullmatch('[0-9a-f]{40}', source):
        raise ValueError('Invalid producer source revision')
    local = subprocess.run(['git', 'rev-parse', '--verify', source + ':' + path], cwd=ROOT,
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if local.returncode == 0:
        return local.stdout.strip()
    return api(f'repos/openresearchtools/Taskarian/contents/{path}?ref={source}')['sha']


def restore_available(directory, target, digest):
    repository = os.environ['GITHUB_REPOSITORY']
    expected = 'openresearchtools/taskarian-build-' + target.removeprefix('linux-')
    if repository != expected:
        raise ValueError('Component restoration must run in the matching build repository')
    name = artifact_name(target, digest)
    page = 0
    while True:
        query = f'name={name}&per_page=100' if page == 0 else f'per_page=100&page={page}'
        artifacts = api(f'repos/{repository}/actions/artifacts?{query}')['artifacts']
        for artifact in artifacts:
            if artifact['expired'] or not artifact['name'].startswith(f'taskarian-browser-{target}-'):
                continue
            try:
                run = api(f'repos/{repository}/actions/runs/{artifact["workflow_run"]["id"]}')
                if target.startswith('android'):
                    # The APK carries exact companion package expectations. A
                    # different assembly revision needs new APK resources, even
                    # when unchanged Gecko objects remain in the compiler cache.
                    source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
                    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
                    if not source or source[1] != current:
                        continue
                evidence = completed_producer(repository, run, target, artifact)
                if not evidence:
                    continue
                source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
                if not source or not equivalent_source(source[1], target):
                    continue
                legacy = legacy_module = legacy_workflow = legacy_fixtures = None
                expected = digest
                if artifact['name'] != name:
                    source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
                    if not source:
                        continue
                    verifier = source_blob(source[1], VERIFIER)
                    candidates = [(verifier, None, None, None)]
                    if not target.startswith('android'):
                        # This exact module is traversed only by mobile Android/iOS
                        # builds. Prove all remaining Linux inputs still match;
                        # retain the original source/blob in the equivalence receipt.
                        module = source_blob(source[1], ANDROID_ONLY_MODULE)
                        workflow = source_blob(source[1], '.github/builders/linux.yml')
                        fixtures = {path: source_blob(source[1], path) for path in FIXTURE_METADATA}
                        candidates += [(v, m, w, f) for v in (None, verifier)
                                       for m in (None, module) for w in (None, workflow) for f in (None, fixtures)]
                    for legacy, legacy_module, legacy_workflow, legacy_fixtures in candidates:
                        expected = fingerprint(target, legacy, legacy_module, legacy_workflow, legacy_fixtures)
                        if artifact['name'] == artifact_name(target, expected):
                            break
                    else:
                        continue
                # Never leave a partial/corrupt optional cache in the build output.
                with tempfile.TemporaryDirectory(prefix='browser-restore-', dir=Path(directory).parent) as temporary:
                    try:
                        download(repository, run['id'], artifact['name'], temporary)
                        verify(temporary, target, expected, legacy_module)
                        for file in Path(temporary).iterdir():
                            shutil.move(str(file), Path(directory) / file.name)
                    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
                        print(f'Skipping unusable browser cache {artifact["id"]}: {error}', flush=True)
                        continue
                if expected != digest or not target.startswith('android'):
                    manifest_file = Path(directory) / ('build-manifest.json' if target.startswith('android') else 'browser-artifact-manifest.txt')
                    manifest = json.loads(manifest_file.read_text()) if target.startswith('android') else dict(line.split('=', 1) for line in manifest_file.read_text().splitlines())
                    producer = manifest['source' if target.startswith('android') else 'taskarian_commit']
                    receipt_file = Path(directory) / EQUIVALENCE
                    if receipt_file.exists():
                        # verify() already checked the previous receipt and complete
                        # artifact. Preserve the original producer through reuse.
                        receipt = json.loads(receipt_file.read_text())
                        receipt['compileFingerprint'] = digest
                    else:
                        if legacy and source_blob(producer, VERIFIER) != legacy:
                            raise ValueError('Producer verifier differs from the proven legacy input')
                        receipt = {'schema': 1, 'target': target, 'producerSource': producer,
                                   'producerFingerprint': expected, 'compileFingerprint': digest,
                                   'legacyVerifierBlob': legacy, 'repository': repository, 'run': run['id'],
                                   'artifact': artifact['id'], 'producerSteps': evidence,
                                   'producerManifestSha256': hashlib.sha256(manifest_file.read_bytes()).hexdigest()}
                    if legacy_module:
                        receipt['legacyAndroidModuleBlob'] = source_blob(producer, ANDROID_ONLY_MODULE)
                    if legacy_workflow:
                        receipt['legacyLinuxWorkflowBlob'] = source_blob(producer, '.github/builders/linux.yml')
                    receipt['legacyFixtureLockBlobs'] = {path: source_blob(producer, path) for path in FIXTURE_METADATA}
                    receipt['restoredFrom'] = {'repository': repository, 'run': run['id'],
                        'source': source[1], 'artifact': artifact['id'], 'steps': evidence,
                        'sha256': {line.split('  ', 1)[1]: line.split('  ', 1)[0]
                                   for line in (Path(directory) / 'SHA256SUMS').read_text().splitlines()
                                   if not line.endswith('  ' + EQUIVALENCE)}}
                    receipt_file.write_text(json.dumps(receipt, indent=2) + '\n')
                    checksums = Path(directory) / 'SHA256SUMS'
                    lines = [line for line in checksums.read_text().splitlines()
                             if not line.endswith('  ' + EQUIVALENCE)]
                    lines.append(hashlib.sha256(receipt_file.read_bytes()).hexdigest() + '  ' + EQUIVALENCE)
                    checksums.write_text('\n'.join(lines) + '\n')
                    verify(directory, target, digest)
                print(f'Reused verified {target} browser from {run["html_url"]}', flush=True)
                return True
            except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
                for file in Path(directory).iterdir():
                    shutil.rmtree(file) if file.is_dir() else file.unlink()
                print(f'Skipping unprovable browser cache {artifact["id"]}: {error}', flush=True)
                continue
        if page == 0:
            page = 1
            continue
        if len(artifacts) < 100:
            return False
        page += 1


def restore(directory, target, digest):
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'openresearchtools/taskarian-build-' + target.removeprefix('linux-'):
        raise ValueError('Browser restoration must use the matching architecture builder')
    if not browser_release().engine_available(target):
        return False
    return browser_release().restore_resources(directory, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['fingerprint', 'restore', 'verify', 'restore-native', 'record-native'])
    parser.add_argument('target', choices=['linux-arm64', 'linux-amd64', 'android', 'android-x86_64'])
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--force-build', action='store_true', help='Bypass optional artifact restoration')
    args = parser.parse_args()
    if args.action in ('restore-native', 'record-native'):
        if args.target.startswith('android') or args.directory is None:
            parser.error('Linux native actions require --directory and a Linux target')
        args.directory.mkdir(parents=True, exist_ok=True)
        if args.action == 'record-native':
            record_native(args.directory, args.target)
            return
        if any(args.directory.iterdir()):
            raise ValueError('Native restore directory must be empty')
        source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        native_digest = linux_engine_inputs(source, args.target)
        forced = args.force_build or os.environ.get('TASKARIAN_FORCE_FULL_BUILD') == '1'
        if forced:
            with Path(os.environ['GITHUB_ENV']).open('a') as environment:
                environment.write('MOZ_ARTIFACT_FILE=\nTASKARIAN_LINUX_NATIVE_RECEIPT=\nTASKARIAN_LINUX_XPT_ARTIFACT=\n')
        hit = False if forced else restore_native(args.directory.resolve(), args.target)
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write(f'hit={str(hit).lower()}\nnative_digest={native_digest}\n')
        return
    digest = fingerprint(args.target)
    name = artifact_name(args.target, digest)
    if args.action == 'fingerprint':
        print(digest)
        return
    if args.directory is None:
        parser.error('--directory is required')
    if args.action == 'verify':
        verify(args.directory, args.target, digest)
        return
    args.directory.mkdir(parents=True, exist_ok=True)
    if any(args.directory.iterdir()):
        raise ValueError('Restore destination must be empty')
    try:
        hit = False if args.force_build or os.environ.get('TASKARIAN_FORCE_FULL_BUILD') == '1' else restore(args.directory, args.target, digest)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        for file in args.directory.iterdir():
            shutil.rmtree(file) if file.is_dir() else file.unlink()
        print(f'Optional browser restoration unavailable: {error}; using a normal build.', flush=True)
        hit = False
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'hit={str(hit).lower()}\ndigest={digest}\nartifact={name}\n')
    print('Matching verified browser artifact found' if hit else 'No matching completed browser resources found')


if __name__ == '__main__':
    main()
