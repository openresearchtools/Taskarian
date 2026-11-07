#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Durable browser components through the existing architecture releases."""
import argparse
from functools import cache
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
LEGACY_CACHE_MOZILLA_VERSION = '153.6.0esr'


@cache
def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'agent/packaging' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def source():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()


def text(revision, path):
    return module('browser-component').network_command(['git', 'show', revision + ':' + path]).strip()


def version(revision, native=False):
    path = 'browser/browser/config/version_display.txt' if native else 'browser/taskarian/config/version.txt'
    return text(revision, path)


def inputs(kind, target, revision=None, companion=None, *, legacy_release_module=False):
    revision = revision or source()
    browser = module('browser-component')
    if kind == 'gecko':
        digest = (module('android-native-component').native_inputs(revision, target)
                  if target.startswith('android') else browser.linux_engine_inputs(revision, target))
        return {'schema': 'taskarian-gecko-2', 'target': target, 'nativeSha256': digest,
                'engineVersion': version(revision, True)}
    # The resource package contains the engine too: retain its actual scoped
    # identity, all application resources, and the native packaging recipe.
    paths = [path for path in browser.input_paths(target) if not path.startswith(':')]
    listing = browser.network_command(['git', 'ls-tree', '-r', '-z', revision, '--', *paths], binary=True)
    entries = []
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        path = entry.split(b'\t', 1)[1].decode()
        if path in browser.FIXTURE_METADATA:
            continue
        if target.startswith('linux') and path.startswith(('browser/mobile/android/', 'browser/taskarian/android/')):
            continue
        if target.startswith('android') and path == browser.RELEASE_RUNTIME and not legacy_release_module:
            mode, entry_kind, blob = entry.split(b'\t', 1)[0].split()
            entry = b' '.join((mode, entry_kind, browser.release_runtime_blob(blob.decode()).encode())) + b'\t' + path.encode()
        entries.append(entry)
    return {'schema': 'taskarian-browser-resources-1', 'target': target,
            'resourceSha256': hashlib.sha256(b'\0'.join(entries)).hexdigest(),
            'productVersion': version(revision), 'gecko': inputs('gecko', target, revision),
            'companion': companion}


def native_key(target, revision=None):
    return module('component-store').identity('gecko', target, inputs('gecko', target, revision))['inputSha256']


def reference(result):
    return {key: result[key] for key in ('repository', 'manifestName', 'manifestSha256')}


def cache_contract(target):
    revision = source()
    rust = json.loads(text(revision, 'agent/machines/rust-toolchain/sources.json'))
    contract = {'schema': 1, 'engine': 'sccache', 'target': target, 'sccacheVersion': '0.17.0',
                'rust': {'version': rust['version'], 'commit': rust['commit']}}
    if target.startswith('android'):
        contract['mozconfig'] = hashlib.sha256(text(revision, 'browser/taskarian/android/mozconfig').encode()).hexdigest()
    else:
        contract['flags'] = ['--enable-application=browser', '--enable-optimize', '--disable-debug',
                             '--disable-crashreporter', '--disable-tests', '--disable-debug-symbols']
    return contract


def legacy_cache_contract(target):
    # The 54e2dd6 engine-only jobs keyed their first disk/backups on 153.6.0esr.
    # Retain that exact historical alias; native sccache validates each object.
    return {**cache_contract(target), 'mozillaVersion': LEGACY_CACHE_MOZILLA_VERSION}


def restore_cache(directory, target):
    helper = module('compiler-cache')
    for contract in (cache_contract(target), legacy_cache_contract(target)):
        if helper.restore('sccache', target, contract, directory):
            return True
    return False


def remember(result, directory, kind):
    file = Path(directory).parent / (kind + '-release-reference.json')
    file.write_text(json.dumps(reference(result), indent=2) + '\n')
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as output:
            output.write('TASKARIAN_' + kind.upper().replace('-', '_') + '_RELEASE_REFERENCE=' + str(file.resolve()) + '\n')
    return file


def checkpoint_inventory(result, directory):
    """Keep release-only metadata outside the native checkpoint's flat inventory."""
    directory = Path(directory)
    checkpoint_files = {line.split('  ', 1)[1]
                        for line in (directory / 'SHA256SUMS').read_text().splitlines()}
    for asset in result['manifest']['assets']:
        if asset['role'] in ('legal', 'metadata') and asset['name'] not in checkpoint_files:
            path = directory / asset['name']
            if path.is_file():
                metadata = directory.parent / (directory.name + '-metadata')
                metadata.mkdir(exist_ok=True)
                shutil.move(path, metadata / asset['name'])


def engine_record(result, directory, target):
    browser = module('browser-component')
    directory = Path(directory)
    checkpoint_inventory(result, directory)
    browser.verify_checksums(directory)
    if target.startswith('android'):
        native = module('android-native-component')
        record = json.loads((directory / 'native-gecko.json').read_text())
        payload = directory / 'gecko-libraries.apk'
        if record['source'] != result['manifest']['sourceCommit']:
            raise ValueError('Android engine native source differs from its release')
        record.setdefault('producer', {
            'kind': 'component-release', 'source': record['source'],
            'repository': result['repository'], 'run': result['manifest']['producer']['run'],
            'release': reference(result),
            'manifest_sha256': hashlib.sha256((directory / 'native-gecko.json').read_bytes()).hexdigest(),
            'apk_sha256': hashlib.sha256(payload.read_bytes()).hexdigest()})
        native.verify_libraries(payload, record, target)
        native.install_native(directory.parent, payload, record)
        return
    manifest = dict(line.split('=', 1) for line in (directory / 'browser-artifact-manifest.txt').read_text().splitlines())
    archive, = directory.glob('taskarian*.tar.*')
    origin = result['manifest']['sourceCommit']
    if manifest['taskarian_commit'] != origin or inputs('gecko', target, origin) != inputs('gecko', target):
        raise ValueError('Stored Gecko producer compiler inputs differ')
    record = {'schema': 1, 'target': target, 'source': origin, 'resourceSource': source(),
              'inputSha256': browser.linux_engine_inputs(origin, target),
              'nativeFiles': browser.native_elf_inventory(archive), 'producer': result['manifest']['producer'],
              'componentRelease': reference(result)}
    producer = record['producer']
    if (producer.get('source') != origin or producer.get('runSource') != origin or
            not producer.get('steps', {}).get('build') or
            producer.get('steps', {}).get('verify', {}).get('conclusion') != 'success' or
            producer.get('steps', {}).get('upload', {}).get('conclusion') != 'success'):
        raise ValueError('Stored Linux engine lacks its original native checkpoint')
    xpts = list(directory.glob('*.xpt_artifacts.zip'))
    if len(xpts) > 1:
        raise ValueError('Duplicate Gecko interface metadata')
    receipt = directory.parent / browser.NATIVE_ENGINE_RECEIPT
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    with Path(os.environ['GITHUB_ENV']).open('a') as output:
        output.write(f'MOZ_ARTIFACT_FILE={archive.resolve()}\nTASKARIAN_LINUX_NATIVE_RECEIPT={receipt.resolve()}\n')
        if xpts:
            output.write(f'TASKARIAN_LINUX_XPT_ARTIFACT={xpts[0].resolve()}\n')


def restore_engine(directory, target):
    directory = Path(directory)
    store = module('component-store')
    result = store.restore('gecko', target, inputs('gecko', target), directory / 'producer',
                    # Native checkpoints inventory their captured license page too.
                    roles=('runtime', 'metadata', 'build-support', 'legal'), requested_source=source())
    if result is None:
        shutil.rmtree(directory / 'producer', ignore_errors=True)
        return False
    try:
        engine_record(result, directory / 'producer', target)
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        shutil.rmtree(directory / 'producer', ignore_errors=True)
        raise store.StoreUnavailable(
            f'Published Gecko checkpoint for {target} failed native verification; '
            'retry without rebuilding the completed engine') from error
    remember(result, directory, 'gecko')
    print('Reused durable Gecko component:', result['url'], flush=True)
    return True


def android_pairing(target, directory):
    """Read exact package expectations from its published, checksummed sidecar."""
    architecture = 'x86_64' if target == 'android-x86_64' else 'aarch64'
    termux = 'termux-' + architecture
    store, application, release = module('component-store'), module('application'), module('release')
    result = store.lookup('application', termux, application.inputs(termux))
    if result is None:
        return None
    packages = [item for item in result['manifest']['assets'] if item['name'].endswith('.deb')]
    if len(packages) != 1:
        raise ValueError('Android requires one published parent Termux package')
    backend, = packages
    metadata_name = backend['name'] + '.json'
    if store.fetch(result, directory, roles=('runtime', 'metadata'), names=[metadata_name]) is None:
        raise store.StoreUnavailable('Published Termux package metadata is unavailable')
    metadata = json.loads((Path(directory) / metadata_name).read_text())
    os_identity = metadata.get('components', {}).get('os', {})
    if (metadata.get('asset') != backend['name'] or metadata.get('sha256') != backend['sha256'] or
            metadata.get('revision') != source() or result['manifest']['sourceCommit'] != source() or
            metadata.get('platform') != 'android' or metadata.get('architecture') != architecture or
            metadata.get('version') != version(source()) or not os_identity.get('embedded')):
        raise ValueError('Published Termux package metadata differs from its actual application identity')
    pairing = release.validate_termux_update({
        'schema': 2, 'productVersion': metadata['version'], 'sourceCommit': metadata['revision'],
        'integrationIteration': metadata.get('integrationIteration', False),
        'bootstrapSha256': hashlib.sha256(subprocess.check_output(
            ['git', 'show', source() + ':agent/src/server/platform/termux/updates-bootstrap.mjs'], cwd=ROOT)).hexdigest(),
        'architecture': architecture, 'apkAbi': 'x86_64' if architecture == 'x86_64' else 'arm64-v8a',
        'packages': [{'name': 'taskarian', 'version': metadata['version'], 'architecture': architecture,
                      'sourceCommit': metadata['revision'], 'sha256': backend['sha256']}],
        'components': {'os': {key: os_identity[key] for key in
            ('version', 'architecture', 'sourceCommit', 'installedManifestSha256')}}})
    return pairing, result


def restore_android_resources(directory, target):
    """Reuse the original APK only for its exact source, version and companion."""
    store, browser, application = module('component-store'), module('browser-component'), module('application')
    result = store.lookup('application', target, application.inputs(target))
    application_hit = result is not None
    with tempfile.TemporaryDirectory(prefix='android-pairing-', dir=Path(directory).parent) as temporary:
        paired = android_pairing(target, Path(temporary))
    if paired is None:
        if application_hit:
            raise store.StoreUnavailable('Published Android application has no available paired Termux metadata; retry without rebuilding')
        return False
    pairing, companion = paired
    if result is None:
        # The resource checkpoint may have completed before final app publication.
        for legacy in (False, True):
            wanted = inputs('browser-resources', target, companion=pairing, legacy_release_module=legacy)
            result = store.lookup('browser-resources', target, wanted)
            if result is not None:
                break
    if result is None:
        return False
    if result['manifest']['sourceCommit'] != source() or result['manifest']['version'] != version(source()):
        raise ValueError('Android resource producer source or embedded product version differs')
    restored = store.fetch(result, directory, roles=('runtime', 'metadata', 'build-support', 'legal'))
    if restored is None:
        raise store.StoreUnavailable('Published Android resources are unavailable; retry without rebuilding')
    checkpoint_inventory(restored, directory)
    browser.verify_checksums(directory)
    browser.record_android_equivalence(directory, target)
    browser.verify(directory, target, browser.fingerprint(target))
    app, = Path(directory).glob('taskarian_*.apk')
    with zipfile.ZipFile(app) as archive:
        actual = module('release').validate_termux_update(json.loads(archive.read('assets/termux-update.json')))
    if actual != pairing:
        raise ValueError('Published APK expectations differ from the actual paired Termux package')
    remember(restored, directory, restored['manifest']['component'])
    if restored['manifest']['component'] == 'application':
        resources = [item for item in restored['manifest'].get('dependencies', [])
                     if item['manifestName'].startswith('component-browser-resources-' + target + '-')]
        if len(resources) != 1:
            raise ValueError('Published Android application lacks its exact browser resource owner')
        remember(resources[0], directory, 'browser-resources')
    remember(companion, directory, 'termux-application')
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('application_hit=' + str(application_hit).lower() + '\n')
    print('Reused complete Android application resources:', restored['url'], flush=True)
    return True


def restore_resources(directory, target):
    if target.startswith('android'):
        try:
            return restore_android_resources(directory, target)
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile) as error:
            raise module('component-store').StoreUnavailable(
                'Published Android resources failed verification; retry without rebuilding: ' + str(error)) from error
    store, browser = module('component-store'), module('browser-component')
    result = store.restore('browser-resources', target, inputs('browser-resources', target), directory,
                           roles=('runtime', 'metadata', 'build-support', 'legal'), requested_source=source())
    if result is None:
        for file in Path(directory).iterdir():
            shutil.rmtree(file) if file.is_dir() else file.unlink()
        return False
    try:
        checkpoint_inventory(result, directory)
        browser.verify(directory, target, browser.fingerprint(target))
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile) as error:
        for file in Path(directory).iterdir():
            shutil.rmtree(file) if file.is_dir() else file.unlink()
        print('Unusable optional browser resource release:', error, flush=True)
        return False
    remember(result, directory, 'browser-resources')
    return True


def engine_available(target):
    return module('component-store').lookup('gecko', target, inputs('gecko', target)) is not None


def android_companion(directory, target):
    """Use full notices and package identities from the actual published backend."""
    directory = Path(directory)
    architecture = 'x86_64' if target == 'android-x86_64' else 'aarch64'
    termux = 'termux-' + architecture
    application, store = module('application'), module('component-store')
    deadline = time.monotonic() + 300 * 60
    while store.lookup('application', termux, application.inputs(termux)) is None:
        if time.monotonic() >= deadline:
            raise ValueError('Timed out waiting for the exact paired Termux application')
        print('Waiting for the exact paired Termux application manifest', flush=True)
        time.sleep(30)
    result = store.restore('application', termux, application.inputs(termux), directory,
                           roles=('runtime', 'metadata', 'legal'))
    if result is None:
        raise ValueError('The exact paired Termux application is not published yet')
    packages = []
    for file in directory.glob('*.deb'):
        fields = dict(line.split(': ', 1) for line in subprocess.check_output(
            ['dpkg-deb', '-f', str(file), 'Package', 'Architecture'], text=True).splitlines())
        if fields != {'Package': 'taskarian', 'Architecture': architecture}:
            raise ValueError('Android requires one matching parent Taskarian package')
        packages.append(file)
    if len(packages) != 1:
        raise ValueError('Expected one Taskarian package with its embedded OS')
    backend = packages[0]
    release = module('release')
    with tempfile.TemporaryDirectory(prefix='android-companion-') as temporary:
        subprocess.run(['dpkg-deb', '-x', str(backend), temporary], check=True)
        app = Path(temporary) / 'data/data/com.termux/files/usr/lib/taskarian'
        os_identity = release.termux_os_identity(Path(temporary))
        if not os_identity['embedded']:
            raise ValueError('The APK requires the OS inside its Taskarian package')
        licenses = app / 'licenses.json'
        records = json.loads(licenses.read_text())
        if not isinstance(records, list) or not records:
            raise ValueError('Paired backend lacks its complete actual license inventory')
        destination = Path(os.environ['TASKARIAN_COMPANION_LICENSES'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(licenses, destination)
    pairing = release.validate_termux_update({
        'schema': 2, 'productVersion': version(source()), 'sourceCommit': source(),
        'integrationIteration': False,
        'bootstrapSha256': hashlib.sha256((ROOT / 'agent/src/server/platform/termux/updates-bootstrap.mjs').read_bytes()).hexdigest(),
        'architecture': architecture, 'apkAbi': 'x86_64' if architecture == 'x86_64' else 'arm64-v8a',
        'packages': [
            {'name': 'taskarian', 'version': version(source()), 'architecture': architecture,
             'sourceCommit': source(), 'sha256': store.sha(backend)}],
        'components': {'os': {key: os_identity[key] for key in
            ('version', 'architecture', 'sourceCommit', 'installedManifestSha256')}}})
    (destination.parent / 'termux-update.json').write_text(json.dumps(pairing, indent=2) + '\n')
    remember(result, directory, 'termux-application')


def dependencies(directories):
    found = {}

    def visit(value):
        if isinstance(value, dict):
            if all(key in value for key in ('repository', 'manifestName', 'manifestSha256')):
                found[(value['repository'], value['manifestName'])] = reference(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for directory in directories:
        for file in Path(directory).rglob('*.json'):
            try:
                visit(json.loads(file.read_text()))
            except (ValueError, UnicodeError):
                continue
    for name in ('GECKO', 'GECKOVIEW', 'BROWSER_RESOURCES', 'TERMUX_APPLICATION', 'ANDROID_APK_MODULE'):
        if os.environ.get('TASKARIAN_' + name + '_RELEASE_REFERENCE'):
            visit(json.loads(Path(os.environ['TASKARIAN_' + name + '_RELEASE_REFERENCE']).read_text()))
    return list(found.values())


def source_legal(directory, revision, target, kind):
    """Retain complete corresponding source and full native notice inputs."""
    browser = module('browser-component')
    browser.ensure_source(revision)
    dependencies = []
    assets = {}
    if kind == 'browser-resources':
        assets, baseline = module('source-delivery').package_repository_source(revision, directory)
        dependencies.extend([baseline, reference(blocker_sources(revision, target))])
    else:
        name = 'browser-source-' + revision + '.tar.gz'
        paths = ['browser', 'references/connector', 'references/vendor/translators', 'references/legal',
                 'references/web/theme.css', 'agent/packaging', 'agent/machines/rust-toolchain',
                 'agent/search/third_party/unsloth-studio', 'LICENSE', 'AGENTS.md']
        module('source_archive').archive_revision(ROOT, revision, directory / name, paths=paths)
        assets[name] = 'source'
    legal = 'browser-licenses-' + revision + '.tar.gz'
    paths = ['LICENSE', 'browser/LICENSE', 'browser/COPYING', 'browser/taskarian/packaging',
             'browser/taskarian/android/notices', 'browser/toolkit/content/license.html',
             'browser/taskarian/SOURCE-NOTICE', 'agent/machines/rust-toolchain',
             'agent/search/third_party/unsloth-studio']
    module('source_archive').archive_revision(ROOT, revision, directory / legal, paths=paths)
    assets[legal] = 'legal'
    if kind == 'gecko':
        pin = json.loads(text(revision, 'agent/machines/rust-toolchain/sources.json'))
        rust = module('source_cache').fetch(pin)
        shutil.copy2(rust, directory / pin['file'])
        assets[pin['file']] = 'source'
    return assets, dependencies


def blocker_sources(revision, target):
    """Retain generated blocker resources' original donors once per exact lock."""
    store = module('component-store')
    paths = ['browser/browser/components/blocker/assets/SOURCES.lock.json',
             'browser/browser/components/blocker/assets/resources',
             'browser/browser/components/blocker/scripts/update-bundled-assets.js',
             'browser/browser/components/blocker/scripts/verify-bundled-resource-source.js',
             'browser/taskarian/scripts/blocker_asset_provenance.py',
             'browser/taskarian/BLOCKER-ASSET-SOURCE-NOTICE']
    listing = subprocess.check_output(['git', 'ls-tree', '-r', '-z', revision, '--', *paths], cwd=ROOT)
    wanted = {'schema': 1, 'sourceInputsSha256': hashlib.sha256(listing).hexdigest(), 'target': target}
    hit = store.lookup('blocker-source', target, wanted)
    if hit:
        return hit
    lock = json.loads(text(revision, paths[0]))
    baseline = module('source-delivery')
    module('browser-component').ensure_source(baseline.BASE_SOURCE)
    if lock == json.loads(text(baseline.BASE_SOURCE, paths[0])):
        original = store.lookup('corresponding-source', 'linux-amd64', {
            'source': baseline.BASE_SOURCE, 'productVersion': baseline.BASE_VERSION, 'target': 'linux-amd64'})
        if not original or original['manifestSha256'] != baseline.BASE_MANIFEST_SHA256:
            raise store.StoreUnavailable('The permanent complete blocker source baseline is unavailable')
        archive = next(item for item in original['manifest']['assets'] if item['name'] == lock['artifact'])
        base_url = original['url'].rsplit('/', 1)[0] + '/'
        with tempfile.TemporaryDirectory(prefix='blocker-source-reference-') as temporary:
            output = Path(temporary)
            metadata = {'schema': 1, 'inputs': wanted, 'lock': lock, 'originalSource': baseline.BASE_SOURCE,
                        'sourceArchive': {**archive, 'parts': [
                            {**part, 'url': base_url + part['asset']} for part in archive['parts']]}}
            (output / 'blocker-source.json').write_text(json.dumps(metadata, indent=2) + '\n')
            assets = {'blocker-source.json': 'metadata'}
            for name, path, checksum in (
                ('Taskarian-LICENSE.txt', 'LICENSE', None),
                ('Brave-adblock-resources-LICENSE', 'browser/taskarian/android/notices/maven/texts/MPL-2.0.txt',
                 lock['sources']['braveAdblockResources']['licenseSha256']),
                ('uBlockOrigin-LICENSE.txt', 'browser/taskarian/android/notices/ublock-LICENSE',
                 lock['sources']['uBlockOrigin']['licenseSha256'])):
                data = subprocess.check_output(['git', 'show', revision + ':' + path], cwd=ROOT)
                if checksum and hashlib.sha256(data).hexdigest() != checksum:
                    raise ValueError('Retained blocker original license differs from its exact source lock')
                (output / name).write_bytes(data)
                assets[name] = 'legal'
            return store.publish('blocker-source', target, lock['sources']['uBlockOrigin']['tag'], wanted, output,
                                 assets=assets, source_commit=revision, dependencies=[reference(original)])
    with tempfile.TemporaryDirectory(prefix='blocker-source-') as temporary:
        temporary = Path(temporary)
        source = temporary / 'source.tar.gz'
        module('source_archive').archive_revision(ROOT, revision, source, prefix='', paths=paths)
        with tarfile.open(source) as archive:
            archive.extractall(temporary / 'inputs', filter='data')
        output = temporary / 'output'
        output.mkdir()
        script = temporary / 'inputs/browser/taskarian/scripts/blocker_asset_provenance.py'
        subprocess.run(['python3', '-I', '-B', str(script), 'build',
                        '--repository', str(temporary / 'inputs/browser'),
                        '--cache-dir', str(Path(os.environ['RUNNER_TEMP']) / 'blocker-source-cache'),
                        '--output', str(output / lock['artifact']), '--skip-reproduction-check'], check=True)
        notices = [text(revision, 'LICENSE')]
        with tarfile.open(output / lock['artifact']) as archive:
            for member in archive:
                if member.isfile() and '/licenses/' in member.name:
                    notices.append(member.name + '\n\n' + archive.extractfile(member).read().decode())
        if len(notices) != 3:
            raise ValueError('Blocker source archive must retain both full original license texts')
        (output / 'blocker-source-NOTICES.txt').write_text('\n\n'.join(notices))
        (output / 'blocker-source-lock.json').write_text(json.dumps(lock, indent=2) + '\n')
        return store.publish('blocker-source', target, lock['sources']['uBlockOrigin']['tag'], wanted, output,
                             assets={lock['artifact']: 'source', 'blocker-source-NOTICES.txt': 'legal',
                                     'blocker-source-lock.json': 'metadata'}, source_commit=revision)


def android_source_inputs(target, revision=None):
    revision = revision or source()
    native = module('native-component')
    inventory = 'agent/vendor/native/android-sources.json'
    pins = 'browser/taskarian/android/sources/sources.json'
    records = json.loads(native.content(revision, inventory))
    paths = sorted({pins, inventory, *(record['versionPin']['file'] for record in records if 'versionPin' in record)})
    return {'schema': 1, 'target': target, 'files': {path: native.entry(revision, path) for path in paths},
            'pinsSha256': hashlib.sha256(subprocess.check_output(['git', 'show', revision + ':' + pins], cwd=ROOT)).hexdigest()}


def android_source_selection(result, target, directory):
    directory = Path(directory)
    store = module('component-store')
    metadata = store.fetch(result, directory, roles=('metadata',), names=['android-library-source.json'])
    if not metadata:
        raise store.StoreUnavailable('The published Android library source metadata is unavailable')
    record = json.loads((directory / 'android-library-source.json').read_text())
    wanted = android_source_inputs(target)
    if record.get('schema') != 1 or record.get('inputs') != wanted:
        raise ValueError('Published Android library source inputs differ')
    selection = {'schema': 1, 'inputs': wanted, 'release': reference(result), 'metadata': record}
    file = directory / 'library-source-reference.json'
    file.write_text(json.dumps(selection, indent=2) + '\n')
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as output:
            output.write('TASKARIAN_ANDROID_LIBRARY_SOURCE_REFERENCE=' + str(file.resolve()) + '\n')
    return selection


def prepare_android_sources(target, directory):
    """Reuse only source metadata; seed unchanged originals by permanent reference."""
    store = module('component-store')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as output:
            output.write('TASKARIAN_ANDROID_LIBRARY_SOURCE_DIRECTORY=' + str(directory.resolve()) + '\n')
    wanted = android_source_inputs(target)
    hit = store.lookup('android-library-source', target, wanted)
    if hit:
        return android_source_selection(hit, target, directory)
    baseline = module('source-delivery')
    module('browser-component').ensure_source(baseline.BASE_SOURCE)
    if wanted != android_source_inputs(target, baseline.BASE_SOURCE):
        print('Android dependency source pins changed; publish matching originals once after resolution', flush=True)
        return None
    application = store.lookup('application', target, {'source': baseline.BASE_SOURCE,
        'productVersion': baseline.BASE_VERSION, 'target': target})
    if not application:
        raise store.StoreUnavailable('The permanent Android source baseline is unavailable; retry without downloading sources')
    ref = next(ref for ref in application['manifest']['dependencies']
               if ref['manifestName'].startswith('component-browser-resources-' + target + '-'))
    with tempfile.TemporaryDirectory(prefix='android-source-metadata-') as temporary:
        temporary = Path(temporary)
        original = store.fetch(ref, temporary, roles=('runtime',), names=['build-manifest.json'])
        if not original:
            raise store.StoreUnavailable('The complete verified Android source owner is unavailable')
        built = json.loads((temporary / 'build-manifest.json').read_text())
        abi = 'x86_64' if target.endswith('x86_64') else 'arm64-v8a'
        archive = next(item for item in original['manifest']['assets']
                       if item['name'] == 'taskarian-android-library-source_' + abi + '.tar.gz')
        base_url = original['url'].rsplit('/', 1)[0] + '/'
        metadata = {'schema': 1, 'inputs': wanted, 'nativeSourceComponents': built['native_source_components'],
                    'sourceArchive': {**archive, 'role': 'source', 'parts': [
                        {**part, 'url': base_url + part['asset']} for part in archive['parts']]},
                    'originalSource': baseline.BASE_SOURCE}
        output = temporary / 'output'
        output.mkdir()
        (output / 'android-library-source.json').write_text(json.dumps(metadata, indent=2) + '\n')
        (output / 'Taskarian-LICENSE.txt').write_bytes(subprocess.check_output(['git', 'show', source() + ':LICENSE'], cwd=ROOT))
        hit = store.publish('android-library-source', target, version(source(), True), wanted, output,
                            assets={'android-library-source.json': 'metadata', 'Taskarian-LICENSE.txt': 'legal'},
                            source_commit=source(), dependencies=[reference(original)])
    return android_source_selection(hit, target, directory)


def publish_android_sources(target, directory, sources, source_manifest, apk):
    """Package changed Maven/native originals once; ordinary APKs only link them."""
    store, native_sources = module('component-store'), module('android_native_sources')
    wanted = android_source_inputs(target)
    if store.lookup('android-library-source', target, wanted):
        return prepare_android_sources(target, directory)
    abi = 'x86_64' if target.endswith('x86_64') else 'arm64-v8a'
    with tempfile.TemporaryDirectory(prefix='android-library-source-') as temporary:
        output = Path(temporary)
        name = 'taskarian-android-library-source_' + abi + '.tar.gz'
        with tarfile.open(output / name, 'w:gz') as archive:
            archive.add(sources, arcname='android-library-sources')
            archive.add(ROOT / 'LICENSE', arcname='Taskarian-LICENSE')
            components = native_sources.collect(lambda name: (ROOT / name).read_bytes(), archive,
                source_manifest=source_manifest, apk=apk,
                fetch_source=lambda record, name: module('source_cache').fetch(
                    native_sources.download_pin(record), local=None if record.get('member') else ROOT / name))
        metadata = {'schema': 1, 'inputs': wanted, 'nativeSourceComponents': components,
                    'sourceArchive': {'name': name, 'size': (output / name).stat().st_size, 'sha256': store.sha(output / name)}}
        (output / 'android-library-source.json').write_text(json.dumps(metadata, indent=2) + '\n')
        notices = [(ROOT / 'LICENSE').read_text(), apk.read('assets/raw/third_party_licenses').decode()]
        notices += [apk.read('assets/' + name + '.txt').decode() for name in
                    ('THIRD-PARTY-NOTICES', 'TASKARIAN-NOTICES', 'TOR-NOTICES', 'BLOCKER-NOTICES', 'QR-NOTICES')]
        (output / 'android-library-source-NOTICES.txt').write_text('\n\n'.join(notices))
        hit = store.publish('android-library-source', target, version(source(), True), wanted, output,
                            assets={name: 'source', 'android-library-source.json': 'metadata',
                                    'android-library-source-NOTICES.txt': 'legal'}, source_commit=source())
    return android_source_selection(hit, target, directory)


def checkpoint_proof(directory, target):
    browser = module('browser-component')
    repository, run_id = os.environ['GITHUB_REPOSITORY'], int(os.environ['GITHUB_RUN_ID'])
    run = browser.api(f'repos/{repository}/actions/runs/{run_id}')
    if target.startswith('android'):
        evidence = module('android-native-component').early_producer(repository, run)
    else:
        evidence = browser.completed_producer(repository, run, target)
    if not evidence:
        raise ValueError('Native build/verify/upload checkpoint has not completed')
    manifest = directory / ('native-gecko.json' if target.startswith('android') else 'browser-artifact-manifest.txt')
    archive = directory / 'gecko-libraries.apk' if target.startswith('android') else next(directory.glob('taskarian*.tar.*'))
    return {'repository': repository, 'run': run_id, 'source': source(), 'runSource': source(),
            'archive': archive.name, 'archiveSha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'manifestSha256': hashlib.sha256(manifest.read_bytes()).hexdigest(), 'steps': evidence}


def packaged_legal(directory, staging, target):
    captured = list(Path(directory).glob('gecko-built-license-*.html'))
    if captured:
        for page in captured:
            destination = staging / page.name
            if not destination.exists() or not page.samefile(destination):
                shutil.copy2(page, destination)
        return {page.name: 'legal' for page in captured}
    archives = []
    if target.startswith('linux'):
        package, = Path(directory).glob('taskarian*.tar.*')
        with tarfile.open(package) as source_archive:
            for member in source_archive:
                if member.name in ('taskarian/omni.ja', 'taskarian/browser/omni.ja'):
                    archives.append((member.name, source_archive.extractfile(member).read()))
    else:
        apks = list(Path(directory).glob('taskarian_*.apk'))
        if apks:
            with zipfile.ZipFile(apks[0]) as app:
                archives.append(('omni.ja', app.read('assets/omni.ja')))
        else:
            obj = Path(os.environ['TASKARIAN_ANDROID_OBJDIR'])
            for omni in (obj / 'dist/geckoview').rglob('omni.ja'):
                archives.append((omni.name, omni.read_bytes()))
    result = {}
    for index, (_, contents) in enumerate(archives):
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            for name in archive.namelist():
                if name.endswith('/license.html'):
                    output = f'browser-full-license-{index}-{len(result)}.html'
                    (staging / output).write_bytes(archive.read(name))
                    result[output] = 'legal'
    if not result:
        raise ValueError('Native browser package lacks its complete built license page')
    return result


def publish(kind, directory, target):
    store = module('component-store')
    directory = Path(directory)
    revision = source()
    identity = inputs(kind, target)
    if kind != 'gecko' and target.startswith('android'):
        app, = directory.glob('taskarian_*.apk')
        release = module('release')
        with zipfile.ZipFile(app) as package:
            pairing = release.validate_termux_update(json.loads(package.read(release.TERMUX_UPDATE_ASSET)))
        identity = inputs(kind, target, companion=pairing)
    found = store.lookup(kind, target, identity)
    if found:
        remember(found, directory, kind)
        if kind == 'browser-resources' and target.startswith('android'):
            compiled = module('android-apk-module').publish(directory, target, found)
            if compiled:
                remember(compiled, directory, 'android-apk-module')
        return found
    producer = None
    if kind == 'gecko':
        receipt = os.environ.get('TASKARIAN_LINUX_NATIVE_RECEIPT') if target.startswith('linux') else None
        if receipt:
            origin = json.loads(Path(receipt).read_text())
            directory = Path(receipt).parent / 'producer'
            revision, producer = origin['source'], origin['producer']
        elif target.startswith('android') and os.environ.get('TASKARIAN_ANDROID_NATIVE_RECEIPT'):
            origin = json.loads(Path(os.environ['TASKARIAN_ANDROID_NATIVE_RECEIPT']).read_text())
            revision, producer = origin['source'], origin.get('producer')
            if producer is None:
                raise ValueError('Borrowed Android engine lacks its original producer')
        else:
            producer = checkpoint_proof(directory, target)
        if inputs(kind, target, revision) != identity:
            raise ValueError('Native source does not match the requested compiler inputs')
    with tempfile.TemporaryDirectory(prefix='browser-release-') as temporary:
        staging = Path(temporary)
        assets = {}
        for file in directory.iterdir():
            if file.is_file():
                try:
                    os.link(file, staging / file.name)
                except OSError:
                    shutil.copy2(file, staging / file.name)
                assets[file.name] = ('source' if '-source' in file.name and any(ext in file.name for ext in ('.tar', '.jar')) else 'runtime')
        source_assets, source_dependencies = source_legal(staging, revision, target, kind)
        assets.update(source_assets)
        assets.update(packaged_legal(directory, staging, target))
        dependencies = source_dependencies
        if kind != 'gecko' and os.environ.get('TASKARIAN_ANDROID_LIBRARY_SOURCE_REFERENCE'):
            dependencies.append(json.loads(Path(os.environ['TASKARIAN_ANDROID_LIBRARY_SOURCE_REFERENCE']).read_text())['release'])
        ref = os.environ.get('TASKARIAN_GECKO_RELEASE_REFERENCE')
        if kind != 'gecko' and ref:
            dependencies.append(json.loads(Path(ref).read_text()))
        ref = os.environ.get('TASKARIAN_GECKOVIEW_RELEASE_REFERENCE')
        if kind != 'gecko' and ref:
            dependencies.append(json.loads(Path(ref).read_text()))
        ref = os.environ.get('TASKARIAN_ANDROID_APK_MODULE_RELEASE_REFERENCE')
        if kind != 'gecko' and ref:
            dependencies.append(json.loads(Path(ref).read_text()))
        result = store.publish(kind, target, version(revision, kind == 'gecko'), identity, staging,
                               assets=assets, source_commit=revision, producer=producer, dependencies=dependencies)
    remember(result, directory, kind)
    if kind == 'browser-resources' and target.startswith('android'):
        compiled = module('android-apk-module').publish(directory, target, result)
        if compiled:
            remember(compiled, directory, 'android-apk-module')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inputs', 'native-key', 'require-engine', 'cache-contract', 'restore-cache', 'restore-engine', 'seed-engine', 'publish-engine',
                                         'publish-resources', 'android-companion', 'android-sources', 'dependencies'])
    parser.add_argument('target')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--producer-source')
    parser.add_argument('--producer-run', type=int)
    parser.add_argument('--producer-artifact', type=int)
    args = parser.parse_args()
    if args.action == 'inputs':
        print(json.dumps(inputs('gecko', args.target), indent=2)); return
    if args.action == 'native-key':
        print(native_key(args.target)); return
    if args.action == 'require-engine':
        if not engine_available(args.target):
            raise ValueError('Required Gecko component is not published; complete its engine_only producer first')
        print('Required canonical Gecko component is published'); return
    if args.action == 'cache-contract':
        value = cache_contract(args.target)
        args.output.write_text(json.dumps(value, indent=2) + '\n')
        if os.environ.get('GITHUB_OUTPUT'):
            legacy = hashlib.sha256(module('component-store').canonical(legacy_cache_contract(args.target))).hexdigest()
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
                output.write('legacy_digest=' + legacy + '\n')
        print(hashlib.sha256(module('component-store').canonical(value)).hexdigest()); return
    if args.action == 'restore-cache':
        print(json.dumps({'hit': restore_cache(args.directory, args.target)})); return
    if args.action == 'seed-engine':
        if not all((args.directory, args.producer_source, args.producer_run, args.producer_artifact)):
            parser.error('seed-engine requires --directory and an exact --producer-source/run/artifact')
        module('android-native-component').seed_checkpoint(args.directory, args.target, args.producer_source,
                                                          args.producer_run, args.producer_artifact)
        publish('gecko', args.directory, args.target); return
    if args.action == 'android-companion':
        android_companion(args.directory, args.target); return
    if args.action == 'android-sources':
        prepare_android_sources(args.target, args.directory); return
    if args.action == 'dependencies':
        args.output.write_text(json.dumps(dependencies([args.directory]), indent=2) + '\n'); return
    if args.action == 'restore-engine':
        hit = restore_engine(args.directory, args.target)
        print(json.dumps({'hit': hit})); return
    publish('gecko' if args.action == 'publish-engine' else 'browser-resources', args.directory, args.target)


if __name__ == '__main__':
    main()
