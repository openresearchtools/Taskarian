#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Reuse the complete pinned GeckoView Maven module through component releases."""
import argparse
import ast
from functools import cache
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from importlib.util import module_from_spec, spec_from_file_location

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = 'android-geckoview'
RUNTIME = 'geckoview-maven-runtime.tar.gz'
RECEIPT = 'geckoview-module.json'
PUBLICATION = ':geckoview:publishDebugPublicationToMavenRepository'
APP_GENERATED = (
    'mobile/android/android-components/components/lib/crash/src/main/java/mozilla/components/lib/crash/service/CrashReport.kt',
    'mobile/android/webcompat_addon_generated_files',
)


@cache
def module(name):
    spec = spec_from_file_location('geckoview_' + name.replace('-', '_'), ROOT / 'agent/packaging' / (name + '.py'))
    value = module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def git(*args, binary=False):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=not binary)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inputs(target, revision=None):
    release, browser = module('browser-release'), module('browser-component')
    revision = revision or release.source()
    native = release.inputs('gecko', target, revision)
    # The native identity already covers GeckoView Java, all upstream build
    # definitions, generated app sources and upstream chrome. Add the Taskarian
    # resources which the native owner correctly excludes from compilation.
    resources = {path for path in browser.packaged_resources(revision)
                 if not path.startswith(('browser/taskarian/browser/',
                                          *module('android-native-component').NATIVE_LEAF_PATHS))}
    resources.update((browser.ANDROID_ONLY_MODULE, 'browser/toolkit/content/license.html',
                      'browser/browser/base/content/overrides/app-license-body.html'))
    # Android explicitly imports this desktop-owned module into its own omni.
    # Resolve absolute EXTRA_JS_MODULES entries from the pinned native owner.
    owner = 'browser/mobile/shared/modules/geckoview/moz.build'
    for node in ast.walk(ast.parse(git('show', revision + ':' + owner))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith('/taskarian/'):
            resources.add('browser' + node.value)
    listing = git('ls-tree', '-r', '-z', revision, '--', *sorted(resources), binary=True)
    return {'schema': 'taskarian-android-geckoview-1', 'target': target,
            'nativeEngine': native, 'engineVersion': release.version(revision, True),
            'resourceSha256': hashlib.sha256(listing).hexdigest(),
            'runtimeLayout': 1, 'publicationTask': PUBLICATION}


def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
            stream.write(f'{name}={value}\n')


def configure(directory, record, select_mode=True):
    """Select the original Maven coordinate while preserving the current app ID."""
    native = module('android-native-component')
    mozconfig = directory / 'mozconfig'
    mozconfig.write_text(native.artifact_mozconfig())
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as stream:
            if select_mode:
                stream.write(f'MOZCONFIG={mozconfig.resolve()}\n')
            stream.write(f'TASKARIAN_GECKOVIEW_MAVEN_COORDINATE={record["coordinate"]}\n')
            stream.write(f'TASKARIAN_GECKOVIEW_MAVEN_REPOSITORY={Path(os.environ["TASKARIAN_ANDROID_OBJDIR"]).resolve() / "gradle/maven"}\n')
            stream.write(f'TASKARIAN_ANDROID_NATIVE_RECEIPT={directory.resolve() / "native-gecko.json"}\n')
            stream.write(f'TASKARIAN_GECKOVIEW_MODULE_RECEIPT={directory.resolve() / RECEIPT}\n')
            # A forced native validation may retain an already canonical module.
            # APK collection must still record that module's actual producer.
            stream.write('TASKARIAN_FORCE_FULL_BUILD=0\n')
    (directory / 'native-gecko.json').write_text(json.dumps(record['nativeGecko'], indent=2) + '\n')


def remember(found, directory, record):
    release = module('browser-release')
    release.remember(found, directory, 'geckoview')
    proof = {'schema': 1, 'coordinate': record['coordinate'], 'source': record['source'],
             'engineVersion': record['engineVersion'], 'aarSha256': record['aarSha256'],
             'receiptSha256': sha(directory / RECEIPT), 'release': release.reference(found)}
    path = directory / 'verified-geckoview-source.json'
    path.write_text(json.dumps(proof, indent=2) + '\n')
    if os.environ.get('GITHUB_ENV'):
        with Path(os.environ['GITHUB_ENV']).open('a') as stream:
            stream.write(f'TASKARIAN_GECKOVIEW_SOURCE_REFERENCE={path.resolve()}\n')


def safe_members(archive):
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()) or
                not path.parts or path.parts[0] not in ('maven', 'app-generated')):
            raise ValueError('Invalid GeckoView runtime archive member: ' + member.name)
        yield member


def check_maven(repository, record, target):
    metadata = list(repository.rglob('*.module'))
    if len(metadata) != 1:
        raise ValueError('Expected one complete GeckoView Gradle module publication')
    metadata, = metadata
    component = json.loads(metadata.read_text())
    identity = component.get('component', {})
    coordinate = ':'.join(identity.get(key, '') for key in ('group', 'module', 'version'))
    if (identity.get('group') != 'org.mozilla.geckoview' or not identity.get('module', '').startswith('geckoview') or
            coordinate != record['coordinate']):
        raise ValueError('GeckoView Maven coordinate differs from its producer receipt')
    poms, aars = list(metadata.parent.glob('*.pom')), list(metadata.parent.glob('*.aar'))
    if len(poms) != 1 or len(aars) != 1:
        raise ValueError('GeckoView publication requires its native POM and complete AAR')
    pom = ET.parse(poms[0]).getroot()
    ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
    pom_identity = {'groupId': identity['group'], 'artifactId': identity['module'], 'version': identity['version']}
    if any(pom.findtext('m:' + key, namespaces=ns) != value for key, value in pom_identity.items()):
        raise ValueError('GeckoView POM and Gradle module metadata differ')
    variants = component.get('variants', [])
    if (not any(v.get('attributes', {}).get('org.gradle.usage') == 'java-api' for v in variants) or
            not any(v.get('attributes', {}).get('org.gradle.usage') == 'java-runtime' for v in variants) or
            not any(c.get('group') == 'org.mozilla.telemetry' and c.get('name') == 'glean-native'
                    for v in variants for c in v.get('capabilities', []))):
        raise ValueError('GeckoView metadata lacks its native API/runtime or Glean capabilities')
    aar = aars[0]
    aar_digest = sha(aar)
    if record.get('aarSha256') != aar_digest:
        raise ValueError('GeckoView AAR differs from its producer')
    for variant in variants:
        for artifact in variant.get('files', []):
            if not artifact.get('name', '').endswith('.aar'):
                raise ValueError('Unexpected non-runtime GeckoView Gradle variant artifact')
            if (artifact.get('size') != aar.stat().st_size or artifact.get('sha256') != aar_digest):
                raise ValueError('GeckoView Gradle variant metadata differs from its actual AAR')
    with zipfile.ZipFile(aar) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not {'AndroidManifest.xml', 'classes.jar', 'R.txt', 'assets/omni.ja'} <= set(names):
            raise ValueError('GeckoView AAR is missing classes, manifest or complete Gecko resources')
        with zipfile.ZipFile(io.BytesIO(archive.read('classes.jar'))) as classes:
            required = {'org/mozilla/geckoview/GeckoSession.class', 'org/mozilla/geckoview/GeckoRuntime.class',
                        'org/mozilla/geckoview/BuildConfig.class'}
            if not required <= set(classes.namelist()):
                raise ValueError('GeckoView AAR is missing its native Java API')
            constants = classes.read('org/mozilla/geckoview/BuildConfig.class')
            # The released ESR display pin includes "esr"; the native configure
            # fields and GeckoView BuildConfig use browser/config/version.txt.
            compiled_version = record['engineVersion'].removesuffix('esr')
            if record['buildId'].encode() not in constants or compiled_version.encode() not in constants:
                raise ValueError('GeckoView Java build ID or upstream engine version differs')
        native = io.BytesIO()
        with zipfile.ZipFile(native, 'w') as libraries:
            for name in names:
                if name.startswith('jni/') and name.endswith('.so'):
                    libraries.writestr('lib/' + name.removeprefix('jni/'), archive.read(name))
        native.seek(0)
        module('android-native-component').verify_libraries(native, record['nativeGecko'], target)
        with zipfile.ZipFile(io.BytesIO(archive.read('assets/omni.ja'))) as omni:
            if not any(name.endswith('/license.html') for name in omni.namelist()):
                raise ValueError('GeckoView resources lack the actual built full license page')


def verify(directory, target, expected):
    record = json.loads((directory / RECEIPT).read_text())
    if (record.get('schema') != 1 or record.get('inputs') != expected or
            not re.fullmatch('[0-9a-f]{40}', record.get('source', '')) or
            not re.fullmatch('[0-9]{14}', record.get('buildId', '')) or
            record.get('engineVersion') != expected['engineVersion']):
        raise ValueError('GeckoView module source, inputs or upstream version differs')
    with tempfile.TemporaryDirectory(prefix='geckoview-verify-') as temporary:
        with tarfile.open(directory / RUNTIME) as archive:
            archive.extractall(temporary, members=safe_members(archive), filter='data')
        temporary = Path(temporary)
        check_maven(temporary / 'maven', record, target)
        for relative in APP_GENERATED:
            if not (temporary / 'app-generated' / relative).exists():
                raise ValueError('GeckoView checkpoint lacks pinned generated app sources: ' + relative)
    return record


def stage(directory, target):
    expected = inputs(target)
    record = verify(directory, target, expected)
    objdir = Path(os.environ['TASKARIAN_ANDROID_OBJDIR'])
    with tempfile.TemporaryDirectory(prefix='geckoview-stage-') as temporary:
        with tarfile.open(directory / RUNTIME) as archive:
            archive.extractall(temporary, members=safe_members(archive), filter='data')
        temporary = Path(temporary)
        shutil.copytree(temporary / 'maven', objdir / 'gradle/maven', dirs_exist_ok=True)
        shutil.copytree(temporary / 'app-generated', objdir, dirs_exist_ok=True)
        # Android's existing About owner reads the actual built omni license
        # inventory from this staging path. Keep using that native owner.
        aar, = (temporary / 'maven').rglob('*.aar')
        with zipfile.ZipFile(aar) as archive:
            omni = objdir / 'dist/geckoview/assets/omni.ja'
            omni.parent.mkdir(parents=True, exist_ok=True)
            omni.write_bytes(archive.read('assets/omni.ja'))
    configure(directory, record, select_mode=False)


def restore(directory, target, select_mode=True):
    release, store = module('browser-release'), module('component-store')
    wanted = inputs(target)
    found = store.restore(COMPONENT, target, wanted, directory, roles=('runtime', 'metadata', 'legal'))
    if found is None:
        return False
    try:
        record = verify(directory, target, wanted)
        if (record['source'] != found['manifest']['sourceCommit'] or
                record['engineVersion'] != found['manifest']['version']):
            raise ValueError('GeckoView module producer or upstream version differs from its permanent release')
        configure(directory, record, select_mode=select_mode)
    except (ValueError, KeyError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise store.StoreUnavailable('The published complete GeckoView module failed verification; retry without rebuilding') from error
    remember(found, directory, record)
    print('Reused complete GeckoView Maven module:', found['url'], flush=True)
    return True


def capture(directory, target):
    """Checkpoint native upstream outputs before the engine owner clobbers."""
    release = module('browser-release')
    wanted = inputs(target)
    objdir = Path(os.environ['TASKARIAN_ANDROID_OBJDIR'])
    repository = objdir / 'gradle/maven'
    metadata = list(repository.glob('org/mozilla/geckoview/**/*.module'))
    if len(metadata) != 1:
        raise ValueError('Run the pinned upstream GeckoView Maven publication first')
    identity = json.loads(metadata[0].read_text())['component']
    coordinate = ':'.join(identity[key] for key in ('group', 'module', 'version'))
    aar, = metadata[0].parent.glob('*.aar')
    native_path = os.environ.get('TASKARIAN_ANDROID_NATIVE_RECEIPT')
    if not native_path:
        native_path = Path(os.environ['RUNNER_TEMP']) / 'android-gecko-artifact/native-gecko.json'
    native = json.loads(Path(native_path).read_text())
    build_id = (objdir / 'buildid.h').read_text().split()[2]
    record = {'schema': 1, 'inputs': wanted, 'source': release.source(),
              'engineVersion': wanted['engineVersion'], 'buildId': build_id,
              'coordinate': coordinate, 'aarSha256': sha(aar), 'nativeGecko': native}
    check_maven(repository / 'org/mozilla/geckoview', record, target)
    with tarfile.open(directory / RUNTIME, 'w:gz') as archive:
        artifact = repository / 'org/mozilla/geckoview' / identity['module']
        for path in sorted(artifact.rglob('*')):
            if not path.is_file() or '-sources.jar' in path.name or '-javadoc.jar' in path.name:
                continue
            archive.add(path, arcname='maven/' + path.relative_to(repository).as_posix(), recursive=False)
        for relative in APP_GENERATED:
            if not (objdir / relative).exists():
                raise ValueError('Pinned upstream app-generated source is missing: ' + relative)
            archive.add(objdir / relative, arcname='app-generated/' + relative)
    record['legalFiles'] = sorted(release.packaged_legal(directory, directory, target))
    (directory / RECEIPT).write_text(json.dumps(record, indent=2) + '\n')
    verify(directory, target, wanted)


def publish(directory, target):
    release, store = module('browser-release'), module('component-store')
    wanted = inputs(target)
    # Publication can be retried after a complete sibling finished. Always keep
    # that actual producer and its build date instead of publishing new bytes.
    if store.lookup(COMPONENT, target, wanted):
        restore(directory, target, select_mode=False)
        stage(directory, target)
        return
    record = json.loads((directory / RECEIPT).read_text())
    engine = store.lookup('gecko', target, wanted['nativeEngine'])
    native = record['nativeGecko']
    if engine is None or engine['manifest']['sourceCommit'] != native['source']:
        raise ValueError('The complete GeckoView module requires its actual published native engine')
    engine_assets = {asset['name']: asset for asset in engine['manifest']['assets']}
    native.setdefault('producer', {
        'kind': 'component-release', 'source': native['source'], 'repository': engine['repository'],
        'run': engine['manifest']['producer']['run'], 'release': release.reference(engine),
        'manifest_sha256': engine_assets['native-gecko.json']['sha256'],
        'apk_sha256': engine_assets['gecko-libraries.apk']['sha256']})
    (directory / RECEIPT).write_text(json.dumps(record, indent=2) + '\n')
    verify(directory, target, wanted)
    assets = {RUNTIME: 'runtime', RECEIPT: 'metadata', **{name: 'legal' for name in record['legalFiles']}}
    source_assets, dependencies = release.source_legal(directory, record['source'], target, 'browser-resources')
    assets.update(source_assets)
    dependencies.append(release.reference(engine))
    # GeckoView's native Maven POM retains AndroidX and other upstream Java
    # dependencies. Reuse their complete pinned original-source owner too.
    library_sources = release.prepare_android_sources(target, directory.parent / 'geckoview-library-source')
    if library_sources is None:
        raise ValueError('Publish the changed pinned Android dependency originals before the GeckoView module')
    dependencies.append(library_sources['release'])
    found = store.publish(COMPONENT, target, record['engineVersion'], wanted, directory,
                          assets=assets, source_commit=record['source'], dependencies=dependencies)
    remember(found, directory, record)
    configure(directory, record, select_mode=False)


def check_plan(path, metadata_only=False):
    tasks = re.findall(r'^(:\S+)\s+SKIPPED\s*$', Path(path).read_text(), re.M)
    expected = ((':fenix:releaseOssLicensesTask', ':fenix:stageTaskarianDependencySources')
                if metadata_only else (':fenix:assembleRelease',))
    if not set(expected) <= set(tasks):
        raise ValueError('Gradle dry run did not select the requested release integration tasks')
    unexpected = [task for task in tasks if task.startswith((':geckoview:', ':messaging_example:', ':port_messaging_example:'))]
    if metadata_only:
        unexpected.extend(task for task in tasks if re.search(
            r':(?:compile|ksp|minify|assemble|bundle|package|buildCMake|externalNativeBuild)[^:]*$', task))
    if unexpected:
        raise ValueError('Prebuilt GeckoView app assembly selected engine/sample tasks: ' + ', '.join(unexpected))
    print('Release integration plan uses the complete Maven module without unintended compilation or sample build tasks', flush=True)


def source_coordinate(target):
    """Only the exact verified architecture component owns this Maven source."""
    coordinate = os.environ.get('TASKARIAN_GECKOVIEW_MAVEN_COORDINATE')
    if not coordinate:
        return None
    paths = [os.environ.get(name) for name in ('TASKARIAN_GECKOVIEW_MODULE_RECEIPT',
             'TASKARIAN_GECKOVIEW_SOURCE_REFERENCE', 'TASKARIAN_GECKOVIEW_RELEASE_REFERENCE')]
    if not all(paths):
        raise ValueError('GeckoView Maven source requires its verified producer and permanent source reference')
    receipt_path, proof_path, reference_path = map(Path, paths)
    record, proof, reference = [json.loads(path.read_text()) for path in (receipt_path, proof_path, reference_path)]
    identity = module('component-store').identity(COMPONENT, target, record['inputs'])
    if (proof.get('schema') != 1 or proof.get('coordinate') != coordinate or record.get('coordinate') != coordinate or
            not re.fullmatch(r'org\.mozilla\.geckoview:geckoview[^:]*:[^:]+', coordinate) or
            proof.get('receiptSha256') != sha(receipt_path) or proof.get('release') != reference or
            proof.get('source') != record.get('source') or proof.get('engineVersion') != record.get('engineVersion') or
            proof.get('aarSha256') != record.get('aarSha256') or
            not re.fullmatch('[0-9a-f]{40}', record.get('source', '')) or
            reference.get('repository') != identity['repository'] or reference.get('manifestName') != identity['manifest'] or
            not re.fullmatch('[0-9a-f]{64}', reference.get('manifestSha256', ''))):
        raise ValueError('GeckoView Maven source does not match its verified permanent producer')
    return coordinate


def filter_maven(path, target):
    coordinate = source_coordinate(target)
    if not coordinate:
        raise ValueError('Only an exact verified GeckoView component can leave the third-party Maven inventory')
    path = Path(path)
    records = json.loads(path.read_text())
    matches = [row for row in records if ':'.join(row[key] for key in ('group', 'name', 'version')) == coordinate]
    if len(matches) != 1:
        raise ValueError('The native Maven inventory must contain exactly its verified GeckoView component')
    temporary = path.with_name(path.name + '.geckoview')
    temporary.write_text(json.dumps([row for row in records if row not in matches], indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inputs', 'restore', 'capture', 'publish', 'stage', 'check-plan',
                                          'source-coordinate', 'filter-maven'))
    parser.add_argument('target', choices=('android', 'android-x86_64'))
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--dependencies', type=Path)
    parser.add_argument('--metadata-only', action='store_true')
    args = parser.parse_args()
    if args.action == 'inputs':
        print(json.dumps(inputs(args.target), indent=2)); return
    if args.action == 'check-plan':
        check_plan(args.plan, args.metadata_only); return
    if args.action == 'source-coordinate':
        print(source_coordinate(args.target) or ''); return
    if args.action == 'filter-maven':
        filter_maven(args.dependencies, args.target); return
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.action == 'restore':
        output('hit', str(restore(args.directory, args.target)).lower())
    elif args.action == 'publish':
        publish(args.directory, args.target)
    elif args.action == 'capture':
        capture(args.directory, args.target)
    else:
        stage(args.directory, args.target)


if __name__ == '__main__':
    main()
