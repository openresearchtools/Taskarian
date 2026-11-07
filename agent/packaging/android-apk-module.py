#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Reassemble only APK metadata around a verified, immutable compiled producer.

The small component is an index into the existing browser-resources release,
not another APK upload. Missing receipts require an ordinary build; old APKs
are never retrospectively declared to have a resolved dependency inventory.
"""
import argparse
import base64
import copy
from functools import cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
COMPONENT = 'android-apk-module'
RECEIPT = 'android-apk-module.json'
SELECTION = 'apk-module-selection.json'
ASSEMBLY = 'apk-metadata-assembly.json'
PACKAGE_METADATA_SOURCES = frozenset(('browser/taskarian/config/version.txt',
    'browser/taskarian/android/scripts/collect-artifacts.py',
    'browser/taskarian/android/scripts/prepare-signing.py'))
METADATA = frozenset(('AndroidManifest.xml', 'assets/about.html', 'assets/licenses.json',
    'assets/termux-update.json', 'assets/updates-bootstrap.mjs', 'assets/updates-prepare.py'))


@cache
def module(name):
    spec = importlib.util.spec_from_file_location('apk_' + name.replace('-', '_'), HERE / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def sha(file):
    with Path(file).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(file, value):
    Path(file).parent.mkdir(parents=True, exist_ok=True)
    Path(file).write_text(json.dumps(value, indent=2) + '\n')


def capture_directory():
    # Full license inventories stay out of normal installer/cache downloads.
    # Only the small compiled-module index owns this build-only receipt.
    return Path(os.environ['TASKARIAN_ANDROID_OBJDIR']) / 'apk-module-capture'


@cache
def metadata_compatible(revision):
    # VERSION_NAME remains the compiled producer's value. Its only admitted
    # consumers are disabled reporting; visible/current version uses PackageInfo.
    # A future functional consumer requires review, never silently stale data.
    result = subprocess.run(['git', 'grep', '-n', '-e', 'VERSION_NAME', revision, '--',
        'browser/mobile/android/fenix/app/src/main'], cwd=ROOT, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot verify the APK metadata consumer contract')
    actual = set()
    for line in result.stdout.splitlines():
        _revision, path, _number, content = line.split(':', 3)
        actual.add((path, content.strip()))
    prefix = 'browser/mobile/android/fenix/app/src/main/java/org/mozilla/fenix/'
    return actual == {
        (prefix + 'crashes/ReleaseRuntimeTagProvider.kt', 'RuntimeTag.RELEASE to BuildConfig.VERSION_NAME,'),
        (prefix + 'components/metrics/GleanUsageReporting.kt', 'Usage.appDisplayVersion.set(BuildConfig.VERSION_NAME)')}


@cache
def inputs(target, revision=None):
    release = module('browser-release')
    revision = revision or release.source()
    if target not in ('android', 'android-x86_64'):
        raise ValueError('An Android architecture is required')
    # Deliberately conservative: every browser source/resource/build definition
    # remains input except explicit version/signing/collection owners. Backend
    # JS and its package/license inventory are companion metadata, never APK code.
    paths = ['browser', '.github/builders/android.yml', 'LICENSE',
        'agent/native/phone-control', 'agent/vendor/phony', 'references/runtime-payload.json',
        'agent/vendor/native/android-sources.json', 'agent/packaging/references-component.py',
        *module('references-component').input_paths()]
    listing = module('browser-component').network_command(
        ['git', 'ls-tree', '-r', '-z', revision, '--', *paths], binary=True)
    tree = b'\0'.join(entry for entry in listing.split(b'\0') if entry and
        entry.split(b'\t', 1)[1].decode() not in PACKAGE_METADATA_SOURCES)
    termux = 'termux-x86_64' if target.endswith('x86_64') else 'termux-aarch64'
    return {'schema': 'taskarian-compiled-apk-1', 'target': target,
        'sourceTreeSha256': hashlib.sha256(tree).hexdigest(),
        'nativeLeaves': {name: module('android-components').binary_inputs(name, termux, revision)
            for name in ('android-remote', 'android-tor', 'android-x11')},
        'librarySources': release.android_source_inputs(target, revision)}


def signature(name):
    upper = name.upper()
    return upper.startswith('META-INF/') and (upper == 'META-INF/MANIFEST.MF' or
        upper.endswith(('.SF', '.RSA', '.DSA', '.EC')))


def inventory(apk):
    result = {}
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not METADATA <= set(names):
            raise ValueError('APK inventory is duplicated or lacks required metadata')
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe APK member: ' + info.filename)
            if not signature(info.filename):
                with archive.open(info) as stream:
                    result[info.filename] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def check_payload(apk, original, metadata=None):
    actual = inventory(apk)
    if (actual.keys() != original.keys() or any(actual[name] != expected
            for name, expected in original.items() if name not in METADATA) or
            (metadata is not None and (set(metadata) != METADATA or
                any(actual[name] != expected for name, expected in metadata.items())))):
        raise ValueError('APK changed immutable payload or expected assembly metadata')
    return actual


def validate_sources(sources, pins):
    # An actual producer captured this list after the ordinary collector checked
    # the resolved graph. Restoring it is not a new Gradle dependency resolution.
    expected = sorted((entry['component'], entry['kind'], Path(item['file']).name, item['sha256'])
        for entry in pins['entries'] for item in ([entry] if entry['kind'] == 'sources' else entry['files']))
    actual = sorted((entry['component'], entry['kind'], entry['file'], entry['sha256'])
        for entry in sources.get('sources.json', []))
    if sources.get('pins.json') != pins or sources.get('missing.json') != [] or actual != expected:
        raise ValueError('Retained resolved dependency inventory differs from current pins')


def validate_record(record, target):
    release = module('browser-release')
    source = record.get('source', '')
    module('browser-component').ensure_source(source)
    if (record.get('schema') != 1 or record.get('target') != target or
            not metadata_compatible(source) or not metadata_compatible(release.source()) or
            not re.fullmatch('[0-9a-f]{40}', source) or record.get('inputs') != inputs(target) or
            record.get('inputs') != inputs(target, source) or
            record.get('manifest', {}).get('source') != source or
            record['manifest'].get('product_version') != record.get('version') or
            record.get('version') != release.version(source) or
            record['manifest'].get('package_id') != 'com.taskarian' or
            record['manifest'].get('publisher_signed') is not True or
            record['manifest'].get('architecture') != ('x86_64' if target.endswith('x86_64') else 'arm64-v8a') or
            record['manifest'].get('build_run') != record.get('run') or
            not re.fullmatch('[0-9]+', str(record.get('run', ''))) or
            record['manifest'].get('build_repository') != module('component-store').repository(target) or
            record.get('apkFile') != 'taskarian_' + str(record.get('version')) + '_' +
                ('x86_64' if target.endswith('x86_64') else 'arm64-v8a') + '.apk' or
            not re.fullmatch('[0-9a-f]{64}', record.get('apkSha256', '')) or
            record['manifest'].get('apks', {}).get(record.get('apkFile')) != record.get('apkSha256')):
        raise ValueError('Compiled APK producer identity or current code inputs differ')
    pins = json.loads(release.text(release.source(), 'browser/taskarian/android/sources/sources.json'))
    validate_sources(record.get('sourceInventory', {}), pins)
    if record.get('librarySourceInputs') != release.android_source_inputs(target):
        raise ValueError('Retained Android source owner differs from current pins')
    if not isinstance(record.get('mavenDependencies'), list) or not record['mavenDependencies']:
        raise ValueError('Missing actual producer Maven dependency inventory')
    expected_dependencies = json.loads(release.text(release.source(),
        'browser/taskarian/android/notices/maven/sources.json'))['dependencies']
    actual_dependencies = sorted({':'.join(entry[key] for key in ('group', 'name', 'version'))
                                  for entry in record['mavenDependencies']})
    if actual_dependencies != expected_dependencies:
        raise ValueError('Retained resolved Maven dependencies differ from current notice pins')
    legal = record.get('compiledLegal')
    if not isinstance(legal, list) or not legal or any(not entry.get('name') or not entry.get('text', '').strip() for entry in legal):
        raise ValueError('Missing compiled APK license inventory')
    files = record.get('files', {})
    if not METADATA <= files.keys() or any(not re.fullmatch('[0-9a-f]{64}', value) for value in files.values()):
        raise ValueError('Invalid compiled APK payload inventory')


def capture(directory, target, manifest, sources):
    """Called only after all ordinary collector checks, using actual outputs."""
    directory, sources = Path(directory), Path(sources)
    retained = os.environ.get('TASKARIAN_APK_MODULE_DIR')
    if retained:
        record = json.loads((Path(retained) / RECEIPT).read_text())
        validate_record(record, target)
        write(capture_directory() / RECEIPT, record)
        verify_assembly(directory, target)
        return
    if not metadata_compatible(manifest['source']):
        print('APK version consumers require review before metadata-only module reuse', flush=True)
        return
    app, = directory.glob('taskarian_*.apk')
    generated = sources.parent
    dependencies = generated / 'third_party_licenses/release/dependencies.json'
    companion = json.loads(Path(os.environ['TASKARIAN_COMPANION_LICENSES']).read_text())
    with zipfile.ZipFile(app) as archive:
        legal = json.loads(archive.read('assets/licenses.json'))
    if not isinstance(companion, list) or not companion or len(legal) <= len(companion):
        raise ValueError('Actual APK legal inventory lacks its companion/compiled boundary')
    for actual, expected in zip(legal, companion):
        # About's existing renderer augments the Taskarian record with permanent
        # component references. It does not change the underlying legal text.
        ignored = {'sourceProvenance'} if expected.get('name') == 'Taskarian' else set()
        if ({k: v for k, v in actual.items() if k not in ignored} !=
                {k: v for k, v in expected.items() if k not in ignored}):
            raise ValueError('APK companion legal prefix differs from its actual published package')
    record = {'schema': 1, 'target': target, 'source': manifest['source'],
        'version': manifest['product_version'], 'run': manifest['build_run'],
        'inputs': inputs(target), 'apkFile': app.name, 'apkSha256': sha(app),
        'manifest': manifest, 'files': inventory(app),
        'sourceInventory': {name: json.loads((sources / name).read_text())
            for name in ('sources.json', 'pins.json', 'missing.json')},
        'mavenDependencies': json.loads(dependencies.read_text()),
        'compiledLegal': legal[len(companion):],
        'librarySourceInputs': module('browser-release').android_source_inputs(target)}
    validate_record(record, target)
    write(capture_directory() / RECEIPT, record)


def matches_resource(record, resource):
    """Bind a capture to the exact canonical APK and its producing manifest."""
    assets = {entry['name']: entry['sha256'] for entry in resource['manifest']['assets']}
    manifest_sha = hashlib.sha256((json.dumps(record['manifest'], indent=2) + '\n').encode()).hexdigest()
    return (resource['manifest']['sourceCommit'] == record['source'] and
            assets.get(record['apkFile']) == record['apkSha256'] and
            assets.get('build-manifest.json') == manifest_sha)


def publish(directory, target, resource):
    directory, store, release = Path(directory), module('component-store'), module('browser-release')
    file = capture_directory() / RECEIPT
    if not file.is_file():
        return  # No legacy seeding/backfill or forced build merely to seed.
    record = json.loads(file.read_text())
    validate_record(record, target)
    existing = store.lookup(COMPONENT, target, inputs(target))
    if existing:
        return existing
    if not matches_resource(record, resource):
        # A retry can find an older canonical browser-resources release. Its
        # unrecorded resolved graph cannot be inferred from a fresh build.
        print('Canonical APK lacks this exact producer capture; no module index seeded', flush=True)
        return
    if os.environ.get('TASKARIAN_APK_MODULE_DIR') or record['source'] != release.source():
        raise ValueError('Only an actual full APK producer may establish a compiled-module index')
    with tempfile.TemporaryDirectory(prefix='apk-module-index-') as temporary:
        output = Path(temporary)
        write(output / RECEIPT, record)
        write(output / 'apk-module-source.json', {'browserResources': release.reference(resource)})
        (output / 'Taskarian-LICENSE.txt').write_bytes((ROOT / 'LICENSE').read_bytes())
        return store.publish(COMPONENT, target, record['version'], record['inputs'], output,
            assets={RECEIPT: 'metadata', 'apk-module-source.json': 'metadata', 'Taskarian-LICENSE.txt': 'legal'},
            source_commit=record['source'], dependencies=[release.reference(resource)])


def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
            stream.write(f'{name}={value}\n')


def restore(directory, target):
    directory, store, release = Path(directory), module('component-store'), module('browser-release')
    if not metadata_compatible(release.source()):
        output('hit', 'false')
        print('APK version consumer contract changed; ordinary build required', flush=True)
        return False
    found = store.lookup(COMPONENT, target, inputs(target))
    if not found:
        output('hit', 'false')
        print('No captured matching compiled APK module; ordinary build required', flush=True)
        return False
    directory.mkdir(parents=True, exist_ok=True)
    store.fetch(found, directory, roles=('metadata',))
    record = json.loads((directory / RECEIPT).read_text())
    validate_record(record, target)
    if found['manifest']['sourceCommit'] != record['source']:
        raise ValueError('Compiled module index differs from its actual producer source')
    origin = json.loads((directory / 'apk-module-source.json').read_text())['browserResources']
    if (store.reference_identity(origin)[:2] != ('browser-resources', target) or
            origin not in found['manifest'].get('dependencies', [])):
        raise ValueError('Compiled module does not reference its architecture APK source owner')
    producer = store.fetch(origin, directory, roles=('runtime',), names=[record['apkFile']])
    if producer is None:
        raise store.StoreUnavailable('Compiled APK producer payload is unavailable; retry without rebuilding')
    app = directory / record['apkFile']
    if (not matches_resource(record, producer) or sha(app) != record['apkSha256'] or
            inventory(app) != record['files']):
        raise ValueError('Compiled APK payload differs from its verified original producer')
    write(directory / SELECTION, {'moduleRelease': release.reference(found), 'browserResources': origin})
    release.remember(found, directory, 'android-apk-module')
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as stream:
            stream.write('TASKARIAN_APK_MODULE_DIR=' + str(directory.resolve()) + '\n')
    output('hit', 'true')
    print('Selected immutable compiled APK producer:', record['source'], flush=True)
    return True


def copy_zip(source, destination, replacements=None, resources_only=False):
    replacements = replacements or {}
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination, 'w') as new:
        if len(old.namelist()) != len(set(old.namelist())):
            raise ValueError('Duplicate APK members')
        if set(replacements) - set(old.namelist()):
            raise ValueError('Metadata assembly cannot add unregistered APK entries')
        for original in old.infolist():
            name = original.filename
            if signature(name) or (resources_only and name not in ('AndroidManifest.xml', 'resources.arsc') and not name.startswith('res/')):
                continue
            info = copy.copy(original)
            if name in replacements:
                new.writestr(info, replacements[name])
            else:
                with old.open(original) as source_stream, new.open(info, 'w') as output_stream:
                    shutil.copyfileobj(source_stream, output_stream, 1024 * 1024)


def tools(directory):
    pins = json.loads((HERE / 'android-apk-tools.json').read_text())
    catalog = module('browser-release').text(module('browser-release').source(), 'browser/gradle/libs.versions.toml')
    plugin = re.search(r'^android-gradle-plugin = "([^"]+)"', catalog, re.M)
    if not plugin or pins['aapt2Version'].split('-', 1)[0] != plugin[1]:
        raise ValueError('Review the AAPT2 metadata adapter with the selected Android Gradle plugin')
    jars = {name: module('source_cache').fetch(pin) for name, pin in pins['files'].items()}
    aapt = directory / 'aapt2'
    with zipfile.ZipFile(jars['aapt2-linux.jar']) as bundle:
        aapt.write_bytes(bundle.read('aapt2'))
    aapt.chmod(0o755)
    java = str(Path(os.environ['JAVA_HOME']) / 'bin/java') if os.environ.get('JAVA_HOME') else shutil.which('java')
    if not java:
        raise ValueError('The existing Android build JDK is required for metadata assembly')
    return [str(aapt)], [java, '--class-path', os.pathsep.join(str(jars[name]) for name in ('aapt2-proto.jar', 'protobuf-java.jar')), str(HERE / 'ManifestVersion.java')]


def manifest_version(apk, directory, old, new, commands=None):
    with zipfile.ZipFile(apk) as archive:
        original = archive.read('AndroidManifest.xml')
    if old == new:
        return original
    aapt, java = commands or tools(directory)
    def convert(source, destination, kind):
        subprocess.run([*aapt, 'convert', '--output-format', kind, '-o', str(destination), str(source)], check=True)
    binary, proto, changed = (directory / name for name in ('resources.apk', 'proto.apk', 'changed.apk'))
    copy_zip(apk, binary, resources_only=True)
    convert(binary, proto, 'proto')
    before, after = directory / 'original.pb', directory / 'changed.pb'
    with zipfile.ZipFile(proto) as archive:
        before.write_bytes(archive.read('AndroidManifest.xml'))
    subprocess.run([*java, 'update', str(before), str(after), old, new], check=True)
    copy_zip(proto, changed, {'AndroidManifest.xml': after.read_bytes()})
    convert(changed, binary, 'binary')
    with zipfile.ZipFile(binary) as archive:
        result = archive.read('AndroidManifest.xml')
    convert(binary, proto, 'proto')
    with zipfile.ZipFile(proto) as archive:
        after.write_bytes(archive.read('AndroidManifest.xml'))
    subprocess.run([*java, 'verify', str(before), str(after), old, new], check=True)
    return result


def assemble(directory, target, destination):
    directory, destination = Path(directory), Path(destination)
    record = json.loads((directory / RECEIPT).read_text())
    validate_record(record, target)
    source = directory / record['apkFile']
    if sha(source) != record['apkSha256'] or inventory(source) != record['files']:
        raise ValueError('Original compiled APK changed before metadata assembly')
    obj = Path(os.environ['TASKARIAN_ANDROID_OBJDIR'])
    generated = obj / 'gradle/build/mobile/android/fenix/app/generated'
    for name, value in record['sourceInventory'].items():
        write(generated / 'taskarian-sources' / name, value)
    write(generated / 'third_party_licenses/release/dependencies.json', record['mavenDependencies'])
    selection = json.loads((directory / SELECTION).read_text())
    provenance = {**selection, 'compiledSource': record['source'], 'compiledVersion': record['version'],
        'compiledRun': record['run'], 'assemblySource': module('browser-release').source()}
    write(directory / 'assembly-provenance.json', provenance)
    write(directory / 'native-gecko.json', record['manifest']['native_gecko'])
    os.environ['TASKARIAN_APK_MODULE_DIR'] = str(directory.resolve())
    os.environ['TASKARIAN_ANDROID_NATIVE_RECEIPT'] = str((directory / 'native-gecko.json').resolve())
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as stream:
            stream.write('TASKARIAN_ANDROID_NATIVE_RECEIPT=' + os.environ['TASKARIAN_ANDROID_NATIVE_RECEIPT'] + '\n')
    about = generated / 'taskarian-about'
    about.mkdir(parents=True, exist_ok=True)
    companion = Path(os.environ['TASKARIAN_COMPANION_LICENSES'])
    release = module('browser-release')
    version = release.version(release.source())
    pairing = module('release').validate_termux_update(json.loads((companion.parent / 'termux-update.json').read_text()),
        version, 'x86_64' if target.endswith('x86_64') else 'arm64-v8a')
    bootstrap = (ROOT / 'agent/src/server/platform/termux/updates-bootstrap.mjs').read_bytes()
    if pairing['sourceCommit'] != release.source() or pairing['bootstrapSha256'] != hashlib.sha256(bootstrap).hexdigest():
        raise ValueError('Current companion pairing/source/bootstrap differs')
    legal = json.loads(companion.read_text())
    if not isinstance(legal, list) or not legal:
        raise ValueError('Current companion has no actual license inventory')
    legal += record['compiledLegal']
    legal.append({'name': 'APK assembly and compiled browser provenance',
        'license': 'See Taskarian and component licenses', 'sourceCommit': release.source(),
        'sourceProvenance': provenance, 'text': json.dumps(provenance, indent=2)})
    spec = importlib.util.spec_from_file_location('apk_license_texts', ROOT / 'browser/taskarian/android/scripts/license_texts.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for entry in legal:
        validator.validate_text(entry['name'], entry.get('text', ''))
    write(about / 'licenses.json', legal)
    subprocess.run(['python3', str(HERE / 'about.py'), 'android', str(about / 'licenses.json'),
        str(about / 'about.html'), '--version', version], check=True)
    shutil.copy2(companion.parent / 'termux-update.json', about / 'termux-update.json')
    (about / 'updates-bootstrap.mjs').write_bytes(bootstrap)
    shutil.copy2(ROOT / 'agent/src/server/platform/termux/updates-prepare.py', about / 'updates-prepare.py')
    replacements = {'assets/' + name: (about / name).read_bytes() for name in
        ('about.html', 'licenses.json', 'termux-update.json', 'updates-bootstrap.mjs', 'updates-prepare.py')}
    with tempfile.TemporaryDirectory(prefix='apk-native-manifest-') as temporary:
        replacements['AndroidManifest.xml'] = manifest_version(source, Path(temporary), record['version'],
            module('browser-release').version(module('browser-release').source()))
    candidate = obj / 'gradle/build/mobile/android/fenix/app/outputs/apk/release/fenix-release.apk'
    candidate.parent.mkdir(parents=True, exist_ok=True)
    copy_zip(source, candidate, replacements)
    actual = check_payload(candidate, record['files'])
    destination.mkdir(parents=True, exist_ok=True)
    write(destination / ASSEMBLY, {'schema': 1, **provenance, 'target': target,
        'producerReceiptSha256': sha(directory / RECEIPT),
        'metadata': {name: actual[name] for name in sorted(METADATA)}})
    print('Reassembled APK metadata without Kotlin, R8 or native compilation:', candidate, flush=True)


def verify_assembly(directory, target):
    directory = Path(directory)
    producer_dir = Path(os.environ['TASKARIAN_APK_MODULE_DIR'])
    record = json.loads((producer_dir / RECEIPT).read_text())
    validate_record(record, target)
    proof = json.loads((directory / ASSEMBLY).read_text())
    if (proof.get('schema') != 1 or proof.get('target') != target or
            proof.get('assemblySource') != module('browser-release').source() or
            proof.get('compiledSource') != record['source'] or
            proof.get('compiledVersion') != record['version'] or proof.get('compiledRun') != record['run'] or
            proof.get('moduleRelease') != json.loads((producer_dir / SELECTION).read_text())['moduleRelease'] or
            proof.get('producerReceiptSha256') != sha(producer_dir / RECEIPT) or
            set(proof.get('metadata', {})) != METADATA):
        raise ValueError('APK metadata assembly lacks its original compiled producer proof')
    app, = directory.glob('taskarian_*.apk')
    check_payload(app, record['files'], proof['metadata'])
    return record


def verify_provenance(directory, target, manifest):
    record = manifest.get('compiled_apk')
    if record is None:
        return  # Older exact-source artifacts remain compatible, never module seeds.
    source = record.get('source', '')
    module('browser-component').ensure_source(source)
    release = module('browser-release')
    if (record.get('inputs') != inputs(target) or record['inputs'] != inputs(target, source) or
            record.get('version') != release.version(source) or
            not re.fullmatch('[0-9]+', str(record.get('run', ''))) or
            not re.fullmatch('[0-9a-f]{64}', record.get('producerApkSha256', ''))):
        raise ValueError('APK compiled producer identity/current inputs differ')
    if source == manifest['source']:
        if record['producerApkSha256'] not in manifest['apks'].values():
            raise ValueError('Full APK producer differs from its actual signed payload')
        return
    if not metadata_compatible(source) or not metadata_compatible(release.source()):
        raise ValueError('APK version consumer contract no longer permits retained compiled metadata')
    store = module('component-store')
    component, selected_target, _digest = store.reference_identity(record.get('moduleRelease', {}))
    if component != COMPONENT or selected_target != target:
        raise ValueError('APK compiled producer reference is not its architecture module')
    proof = json.loads((Path(directory) / ASSEMBLY).read_text())
    if (proof.get('schema') != 1 or proof.get('target') != target or
            proof.get('assemblySource') != manifest['source'] or proof.get('compiledSource') != source or
            proof.get('compiledVersion') != record['version'] or proof.get('compiledRun') != record['run'] or
            proof.get('moduleRelease') != record['moduleRelease'] or set(proof.get('metadata', {})) != METADATA):
        raise ValueError('APK metadata assembly provenance differs')
    app, = Path(directory).glob('taskarian_*.apk')
    with zipfile.ZipFile(app) as archive:
        for name, expected in proof['metadata'].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError('APK metadata differs from its verified assembly')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['restore', 'assemble', 'inputs'])
    parser.add_argument('target')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action == 'inputs':
        print(json.dumps(inputs(args.target), indent=2))
    elif args.action == 'restore':
        restore(args.directory, args.target)
    else:
        assemble(args.directory, args.target, args.output)
