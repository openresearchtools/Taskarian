#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Reuse unchanged Gecko libraries through Mozilla's Android artifact build."""
import argparse
import base64
from functools import cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = '.github/builders/android.yml'
# These application files do not generate Gecko JNI wrappers. In particular,
# GeckoView Java, build definitions, native sources and version pins stay inputs.
APPLICATION_PATHS = (
    'browser/mobile/android/taskarian/src/main/java/',
    'browser/mobile/android/taskarian/src/main/assets/',
    'browser/mobile/android/taskarian/src/main/res/',
    'browser/mobile/android/fenix/app/src/main/java/',
    'browser/mobile/android/fenix/app/src/main/assets/',
    'browser/mobile/android/fenix/app/src/main/res/',
)
COLLECTOR = 'browser/taskarian/android/scripts/collect-artifacts.py'
NATIVE_LEAF_PATHS = (
    'browser/third_party/tor/',
    'browser/taskarian/third_party/tor-notices/',
    'browser/taskarian/display/',
    'browser/mobile/android/taskarian/display/',
    'browser/taskarian/vendor/termux-x11/',
    'browser/taskarian/vendor/display-dependencies/',
    'browser/taskarian/android/tor/',
    'browser/taskarian/android/notices/',
    'browser/taskarian/android/sources/',
)
spec = importlib.util.spec_from_file_location('browser_component', ROOT / 'agent/packaging/browser-component.py')
component = importlib.util.module_from_spec(spec)
spec.loader.exec_module(component)


def git(*args):
    if args and args[0] in ('show', 'ls-tree'):
        return component.network_command(['git', *args], binary=True)
    return subprocess.check_output(['git', *args], cwd=ROOT)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def native_recipe(workflow, legacy_cache_environment=False):
    """Compare the actual compiler environment and commands, not cache policy."""
    runner = re.search(r'^    runs-on: (.+)$', workflow, re.M)
    environment = re.search(r'^    env:\n((?:      .+\n)+)', workflow, re.M)
    if not runner or not environment:
        raise ValueError('Android native recipe is missing its runner or environment')
    recipe = {'runner': runner[1], 'environment': [line for line in environment[1].splitlines()
              if not re.match(r'      (?:#|ANDROID_KEY|MOZ_SOURCE_CHANGESET:)', line)
              and (legacy_cache_environment or not re.match(r'      (?:SCCACHE_[A-Z_]+|GH_TOKEN):', line))]}
    for name in ('Select the Android architecture', 'Prepare hosted runner', 'Install the compatible Rust toolchain',
                 'Bootstrap the pinned Mozilla Android toolchain', 'Generate the product artwork',
                 'Build Gecko for the selected architecture'):
        block = re.search(r'^    - name: ' + re.escape(name) + r'\n((?:      .*\n|\n)+)', workflow, re.M)
        if not block:
            raise ValueError('Android native recipe is missing ' + name)
        # Conditions and timing do not change the compilation. Preserve commands,
        # including multiline shell, the working directory and step environment.
        recipe[name] = [line for line in block[1].splitlines()
                        if not re.match(r'      (?:if|timeout-minutes):', line)]
    for action in (('mozilla-actions/sccache-action', 'actions/setup-java') if legacy_cache_environment
                   else ('actions/setup-java',)):
        block = re.search(r'^    - uses: ' + re.escape(action) + r'@(.+\n)((?:      .*\n|\n)*)', workflow, re.M)
        if not block:
            raise ValueError('Android native recipe is missing ' + action)
        recipe[action] = block[1] + '\n'.join(line for line in block[2].splitlines()
                                             if not re.match(r'      if:', line))
    return recipe


@cache
def historical_native_recipe(revision):
    # Read the small immutable recipe before considering a product-tree fetch.
    # Older trees can contain source archives removed from current history.
    record = component.api(f'repos/openresearchtools/Taskarian/contents/{WORKFLOW}?ref={revision}')
    return native_recipe(base64.b64decode(record['content']).decode())


@cache
def native_tree(revision):
    component.ensure_source(revision)
    return git('ls-tree', '-r', '-z', revision, '--', 'browser')


@cache
def source_finalizer_base(payload):
    """Source-retention finalization does not compile Gecko or generate JNI."""
    if b'TASKARIAN_ANDROID_LIBRARY_SOURCE_REFERENCE' not in payload:
        return payload
    path = 'browser/mobile/android/fenix/app/build.gradle'
    baseline = '522a9d8a4e24ae2fdd0b76fd2cc9f98af2ba7190'
    component.ensure_source(baseline)
    original = git('show', baseline + ':' + path)
    start = b"tasks.register('stageTaskarianDependencySources') {\n"
    end = b"tasks.matching { it.name == 'assembleRelease' }.configureEach {\n    finalizedBy 'stageTaskarianDependencySources'\n}"
    if payload.count(start) != 1 or payload.count(end) != 1 or original.count(start) != 1 or original.count(end) != 1:
        raise ValueError('Android source finalizer boundaries differ; retain native inputs for review')
    first, last = payload.index(start), payload.index(end) + len(end)
    original_first, original_last = original.index(start), original.index(end) + len(end)
    return payload[:first] + original[original_first:original_last] + payload[last:]


def native_inputs(revision, target, legacy_android_module=False,
                  legacy_fixture_metadata=False, legacy_desktop_resources=False,
                  legacy_artifact_policy=False, legacy_resource_scope=False):
    if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Invalid native source revision')
    if subprocess.run(['git', 'cat-file', '-e', revision + '^{commit}'], cwd=ROOT,
                      stderr=subprocess.DEVNULL).returncode:
        # Checkout is shallow in CI. Fetch only this immutable product commit,
        # sharing the current tree's blobs; never fetch Mozilla ancestry or tags.
        component.network_command(['git', 'fetch', '--filter=blob:none', '--no-tags', '--depth=1', 'origin', revision])
    entries = []
    resources = component.packaged_resources(revision) if not legacy_resource_scope else frozenset()
    for entry in native_tree(revision).split(b'\0'):
        if not entry:
            continue
        path = entry.split(b'\t', 1)[1].decode()
        # This exact EXTRA_JS_MODULES input is repackaged into omni.ja. Keep its
        # moz.build and all other shared files in the native input comparison.
        if path == component.ANDROID_ONLY_MODULE and not legacy_android_module:
            continue
        if path in component.FIXTURE_METADATA and not legacy_fixture_metadata:
            continue
        if path in component.ANDROID_DESKTOP_RESOURCES and not legacy_desktop_resources:
            continue
        if not legacy_resource_scope and (path in resources or
                path in (component.PRODUCT_VERSION, *component.RESOURCE_GENERATORS) or
                path.startswith(NATIVE_LEAF_PATHS)):
            continue
        if path == COLLECTOR or path.startswith(APPLICATION_PATHS) or '/__pycache__/' in path:
            continue
        if path in component.NATIVE_POLICY_FILES and not legacy_artifact_policy:
            # These exact delimited additions only assemble resources or select
            # a prebuilt Java/AAR consumer; native producers do not enable them.
            # Preserve every native recipe byte outside the proven blocks.
            header, raw_path = entry.split(b'\t', 1)
            mode, kind, _identity = header.split()
            identity = component.blob_identity(component.artifact_policy_base(git('show', revision + ':' + path)))
            entry = mode + b' ' + kind + b' ' + identity.encode() + b'\t' + raw_path
        if path == 'browser/mobile/android/fenix/app/build.gradle':
            header, raw_path = entry.split(b'\t', 1)
            mode, kind, _identity = header.split()
            identity = component.blob_identity(source_finalizer_base(git('show', revision + ':' + path)))
            entry = mode + b' ' + kind + b' ' + identity.encode() + b'\t' + raw_path
        entries.append(entry)
    recipe = native_recipe(git('show', revision + ':' + WORKFLOW).decode(), legacy_resource_scope)
    return hashlib.sha256(target.encode() + b'\0' + b'\0'.join(entries) + b'\0' +
                          json.dumps(recipe, sort_keys=True).encode()).hexdigest()


def gecko_libraries(apk, abi):
    # Use the existing Gecko package manifest, not all JNI libraries in Fenix.
    # Go, Tor, Display and Maven native dependencies remain freshly assembled by
    # their existing Gradle/build owners and never enter the reuse archive.
    manifest = (ROOT / 'browser/mobile/android/installer/package-manifest.in').read_text()
    names = {'lib' + name + '.so' for name in re.findall(r'@DLL_PREFIX@([\w-]+)@DLL_SUFFIX@', manifest)}
    names.add('libplugin-container.so')
    # This mozconfig uses the pinned Maven app-services library, not the optional
    # --enable-appservices-in-tree Gecko build mentioned in package-manifest.in.
    names.discard('libmegazord.so')
    libraries = {}
    with zipfile.ZipFile(apk) as archive:
        for name in archive.namelist():
            if name.startswith(f'lib/{abi}/') and Path(name).name in names:
                with archive.open(name) as stream:
                    libraries[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if f'lib/{abi}/libxul.so' not in libraries or f'lib/{abi}/libmozglue.so' not in libraries:
        raise ValueError('Android artifact is missing its Gecko libraries')
    return libraries


def verify_native(apk, record, target):
    if not isinstance(record, dict) or not isinstance(record.get('libraries'), dict):
        raise ValueError('Native Gecko receipt must contain its library inventory')
    abi = 'x86_64' if target == 'android-x86_64' else 'arm64-v8a'
    current = git('rev-parse', 'HEAD').decode().strip()
    producer_inputs = native_inputs(record.get('source', ''), target)
    # Preserve old receipts, including their original digest. Validate that
    # digest under its actual producer's old/new calculation before comparing
    # current native inputs with the producer under the narrow resource rule.
    recorded_inputs = {native_inputs(record.get('source', ''), target, module, fixtures, desktop, policy, resources)
                       for module in (False, True) for fixtures in (False, True)
                       for desktop in (False, True) for policy in (False, True) for resources in (False, True)}
    if (record.get('schema') != 1 or record.get('architecture') != abi or
            record.get('input_sha256') not in recorded_inputs or
            producer_inputs != native_inputs(current, target) or
            record.get('libraries') != gecko_libraries(apk, abi)):
        raise ValueError('Gecko native producer, inputs or library bytes do not match')
    producer = record.get('producer')
    if producer:
        if not isinstance(producer, dict):
            raise ValueError('Invalid Gecko producer evidence')
        release = producer.get('release') if producer.get('kind') == 'component-release' else None
        if release and not isinstance(release, dict):
            raise ValueError('Invalid Gecko component release reference')
        if (producer.get('source') != record['source'] or
                producer.get('repository') != 'openresearchtools/taskarian-build-' + target or
                not re.fullmatch('[0-9]+', str(producer.get('run', ''))) or
                (not release and not re.fullmatch('[0-9]+', str(producer.get('artifact', '')))) or
                (release and (release.get('repository') != producer['repository'] or
                    not re.fullmatch('[0-9a-f]{64}', release.get('manifestSha256', '')))) or
                not all(re.fullmatch('[0-9a-f]{64}', producer.get(key, ''))
                        for key in ('manifest_sha256', 'apk_sha256'))):
            raise ValueError('Invalid original Gecko producer evidence')
    elif record['source'] != current:
        raise ValueError('Reused Gecko libraries require their original producer evidence')


def record_build(apk, target):
    abi = 'x86_64' if target == 'android-x86_64' else 'arm64-v8a'
    current = git('rev-parse', 'HEAD').decode().strip()
    component.verify_android_resources(apk)
    receipt = os.environ.get('TASKARIAN_ANDROID_NATIVE_RECEIPT') if os.environ.get('TASKARIAN_FORCE_FULL_BUILD') != '1' else None
    if receipt:
        record = json.loads(Path(receipt).read_text())
        verify_native(apk, record, target)
        return record
    return {'schema': 1, 'architecture': abi, 'source': current,
            'input_sha256': native_inputs(current, target), 'libraries': gecko_libraries(apk, abi)}


def artifact_mozconfig():
    # Artifact mode disables toolchain.configure, so its compiler-only options
    # are unavailable. Keep every assembly option and the native input file
    # unchanged; shell no-ops preserve conditional branches such as SCCACHE_PATH.
    source = (ROOT / 'browser/taskarian/android/mozconfig').read_text()
    source = re.sub(r'^([ \t]*)ac_add_options --(?:(?:enable|disable)-(?:optimize|debug-symbols)|'
                    r'(?:with|without)-ccache)(?:=.*)?$',
                    r'\1: # Native compilation option; retained in the producer configuration.',
                    source, flags=re.M)
    # Full native builds produce these host generators. Artifact builds obtain
    # them from the same source-hashed Mozilla toolchain definitions instead.
    return (source + '\nac_add_options --enable-artifact-builds\n'
            'ac_add_options --enable-bootstrap=embedded-uniffi-bindgen,nimbus-fml\n')


def native_artifact(target, digest):
    return f'taskarian-gecko-{target}-{digest}'


def verify_libraries(payload, record, target):
    verify_native(payload, record, target)
    machine = 62 if target == 'android-x86_64' else 183
    with zipfile.ZipFile(payload) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(record['libraries']):
            raise ValueError('Native Gecko archive must contain exactly the recorded libraries')
        for name in record['libraries']:
            data = archive.read(name)
            if data[:6] != b'\x7fELF\x02\x01' or len(data) < 64 or struct.unpack_from('<H', data, 18)[0] != machine:
                raise ValueError('Native Gecko library architecture differs: ' + name)
            offset = struct.unpack_from('<Q', data, 32)[0]
            size, count = struct.unpack_from('<HH', data, 54)
            if size < 56 or offset + size * count > len(data):
                raise ValueError('Invalid Gecko ELF program headers')
            loads = 0
            for index in range(count):
                segment = offset + index * size
                if struct.unpack_from('<I', data, segment)[0] != 1:
                    continue
                position, address = struct.unpack_from('<QQ', data, segment + 8)
                if struct.unpack_from('<Q', data, segment + 48)[0] < 16384 or position % 16384 != address % 16384:
                    raise ValueError('Native Gecko library is not 16 KB aligned: ' + name)
                loads += 1
            if not loads:
                raise ValueError('Native Gecko library has no load segments: ' + name)


def publish(directory, target):
    abi = 'x86_64' if target == 'android-x86_64' else 'arm64-v8a'
    source = git('rev-parse', 'HEAD').decode().strip()
    stage = Path(os.environ['TASKARIAN_ANDROID_OBJDIR']) / 'dist/geckoview/lib' / abi
    payload = directory / 'gecko-libraries.apk'
    manifest = (ROOT / 'browser/mobile/android/installer/package-manifest.in').read_text()
    names = {'lib' + name + '.so' for name in re.findall(r'@DLL_PREFIX@([\w-]+)@DLL_SUFFIX@', manifest)}
    names.add('libplugin-container.so')
    names.discard('libmegazord.so')
    with zipfile.ZipFile(payload, 'w') as archive:
        for file in sorted(stage.iterdir()):
            if file.name in names and file.is_file():
                archive.write(file, f'lib/{abi}/{file.name}')
    receipt = os.environ.get('TASKARIAN_ANDROID_NATIVE_RECEIPT') if os.environ.get('TASKARIAN_FORCE_FULL_BUILD') != '1' else None
    record = json.loads(Path(receipt).read_text()) if receipt else {
        'schema': 1, 'architecture': abi, 'source': source,
        'input_sha256': native_inputs(source, target), 'libraries': gecko_libraries(payload, abi)}
    verify_libraries(payload, record, target)
    record['archiveSource'] = source
    (directory / 'native-gecko.json').write_text(json.dumps(record, indent=2) + '\n')
    # Keep the actual built page with the checkpoint, so an engine-only owner
    # can clobber object intermediates before archiving its compiler cache.
    count = 0
    for omni in (Path(os.environ['TASKARIAN_ANDROID_OBJDIR']) / 'dist').rglob('omni.ja'):
        with zipfile.ZipFile(omni) as archive:
            for name in archive.namelist():
                if name.endswith('/license.html'):
                    (directory / f'gecko-built-license-{count}.html').write_bytes(archive.read(name))
                    count += 1
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('license_captured=' + str(bool(count)).lower() + '\n')
    (directory / 'SHA256SUMS').write_text(''.join(sha(path) + '  ' + path.name + '\n'
        for path in sorted(directory.iterdir()) if path.is_file() and path.name != 'SHA256SUMS'))
    component.verify_checksums(directory)


def early_producer(repository, run, artifact=None):
    for attempt in component.producer_attempts(run):
        evidence = early_attempt(repository, attempt)
        if evidence and component.upload_matches(evidence, artifact):
            return {**evidence, 'attempt': attempt['run_attempt']}
    return None


def seed_checkpoint(directory, target, producer_source, producer_run, producer_artifact):
    """Verify one explicitly identified native upload; never search old runs."""
    repository = 'openresearchtools/taskarian-build-' + target
    if target not in ('android', 'android-x86_64') or os.environ.get('GITHUB_REPOSITORY') != repository:
        raise ValueError('Seed the checkpoint in its original Android architecture builder')
    if not re.fullmatch('[0-9a-f]{40}', producer_source) or min(producer_run, producer_artifact) < 1:
        raise ValueError('An exact native source/run/artifact is required')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Native checkpoint seed directory must be empty')
    artifact = component.api(f'repos/{repository}/actions/artifacts/{producer_artifact}')
    run = component.api(f'repos/{repository}/actions/runs/{producer_run}')
    digest = native_inputs(producer_source, target)
    evidence = early_producer(repository, run, artifact)
    if (artifact.get('id') != producer_artifact or artifact.get('expired') or
            artifact.get('workflow_run', {}).get('id') != producer_run or
            artifact.get('name') != native_artifact(target, digest) or
            not run.get('display_title', '').startswith('Taskarian ' + producer_source + ' / ') or
            not evidence or digest != native_inputs(git('rev-parse', 'HEAD').decode().strip(), target) or
            not re.fullmatch('sha256:[0-9a-f]{64}', artifact.get('digest', ''))):
        raise ValueError('Explicit native upload, original workflow evidence or compiler inputs differ')
    with tempfile.TemporaryFile() as stream:
        for attempt in range(3):
            stream.seek(0); stream.truncate()
            result = subprocess.run(['gh', 'api', f'repos/{repository}/actions/artifacts/{producer_artifact}/zip'],
                                    stdout=stream, stderr=subprocess.PIPE)
            if result.returncode == 0:
                break
            if attempt == 2:
                raise component.NetworkUnavailable('Explicit native checkpoint download unavailable; retry without compiling')
            time.sleep(2 ** attempt)
        stream.seek(0)
        if (os.fstat(stream.fileno()).st_size != artifact['size_in_bytes'] or
                'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest() != artifact['digest']):
            raise ValueError('Explicit uploaded native checkpoint checksum or size differs')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            names = archive.namelist()
            if (len(names) != len(set(names)) or not {'SHA256SUMS', 'native-gecko.json', 'gecko-libraries.apk'} <= set(names) or
                    any(name not in ('SHA256SUMS', 'native-gecko.json', 'gecko-libraries.apk') and
                        not re.fullmatch(r'gecko-built-license-[0-9]+\.html', name) for name in names) or
                    any((item.external_attr >> 16) & 0o170000 == 0o120000 for item in archive.infolist())):
                raise ValueError('Unexpected native checkpoint members')
            archive.extractall(directory)
    component.verify_checksums(directory)
    manifest, payload = directory / 'native-gecko.json', directory / 'gecko-libraries.apk'
    record = json.loads(manifest.read_text())
    if (record.get('source') != producer_source or record.get('archiveSource') != producer_source or
            record.get('input_sha256') != digest or record.get('producer') or
            not any(page.stat().st_size for page in directory.glob('gecko-built-license-*.html'))):
        raise ValueError('Original native source, complete built legal page or native input record differs')
    proof = {'source': producer_source, 'runSource': producer_source,
             'repository': repository, 'run': producer_run, 'artifact': producer_artifact,
             'artifactName': artifact['name'], 'artifactSha256': artifact['digest'].removeprefix('sha256:'),
             'manifest_sha256': sha(manifest), 'apk': payload.name, 'apk_sha256': sha(payload), 'steps': evidence}
    record['producer'] = proof
    verify_libraries(payload, record, target)
    # Keep every uploaded checkpoint byte unchanged. The outside receipt binds
    # the original native producer to the current source-equivalent publisher.
    receipt = directory.parent / 'seed-native-gecko.json'
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    os.environ['TASKARIAN_ANDROID_NATIVE_RECEIPT'] = str(receipt.resolve())
    return receipt


def early_attempt(repository, run):
    if run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/build.yml':
        return None
    recipe = component.workflow_steps(repository, run['head_sha'], 'apk')
    if recipe is None:
        return None
    source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
    if not source:
        return None
    component.ensure_source(source[1])
    immutable = base64.b64decode(component.api(f'repos/{repository}/contents/.github/workflows/build.yml?ref={run["head_sha"]}')['content']).decode()
    if native_recipe(immutable) != native_recipe(git('show', source[1] + ':' + WORKFLOW).decode()):
        return None
    steps, workflow = recipe
    numbers = {}
    for name in ('Build Gecko for the selected architecture', 'Stage and verify native Gecko libraries'):
        matches = [index + 2 for index, step in enumerate(steps) if step.startswith('name: ' + name + '\n')]
        if len(matches) != 1:
            return None
        numbers[name] = matches[0]
    stage = numbers['Stage and verify native Gecko libraries']
    if (stage <= numbers['Build Gecko for the selected architecture'] or
            '        ./mach build stage-package' not in steps[stage - 2] or
            'python3 ../agent/packaging/android-native-component.py "$BROWSER_TARGET" --publish --directory "$RUNNER_TEMP/android-gecko-artifact"' not in steps[stage - 2] or
            not re.match(r'uses: actions/upload-artifact@[0-9a-f]{40}\n', steps[stage - 1]) or
            'path: ${{ runner.temp }}/android-gecko-artifact/' not in steps[stage - 1] or
            'name: ${{ steps.native-component.outputs.artifact }}' not in steps[stage - 1]):
        return None
    jobs = component.api(f'repos/{repository}/actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs?per_page=100')['jobs']
    for job in jobs:
        if job.get('run_id') != run['id'] or job.get('run_attempt') != run['run_attempt']:
            raise ValueError('Native Gecko producer job belongs to another run or attempt')
        results = {step['number']: step for step in job.get('steps', [])}
        if all(results.get(number, {}).get('conclusion') == 'success' for number in (*numbers.values(), stage + 1)):
            return {'job': job['id'], 'workflow': workflow, 'build': results[numbers['Build Gecko for the selected architecture']],
                    'verify': results[stage], 'upload': results[stage + 1]}
    return None


def install_native(directory, payload, record):
    receipt = directory / 'native-gecko.json'
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    mozconfig = directory / 'mozconfig'
    mozconfig.write_text(artifact_mozconfig())
    with Path(os.environ['GITHUB_ENV']).open('a') as environment:
        environment.write(f'MOZCONFIG={mozconfig.resolve()}\nMOZ_ARTIFACT_FILE={payload.resolve()}\n'
                          f'TASKARIAN_ANDROID_NATIVE_RECEIPT={receipt.resolve()}\n')


def restore_early(directory, target):
    repository = os.environ['GITHUB_REPOSITORY']
    current = git('rev-parse', 'HEAD').decode().strip()
    digest = native_inputs(current, target)
    name = native_artifact(target, digest)
    page = 0
    while True:
        query = f'name={name}&per_page=100' if page == 0 else f'per_page=100&page={page}'
        artifacts = component.api(f'repos/{repository}/actions/artifacts?{query}')['artifacts']
        for artifact in artifacts:
            if artifact['expired'] or not artifact['name'].startswith(f'taskarian-gecko-{target}-'):
                continue
            try:
                run = component.api(f'repos/{repository}/actions/runs/{artifact["workflow_run"]["id"]}')
                source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
                evidence = early_producer(repository, run, artifact)
                if not source or not evidence or native_inputs(source[1], target) != digest:
                    continue
                accepted_names = {native_artifact(target, native_inputs(source[1], target, module, fixtures, desktop, policy, resources))
                                  for module in (False, True) for fixtures in (False, True)
                                  for desktop in (False, True) for policy in (False, True) for resources in (False, True)}
                if artifact['name'] not in accepted_names:
                    continue
                with tempfile.TemporaryDirectory(prefix='gecko-restore-', dir=directory.parent) as temporary:
                    producer = Path(temporary)
                    component.download(repository, run['id'], artifact['name'], producer)
                    component.verify_checksums(producer)
                    manifest = producer / 'native-gecko.json'
                    record = json.loads(manifest.read_text())
                    payload = producer / 'gecko-libraries.apk'
                    if record.get('archiveSource', record.get('source')) != source[1]:
                        raise ValueError('Native Gecko archive source differs from its producer')
                    proof = {'source': source[1], 'repository': repository, 'run': run['id'],
                             'artifact': artifact['id'], 'manifest_sha256': sha(manifest),
                             'apk': payload.name, 'apk_sha256': sha(payload), 'steps': evidence}
                    record.setdefault('producer', proof)
                    record['restored_from'] = proof
                    verify_libraries(payload, record, target)
                    destination = directory / payload.name
                    shutil.copyfile(payload, destination)
                    install_native(directory, destination, record)
                print(f'Reusing verified native Gecko upload immediately from {run["html_url"]}', flush=True)
                return True
            except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
                print(f'Skipping unusable native Gecko cache {artifact["id"]}: {error}', flush=True)
        if page == 0:
            page = 1
            continue
        if len(artifacts) < 100:
            return False
        page += 1


def restore_apk(directory, target):
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'openresearchtools/taskarian-build-' + target:
        raise ValueError('Native restoration must use its matching Android builder')
    current = git('rev-parse', 'HEAD').decode().strip()
    digest = native_inputs(current, target)
    current_recipe = native_recipe(git('show', current + ':' + WORKFLOW).decode())
    abi = 'x86_64' if target == 'android-x86_64' else 'arm64-v8a'
    page = 1
    while True:
        artifacts = component.api(f'repos/{repository}/actions/artifacts?per_page=100&page={page}')['artifacts']
        for artifact in artifacts:
            if artifact['expired'] or not artifact['name'].startswith(f'taskarian-browser-{target}-'):
                continue
            try:
                run = component.api(f'repos/{repository}/actions/runs/{artifact["workflow_run"]["id"]}')
                source = re.match(r'^Taskarian ([0-9a-f]{40}) / ', run.get('display_title', ''))
                if not source:
                    continue
                evidence = component.completed_producer(repository, run, target, artifact)
                if not evidence:
                    continue
                try:
                    if historical_native_recipe(source[1]) != current_recipe:
                        continue
                except subprocess.CalledProcessError as error:
                    try:
                        response = json.loads(error.output)
                    except (TypeError, ValueError):
                        raise error
                    if not isinstance(response, dict) or response.get('status') != '404':
                        raise
                    print(f'Skipping native Gecko cache producer whose source is unavailable: {source[1]}', flush=True)
                    continue
                except ValueError:
                    # An older unsupported native recipe cannot prove a match.
                    continue
                try:
                    producer_inputs = native_inputs(source[1], target)
                except ValueError:
                    # A historical, different native recipe is a cache miss.
                    continue
                if producer_inputs != digest:
                    continue
                producer = directory / 'producer'
                component.download(repository, run['id'], artifact['name'], producer)
                component.verify_checksums(producer)
                manifest_path = producer / 'build-manifest.json'
                manifest = json.loads(manifest_path.read_text())
                apks = list(producer.glob('*.apk'))
                if (manifest.get('source') != source[1] or manifest.get('architecture') != abi or
                        len(apks) != 1 or manifest.get('apks', {}).get(apks[0].name) != sha(apks[0])):
                    raise ValueError('Native producer APK source, architecture or checksum mismatch')
                apk = apks[0]
                proof = {'source': source[1], 'repository': repository, 'run': run['id'],
                         'artifact': artifact['id'], 'manifest_sha256': sha(manifest_path),
                         'apk': apk.name, 'apk_sha256': sha(apk), 'steps': evidence}
                record = manifest.get('native_gecko') or {
                    'schema': 1, 'architecture': abi, 'source': source[1], 'input_sha256': digest,
                    'libraries': gecko_libraries(apk, abi)}
                # Keep the first actual native producer through subsequent reuse.
                record.setdefault('producer', proof)
                record['restored_from'] = proof
                verify_native(apk, record, target)
                receipt = directory / 'native-gecko.json'
                receipt.write_text(json.dumps(record, indent=2) + '\n')
                payload = directory / 'gecko-libraries.apk'
                with zipfile.ZipFile(apk) as source_apk, zipfile.ZipFile(payload, 'w') as output:
                    for name in sorted(record['libraries']):
                        output.writestr(name, source_apk.read(name))
                # Mozilla's normal mach build installs this local archive and rebuilds
                # resources/Java. The normal mozconfig remains an immutable input.
                mozconfig = directory / 'mozconfig'
                mozconfig.write_text(artifact_mozconfig())
                with Path(os.environ['GITHUB_ENV']).open('a') as environment:
                    environment.write(f'MOZCONFIG={mozconfig.resolve()}\nMOZ_ARTIFACT_FILE={payload.resolve()}\n'
                                      f'TASKARIAN_ANDROID_NATIVE_RECEIPT={receipt.resolve()}\n')
                print(f'Reusing Gecko native producer {record["source"]} from {run["html_url"]}; '
                      'assembling current application resources and Java.', flush=True)
                return True
            except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
                for path in directory.iterdir():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                print(f'Skipping unprovable APK native cache {artifact["id"]}: {error}', flush=True)
                continue
        if len(artifacts) < 100:
            return False
        page += 1


def restore(directory, target):
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'openresearchtools/taskarian-build-' + target:
        raise ValueError('Native restoration must use its matching Android builder')
    return component.browser_release().restore_engine(directory, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['android', 'android-x86_64'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--publish', action='store_true', help='Verify and publish staged native Gecko libraries before APK assembly')
    parser.add_argument('--force-build', action='store_true', help='Bypass optional native restoration')
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if any(args.directory.iterdir()):
        raise ValueError('Native restore directory must be empty')
    if args.publish:
        publish(args.directory.resolve(), args.target)
        return
    digest = native_inputs(git('rev-parse', 'HEAD').decode().strip(), args.target)
    if args.force_build or os.environ.get('TASKARIAN_FORCE_FULL_BUILD') == '1':
        with Path(os.environ['GITHUB_ENV']).open('a') as environment:
            environment.write(f'TASKARIAN_FORCE_FULL_BUILD=1\nTASKARIAN_ANDROID_NATIVE_RECEIPT=\nMOZ_ARTIFACT_FILE=\nMOZCONFIG={ROOT / "browser/taskarian/android/mozconfig"}\n')
    try:
        hit = False if args.force_build or os.environ.get('TASKARIAN_FORCE_FULL_BUILD') == '1' else restore(args.directory.resolve(), args.target)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f'Optional native Gecko cache is unusable: {error}; using a normal build.', flush=True)
        for path in args.directory.iterdir():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        hit = False
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'hit={str(hit).lower()}\ndigest={digest}\nartifact={native_artifact(args.target, digest)}\n')
    if not hit:
        print('No matching verified native Gecko artifact; using the normal compiler cache.')


if __name__ == '__main__':
    main()
