#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve one stable Omarchy skill snapshot, then overlay Arch packaging only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

REPOSITORY = 'omacom/omarchy'
SOURCE_PATH = 'default/agents/skills/omarchy'
PIN = 'UPSTREAM.json'
PI = 'usr/lib/taskarian/pi/'
NOTICES = ('usr/lib/taskarian/THIRD_PARTY_NOTICES.md',
           'usr/share/doc/taskarian/THIRD_PARTY_NOTICES.md')
LICENSES = ('usr/lib/taskarian/licenses.json', 'usr/share/doc/taskarian/licenses.json')
REQUIRED = frozenset((PI + 'package.json', PI + 'NOTICE',
                     'usr/lib/taskarian/about.html', *NOTICES, *LICENSES))


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def api(route):
    return json.loads(subprocess.check_output(['gh', 'api', 'repos/' + REPOSITORY + '/' + route]))


def blob(identity):
    if not re.fullmatch('[0-9a-f]{40}', identity):
        raise ValueError('Invalid upstream Git blob identity')
    item = api('git/blobs/' + identity)
    if item.get('encoding') != 'base64' or item.get('sha') != identity:
        raise ValueError('Unexpected upstream Git blob response')
    data = base64.b64decode(item['content'])
    if len(data) > 2 * 1024 ** 2 or item.get('size') != len(data):
        raise ValueError('Oversized or truncated Omarchy skill source')
    if hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != identity:
        raise ValueError('Omarchy source differs from its immutable Git blob')
    return data


def relative(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')) or '\\' in name:
        raise ValueError('Unsafe Omarchy skill source path')
    return name


def prepare(directory):
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Omarchy skill staging directory must be empty')
    release = api('releases/latest')
    tag = release.get('tag_name', '')
    if release.get('draft') is not False or release.get('prerelease') is not False or not re.fullmatch(r'v[0-9]+(?:\.[0-9]+){1,2}', tag):
        raise ValueError('Latest Omarchy release must be a stable version')
    commit = api('commits/' + tag)['sha']
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid Omarchy release commit')
    tree = api('git/trees/' + commit + ':' + SOURCE_PATH + '?recursive=1')
    if tree.get('truncated') is not False or not tree.get('tree') or len(tree['tree']) > 256:
        raise ValueError('Incomplete or unexpectedly large Omarchy skill tree')
    files, inventory = {}, {}
    for item in tree['tree']:
        if item['type'] == 'tree':
            continue
        name = relative(item['path'])
        if item['type'] != 'blob' or item['mode'] != '100644' or not name.endswith('.md'):
            raise ValueError('Review the new Omarchy skill layout before shipping non-Markdown files')
        data = blob(item['sha'])
        files[name] = data
        inventory[name] = {'sha256': digest(data), 'gitBlob': item['sha']}
    if 'SKILL.md' not in files or not re.search(rb'(?m)^name:\s*omarchy\s*$', files['SKILL.md']):
        raise ValueError('Stable Omarchy release lacks its native skill')
    license_item = api('contents/LICENSE?ref=' + commit)
    license_text = blob(license_item['sha'])
    if b'Permission is hereby granted, free of charge' not in license_text or b'THE SOFTWARE IS PROVIDED "AS IS"' not in license_text:
        raise ValueError('Review changed upstream Omarchy license before redistribution')
    files['LICENSE'] = license_text
    inventory['LICENSE'] = {'sha256': digest(license_text), 'gitBlob': license_item['sha']}
    pin = {'schema': 1, 'repository': REPOSITORY, 'tag': tag, 'commit': commit,
           'release': 'https://github.com/' + REPOSITORY + '/releases/tag/' + tag,
           'source': 'https://github.com/' + REPOSITORY + '/tree/' + commit + '/' + SOURCE_PATH,
           'sourcePath': SOURCE_PATH, 'tree': tree['sha'], 'license': 'MIT', 'files': inventory}
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (directory / PIN).write_bytes(encoded(pin))
    validate(directory)
    print('Resolved stable Omarchy skill ' + tag + ' at ' + commit, flush=True)
    return pin


def validate(directory):
    directory = Path(directory)
    pin = json.loads((directory / PIN).read_text())
    if (pin.get('schema') != 1 or pin.get('repository') != REPOSITORY or
            pin.get('sourcePath') != SOURCE_PATH or pin.get('license') != 'MIT' or
            not re.fullmatch('[0-9a-f]{40}', pin.get('commit', '')) or
            not re.fullmatch(r'v[0-9]+(?:\.[0-9]+){1,2}', pin.get('tag', '')) or
            not {'SKILL.md', 'LICENSE'} <= pin.get('files', {}).keys()):
        raise ValueError('Invalid stable Omarchy skill source receipt')
    files = {}
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('Omarchy skill staging must not contain symlinks')
        if path.is_file() and path.name != PIN:
            files[str(path.relative_to(directory))] = path.read_bytes()
    if files.keys() != pin['files'].keys():
        raise ValueError('Omarchy skill source inventory differs from staging')
    for name, data in files.items():
        relative(name)
        entry = pin['files'][name]
        if (digest(data) != entry['sha256'] or
                hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != entry['gitBlob']):
            raise ValueError('Omarchy skill source/license checksum mismatch: ' + name)
    files[PIN] = encoded(pin)
    return pin, files


def overlay(directory, payload, version):
    if not REQUIRED <= payload.keys():
        raise ValueError('Arch skill overlay requires existing Pi, license and About owners')
    pin, files = validate(directory)
    package = json.loads(payload[PI + 'package.json'])
    skills = package['pi']['skills']
    if './skills/omarchy' in skills:
        raise ValueError('Source DEB already includes Omarchy; it must remain Arch-only')
    skills.append('./skills/omarchy')
    attribution = ('Omarchy skill, ' + pin['tag'] + ', MIT.\nSource: ' + pin['source'] +
                   '\nUpstream files are unmodified; Taskarian includes this skill only in Arch/Omarchy packages.\n' +
                   'Full license and source receipt: pi/skills/omarchy/LICENSE and UPSTREAM.json.\n')
    result = {PI + 'package.json': encoded(package),
              PI + 'NOTICE': payload[PI + 'NOTICE'] + b'\n' + attribution.encode()}
    for name in NOTICES:
        result[name] = payload[name] + b'\n\n## Omarchy skill\n\n' + attribution.encode()
    records = json.loads(payload[LICENSES[0]])
    if json.loads(payload[LICENSES[1]]) != records or any(item.get('name') == 'Omarchy skill' for item in records):
        raise ValueError('Existing license inventories differ or already contain Omarchy')
    records.append({'name': 'Omarchy skill', 'version': pin['tag'].removeprefix('v'), 'license': 'MIT',
                    'source': pin['source'], 'sourceCommit': pin['commit'],
                    'attribution': attribution, 'sourceProvenance': pin,
                    'text': files['LICENSE'].decode()})
    for name in LICENSES:
        result[name] = encoded(records)
    with tempfile.TemporaryDirectory(prefix='taskarian-arch-about-') as temporary:
        root = Path(temporary)
        (root / 'licenses.json').write_bytes(encoded(records))
        renderer = Path(__file__).resolve().parents[1] / 'about.py'
        subprocess.run(['python3', str(renderer), 'linux', str(root / 'licenses.json'),
                        str(root / 'about.html'), '--version', version], check=True)
        result['usr/lib/taskarian/about.html'] = (root / 'about.html').read_bytes()
    for name, data in files.items():
        result[PI + 'skills/omarchy/' + name] = data
    result['usr/share/licenses/taskarian/omarchy-skill/LICENSE'] = files['LICENSE']
    result['usr/share/doc/taskarian/omarchy-skill-source.json'] = encoded(pin)
    return result, pin


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    prepare(arguments.output)
