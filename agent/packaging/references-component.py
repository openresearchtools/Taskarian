#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare the ordinary capture extension before the native browser build."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
CONNECTOR = ROOT / 'references/connector'


def input_paths():
    """Shared capture inputs for the native build and immutable APK reuse."""
    return ['references/connector', 'references/vendor/translators', 'references/legal',
            'references/web/theme.css', 'agent/packaging/source_cache.py',
            *(f'browser/taskarian/browser/branding/default{size}.png' for size in [16, 32, 64, 128])]


def fingerprint():
    result = hashlib.sha256()
    paths = []
    for name in input_paths():
        base = ROOT / name
        if base.is_dir():
            paths.extend(path for path in base.rglob('*') if path.is_file()
                         and not {'.build', 'dist', 'node_modules', '__pycache__'}.intersection(path.relative_to(base).parts))
        else:
            paths.append(base)
    for path in sorted(paths):
        result.update(str(path.relative_to(ROOT)).encode() + b'\0')
        result.update(hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def verify_output():
    runtime = CONNECTOR / 'dist/references'
    checksums = json.loads((CONNECTOR / 'dist/runtime-sha256.json').read_text())
    if not checksums or set(checksums) != {str(p.relative_to(runtime)) for p in runtime.rglob('*') if p.is_file()}:
        raise ValueError('References capture runtime inventory differs')
    for name, expected in checksums.items():
        path = runtime / name
        if not path.resolve().is_relative_to(runtime.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('References capture checksum mismatch: ' + name)
    component = json.loads((CONNECTOR / 'dist/component.json').read_text())
    if component['extensionId'] != 'references@taskarian.org' or component['hostId'] != 'references-host@taskarian.org':
        raise ValueError('Unexpected References extension identity')
    if hashlib.sha256((CONNECTOR / 'dist/references.xpi').read_bytes()).hexdigest() != component['sha256']:
        raise ValueError('References capture package checksum mismatch')


def main():
    stamp = CONNECTOR / 'dist/build-inputs.json'
    wanted = fingerprint()
    try:
        current = json.loads(stamp.read_text())
        if current['sha256'] == wanted:
            verify_output()
            print('Verified unchanged References capture build')
            return
    except (OSError, ValueError, KeyError):
        pass
    subprocess.run(['python3', str(CONNECTOR / 'build.py')], check=True, cwd=ROOT)
    verify_output()
    stamp.write_text(json.dumps({'schema': 1, 'sha256': wanted}, indent=2) + '\n')


if __name__ == '__main__':
    main()
