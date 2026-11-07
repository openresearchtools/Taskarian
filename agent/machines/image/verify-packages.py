#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Verify native APT's installed guest packages against this app's exact inputs."""
import argparse
import json
from pathlib import Path
import subprocess


def verify(manifest, root):
    required = {'taskarian-backend', 'taskarian-os', 'taskarian-machines-guest'}
    packages = manifest['packages']
    expected = {item['package']: item for item in packages}
    if set(expected) != required or len(packages) != len(required):
        raise ValueError('The guest needs exactly one backend, OS and guest-service package')
    if expected['taskarian-backend']['version'] != manifest['version']:
        raise ValueError('The guest backend must match the Taskarian app version')
    actual = subprocess.check_output([
        'dpkg-query', '--admindir=' + str(root / 'var/lib/dpkg'), '-W',
        '-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n',
        *sorted(required),
    ], text=True)
    for line in actual.splitlines():
        name, version, architecture, status = line.split('\t')
        if (status != 'installed' or version != expected[name]['version'] or
                architecture != manifest['architecture']):
            raise ValueError('Installed guest package does not match the app manifest: ' + name)
    backend = json.loads((root / 'usr/lib/taskarian/build-platform.json').read_text())
    if any(backend.get(key) != manifest[key] for key in ('version', 'revision', 'architecture')):
        raise ValueError('Installed guest backend source/version/architecture differs from this app')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--root', type=Path, default=Path('/'))
    args = parser.parse_args()
    try:
        verify(json.loads(args.manifest.read_text()), args.root)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, 'Guest package verification failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
