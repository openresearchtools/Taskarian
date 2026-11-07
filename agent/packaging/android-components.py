#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent existing Android AAR/JNI owners in the architecture builders."""
import argparse
from functools import cache
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import tarfile
import tempfile
import time
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ('android-remote', 'android-tor', 'android-x11')
REMOTE_METADATA = 'auth/dependencies/go/android-remote.json'


@cache
def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


native, store = module('native-component'), module('component-store')


def head():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()


def abi(target):
    return 'x86_64' if target.endswith('x86_64') else 'arm64-v8a'


def inputs(component, target, revision=None):
    if component not in COMPONENTS:
        raise ValueError('Unknown Android native leaf')
    revision = revision or head()
    paths = ['LICENSE', 'agent/packaging/source_cache.py']
    records = {'jdk': 17, 'abi': abi(target)}
    if component == 'android-remote':
        records.update(androidApi=31, ndk='29.0.14206865')
        sources = native.go_sources(revision, 'agent/native/remote/android')
        paths += [*sources, 'auth/build/remote-android.sh', 'auth/build/go-env.sh', 'auth/build/go-notices.py',
                  'auth/dependencies/go-toolchain', REMOTE_METADATA]
        records['goPins'] = native.go_pins(revision, sources)
        records['moduleListingMetadata'] = (json.loads(native.content(revision, REMOTE_METADATA))['moduleMetadata']
                                           if native.entry(revision, REMOTE_METADATA) else [])
    elif component == 'android-tor':
        paths += ['browser/taskarian/android/tor', 'browser/third_party/tor', 'browser/taskarian/scripts/tor-runtime-provenance.py',
                  'browser/taskarian/android/scripts/tor.py',
                  'browser/taskarian/third_party/tor.toml', 'browser/taskarian/android/notices']
        pins = json.loads(native.content(revision, 'browser/taskarian/android/tor/pins.json'))
        records['ndk'] = pins['ndkVersion']
        retained = json.loads(native.content(revision, 'agent/vendor/native/android-sources.json'))
        records['dependencies'] = [item for item in retained if item['file'] in pins['dependencies'].values()]
        records['ndkSource'] = json.loads(native.content(revision, 'agent/packaging/node/lock.json'))['termux']['dependency_sources']['libc++']
    else:
        records.update(androidApi=31, ndk='29.0.14206865')
        paths += ['browser/mobile/android/taskarian/display/' + name for name in
                  ('build.gradle', 'prepare.py', 'CMakeLists.txt', 'patches', 'consumer-rules.pro', 'src')]
        paths += ['browser/mobile/android/taskarian-sdk',
                  'browser/taskarian/vendor/termux-x11', 'browser/taskarian/vendor/display-dependencies',
                  'browser/settings.gradle', 'browser/build.gradle', 'browser/gradle.properties',
                  'browser/gradle', 'browser/mobile/android/gradle.configure',
                  'browser/build/moz.configure/android-sdk.configure',
                  'browser/mobile/android/android-components/.config.yml']
        records['cmake'] = '3.22.1'
    if component == 'android-remote':
        layout = native.entry(revision, 'agent/packaging/runtime-layout.py')
        if layout:
            records['runtimeLayout'] = {'version': 1, 'recipe': layout}
    files = {path: native.entry(revision, path) for path in paths}
    if component == 'android-x11':
        # The exact reviewed Maven consumer block is inactive in this native
        # producer, just as it is in the Gecko producer. Keep all other bytes.
        browser = module('browser-component')
        browser.ensure_source(revision)
        files['browser/build.gradle'] = browser.blob_identity(browser.native_gradle_base(
            browser.network_command(['git', 'show', revision + ':browser/build.gradle'], binary=True)))
    return {'schema': 1, 'component': component, 'abi': abi(target), 'records': records, 'files': files}


component_inputs = inputs


def binary_inputs(component, target, revision=None):
    """Compare actual compiler inputs independently of source-only packaging."""
    value = inputs(component, target, revision)
    value['records'].pop('runtimeLayout', None)
    return value


def version(component, target, revision=None):
    revision = revision or head()
    if component == 'android-tor':
        return tomllib.loads(native.content(revision, 'browser/taskarian/third_party/tor.toml'))['version']
    if component == 'android-x11':
        return json.loads(native.content(revision, 'browser/taskarian/vendor/termux-x11/source.json'))['version']
    return native.content(revision, 'browser/taskarian/config/version.txt').strip()


def restore(component, target, directory):
    directory = Path(directory)
    result = store.restore(component, target, inputs(component, target), directory, roles=('runtime', 'metadata'))
    if result is None:
        return None
    archive = directory / (component + '.tar.gz')
    with tarfile.open(archive) as bundle:
        bundle.extractall(directory, filter='data')
    verify_payload(component, target, directory)
    receipt = {'schema': 1, 'component': component, 'inputs': inputs(component, target),
               'producerSource': result['manifest']['sourceCommit'], 'requestedSource': head(),
               'release': {key: result[key] for key in ('repository', 'manifestName', 'manifestSha256')}}
    (directory / 'component-release.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return result


def verify_payload(component, target, directory):
    directory = Path(directory)
    if component == 'android-tor':
        target_dir = directory / abi(target)
        record = json.loads((target_dir / 'tor-android.json').read_text())
        binary = target_dir / 'libtor.so'
        if (record.get('abi') != abi(target) or record.get('version') != version(component, target) or
                record.get('binarySha256') != store.sha(binary)):
            raise ValueError('Tor native component identity differs from its payload')
        payloads = [binary.read_bytes()]
    else:
        archive = directory / ('taskarian-remote.aar' if component == 'android-remote' else 'taskarian-display.aar')
        with zipfile.ZipFile(archive) as package:
            libraries = [name for name in package.namelist() if name.startswith('jni/') and name.endswith('.so')]
            if not libraries or any(not name.startswith('jni/' + abi(target) + '/') for name in libraries):
                raise ValueError('Android native library payload has the wrong ABI')
            payloads = [package.read(name) for name in libraries]
    machine = 62 if abi(target) == 'x86_64' else 183
    for data in payloads:
        if data[:6] != b'\x7fELF\x02\x01' or len(data) < 64 or struct.unpack_from('<H', data, 18)[0] != machine:
            raise ValueError('Android native component has an invalid ELF target')
        offset = struct.unpack_from('<Q', data, 32)[0]
        size, count = struct.unpack_from('<HH', data, 54)
        if size < 56 or offset + size * count > len(data):
            raise ValueError('Invalid Android native ELF program headers')
        loads = 0
        for index in range(count):
            entry = offset + index * size
            if struct.unpack_from('<I', data, entry)[0] == 1:
                loads += 1
                position, address = struct.unpack_from('<QQ', data, entry + 8)
                alignment = struct.unpack_from('<Q', data, entry + 48)[0]
                if alignment < 16384 or position % 16384 != address % 16384:
                    raise ValueError('Android native component lacks 16 KB page alignment')
        if not loads:
            raise ValueError('Android native component has no loadable segments')


def materialize(component, target):
    values = inputs(component, target)
    paths = [path for path, value in values['files'].items() if value]
    paths += ['agent/packaging', 'auth/build', 'auth/dependencies/go-toolchain']
    if component == 'android-remote':
        # The existing offline proxy validates its shared inventory before Go
        # resolves this component's locked closure. Retain those small records.
        retained = json.loads(native.content(head(), 'auth/dependencies/go/sources.json'))
        paths += ['auth/dependencies/go/sources.json', *retained['locks']]
        pins = [pin for entry in retained['modules'] for pin in entry['files'].values()]
        pins += retained['moduleMetadata']
        pins += values['records']['moduleListingMetadata']
        paths += ['auth/dependencies/go/' + pin['file'] for pin in pins
                  if pin.get('file', '').startswith('proxy/') and pin['file'].endswith(('.mod', '.info'))]
    if component == 'android-x11':
        paths += ['browser']  # Native mach/Gradle configuration reads its actual source graph.
    env = {**os.environ, 'GIT_LFS_SKIP_SMUDGE': '1'}
    subprocess.run(['git', 'sparse-checkout', 'add', *('/' + path for path in paths)], cwd=ROOT, env=env, check=True)


def setup(component, target, work):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != store.repository(target):
        raise ValueError('Android native compilation runs only in its architecture builder')
    materialize(component, target)
    subprocess.run(['sudo', 'apt-get', 'update'], check=True)
    subprocess.run(['sudo', 'apt-get', 'install', '-y', 'build-essential', 'autoconf', 'automake', 'libtool',
                    'pkg-config', 'python3-dev', 'python3-venv', 'ninja-build', 'unzip', 'zip', 'zstd'], check=True)
    jdk = Path(os.environ.get('JAVA_HOME_17_X64', os.environ.get('JAVA_HOME', '')))
    if not (jdk / 'bin/java').is_file():
        raise ValueError('Pinned Java17 worker toolchain is missing')
    if not subprocess.check_output([str(jdk / 'bin/javac'), '-version'], text=True, stderr=subprocess.STDOUT).startswith('javac 17.'):
        raise ValueError('Android native owner requires the actual Java17 toolchain')
    os.environ['JAVA_HOME'] = str(jdk)
    os.environ['PATH'] = str(jdk / 'bin') + os.pathsep + os.environ['PATH']
    os.environ['TASKARIAN_ANDROID_ABI'] = abi(target)
    if component == 'android-tor':
        return  # Guardian's native owner fetches and verifies its actual NDK r30.
    sdk = Path(os.environ['ANDROID_HOME'])
    manager = sdk / 'cmdline-tools/latest/bin/sdkmanager'
    subprocess.run([str(manager), 'platforms;android-31', 'platforms;android-37.0',
                    'build-tools;37.0.0', 'ndk;29.0.14206865', 'cmake;3.22.1'], check=True)
    os.environ['ANDROID_NDK_HOME'] = str(sdk / 'ndk/29.0.14206865')


def prepare_remote_metadata(records):
    # gomobile lists the complete module graph, including these pinned .info
    # records which are not needed by the other native Go package closures.
    sources = ROOT / 'auth/dependencies/go'
    cache = module('source_cache')
    spec = importlib.util.spec_from_file_location('retained_go_sources', ROOT / 'auth/build/retained-sources.py')
    retained = importlib.util.module_from_spec(spec); spec.loader.exec_module(retained)
    proxy = cache.default_cache() / 'auth-go-proxy' / store.sha(sources / 'sources.json')
    for pin in records['moduleListingMetadata']:
        original = retained.checked(sources, pin)
        retained.stage_file(original, proxy / Path(pin['file']).relative_to('proxy'), pin['sha256'])


def run(component, target, work, output, force=False):
    work, output = Path(work).resolve(), Path(output).resolve()
    work.mkdir(parents=True, exist_ok=True); output.mkdir(parents=True, exist_ok=True)
    if not force:
        previous = restore(component, target, output)
        if previous:
            return previous
    setup(component, target, work)
    runtime = work / 'runtime'; runtime.mkdir(exist_ok=True)
    if component == 'android-remote':
        prepare_remote_metadata(inputs(component, target)['records'])
        subprocess.run(['bash', str(ROOT / 'auth/build/remote-android.sh'), str(runtime)], cwd=ROOT, check=True)
    elif component == 'android-tor':
        subprocess.run(['python3', str(ROOT / 'browser/taskarian/android/tor/build.py'), '--abi', abi(target),
                        '--jdk', os.environ['JAVA_HOME'], '--work', str(work / 'tor'), '--output', str(runtime), '--jobs', '2'], cwd=ROOT, check=True)
    else:
        obj = work / 'obj'
        mozconfig = work / 'mozconfig-artifact'
        mozconfig.write_text(module('android-native-component').artifact_mozconfig())
        env = {**os.environ, 'TASKARIAN_ANDROID_OBJDIR': str(obj),
               'MOZCONFIG': str(mozconfig),
               'MOZBUILD_STATE_PATH': str(work / 'mozbuild'), 'SCCACHE_PATH': ''}
        subprocess.run(['./mach', '--no-interactive', 'bootstrap', '--application-choice', 'mobile_android'], cwd=ROOT / 'browser', env=env, check=True)
        subprocess.run(['./mach', 'configure'], cwd=ROOT / 'browser', env=env, check=True)
        subprocess.run(['./mach', 'build', '--allow-subdirectory-build', '-C', '.', 'buildid.h'],
                       cwd=ROOT / 'browser', env=env, check=True)
        subprocess.run(['./mach', 'gradle', ':taskarian-display:assembleRelease'], cwd=ROOT / 'browser', env=env, check=True)
        matches = list((obj / 'gradle/build').rglob('display-release.aar'))
        if not matches:
            matches = list((obj / 'gradle/build').rglob('*display*release.aar'))
        if len(matches) != 1:
            raise ValueError('Expected the one native display AAR')
        shutil.copy2(matches[0], runtime / 'taskarian-display.aar')
    verify_payload(component, target, runtime)
    archive = output / (component + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.add(runtime, arcname='.')
    from source_archive import archive_revision
    paths = [path for path, value in inputs(component, target)['files'].items() if value]
    paths += ['agent/packaging/android-components.py', 'agent/packaging/native-component.py',
              'agent/packaging/component-store.py', 'agent/packaging/native-tools.py',
              'agent/packaging/source_archive.py', 'auth/build/retained-sources.py']
    tracked = work / (component + '-tracked-source.tar.gz')
    archive_revision(ROOT, head(), tracked, paths=paths)
    originals = []
    records = inputs(component, target)['records']
    fetch = module('source_cache').fetch
    if component == 'android-remote':
        originals += list((runtime / 'share/source').glob('*'))
        originals += list(runtime.glob('*sources.jar'))
        pins = [pin for entry in records['goPins']['modules'] for pin in entry['files'].values()]
        pins += records['goPins']['moduleMetadata']
        pins += records['moduleListingMetadata']
        for pin in pins:
            local = None
            if pin.get('file', '').startswith('proxy/'):
                if pin['file'].endswith(('.mod', '.info')):
                    local = ROOT / 'auth/dependencies/go' / pin['file']
                if not pin.get('url'):
                    pin = {**pin, 'url': 'https://proxy.golang.org/' + pin['file'][6:]}
            originals.append(fetch(pin, local=local))
    elif component == 'android-tor':
        download_pin = module('android_native_sources').download_pin
        originals += [fetch(download_pin(pin)) for pin in records['dependencies']]
        originals += list((work / 'tor').rglob('tor-*-pristine.tar'))
    else:
        donor = json.loads((ROOT / 'browser/taskarian/vendor/termux-x11/source.json').read_text())
        originals.append(fetch(donor['sourceArchive']))
        for name in ('libtirpc', 'libx11', 'xserver'):
            pin = json.loads((ROOT / 'browser/taskarian/vendor/display-dependencies' / name / 'source.json').read_text())
            originals.append(fetch(pin['archive']))
    source = output / (component + '-source.tar.gz')
    with tarfile.open(source, 'w:gz') as bundle:
        bundle.add(tracked, arcname=tracked.name)
        seen = set()
        for original in originals:
            checksum = store.sha(original)
            if checksum not in seen:
                bundle.add(original, arcname='originals/' + checksum + '-' + Path(original).name)
                seen.add(checksum)
    legal = output / (component + '-licenses.tar.gz')
    with tarfile.open(legal, 'w:gz') as bundle:
        bundle.add(ROOT / 'LICENSE', arcname='Taskarian-LICENSE')
        for path in paths:
            candidate = ROOT / path
            if candidate.is_dir():
                for file in candidate.rglob('*'):
                    if file.is_file() and file.name.lower().startswith(('license', 'copying', 'notice', 'copyright')):
                        bundle.add(file, arcname=file.relative_to(ROOT))
        if (runtime / 'share/licenses').is_dir():
            bundle.add(runtime / 'share/licenses', arcname='built-runtime-licenses')
    result = store.publish(component, target, version(component, target), inputs(component, target), output,
                           assets={archive.name: 'runtime', source.name: 'source', legal.name: 'legal'}, source_commit=head())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inputs', 'restore', 'run'))
    parser.add_argument('component', choices=COMPONENTS); parser.add_argument('target')
    parser.add_argument('--directory', type=Path); parser.add_argument('--work', type=Path); parser.add_argument('--output', type=Path)
    parser.add_argument('--force-rebuild', action='store_true')
    parser.add_argument('--wait', action='store_true', help='Wait for this exact native input manifest')
    args = parser.parse_args()
    if args.action == 'inputs':
        result = inputs(args.component, args.target)
    elif args.action == 'restore':
        deadline = time.monotonic() + 300 * 60
        while args.wait and store.lookup(args.component, args.target, inputs(args.component, args.target)) is None:
            if time.monotonic() >= deadline:
                raise SystemExit('Timed out waiting for the exact Android native component')
            print('Waiting for ' + args.component + ' durable manifest', flush=True)
            time.sleep(30)
        result = restore(args.component, args.target, args.directory)
        if result is None:
            raise SystemExit('Required Android native component has not been published')
    else:
        result = run(args.component, args.target, args.work, args.output, args.force_rebuild)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
