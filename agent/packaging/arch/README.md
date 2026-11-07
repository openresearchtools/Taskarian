# Native Arch release packages

The normal Taskarian publisher converts its already downloaded and verified
Linux `amd64`/`arm64` Debian payloads into `x86_64`/`aarch64` pacman packages.
It uploads both formats in the same new release. It does not rebuild the
application or components, download them again, or change an older release.

```sh
python3 agent/packaging/arch/omarchy-skill.py --output /path/to/omarchy-skill
python3 agent/packaging/arch/release.py \
  --input-directory /path/to/verified-release \
  --release-tag v153.6.6 \
  --omarchy-skill-directory /path/to/omarchy-skill \
  --output /path/to/arch-output
```

Python 3.11+, `zstd`, and either Python's built-in zstd support or
`python3-zstandard` are required. Output comprises two `.pkg.tar.zst` files,
per-package provenance, and `SHA256SUMS.arch.txt`. The publisher adds these to
its complete `SHA256SUMS`; the existing Debian `release.json` entries and source
indexes are preserved. A packaging-only change at the same application version
requires increasing `--pkgrel`. Different existing package bytes are rejected.

The source `SHA256SUMS`, `release.json`, DEB size/hash, Linux platform and actual
Debian control identity must agree. Only Linux controls `amd64` and `arm64` are
accepted; native Termux `aarch64` and `x86_64` packages are rejected. Package
version comes from the actual DEB, never from its filename or release tag.

`rules.json` explicitly maps the reviewed Debian dependencies to native Arch
packages. Unknown dependencies or constraints stop assembly for review.
`python>=3.12`/`python<3.15` preserve the shipped search wheels' supported Python
range. Native GStreamer packages supply the same PipeWire and PulseAudio
functions; `shadow` provides `newuidmap`/`newgidmap`. The existing private
Podman/crun/conmon/network/storage helpers remain bundled, with their own
Taskarian lifetime and private socket. No system Podman daemon is installed or
activated by this packaging.

Debian maintainer scripts are never executed by the converter. The small
native pacman `taskarian.install` adapter preserves runtime initialization,
installation stamps, the exact legacy Pi alias retirement, optional AppArmor
loading, administrator policy choices and retained runtime/user data. Source
scripts must exactly match `reviewed-scripts` after substituting the runtime
identity derived from the shipped build stamp and runtime default. Changes to
script behavior stop assembly. Pacman's `backup` protects the AppArmor profile;
Arch's existing desktop/icon/font hooks handle their own caches.

Compiled payload bytes, modes and symlinks are retained. The reviewed Omarchy
overlay adds the official Omarchy skill to Pi's existing package manifest,
including all Markdown topic guides, the full upstream MIT license and an exact
source receipt. The publisher resolves the latest non-prerelease Omarchy tag
once, pins its actual commit, verifies each Git blob and records SHA-256 hashes;
both CPU packages use that same snapshot. Missing, changed or unsupported source
layouts fail packaging. No host skill symlink or live download at application
startup is used. Debian and Android packages do not receive this overlay.

The overlay appends attribution to existing notices, adds the full license and
source revision to both installed license inventories, and regenerates About
with the existing license renderer. Other license records and component source
provenance are retained. Pi's normal managed package registration discovers the
additional skill; its upstream engine is unchanged. A user-customized skill
selection remains under the existing registration policy.

Ownership is normalized to root and timestamps use
`SOURCE_DATE_EPOCH` (default 0). No payload is extracted onto the packaging host,
no Debian library-path mapping is guessed, and special/setuid files are
rejected. Provenance identifies the original installer, release, source commit,
reviewed scripts, native adapter, packaging commit/source checksums and the
Omarchy skill's immutable source/license inventory. Full component
sources retain their original permanent release links.

The initial archive reader/converter was adapted from
[SimpleHF](https://github.com/openresearchtools/simplehf/tree/0acb82b8264a5ccbd9338ee5dcf901944df6665b/packaging/arch)
(MIT; full text in `LICENSE.simplehf`). Taskarian additions are AGPL-3.0-only.
Packaging checks and fixtures stay outside this repository, as required by
`AGENTS.md`; successful archive checks do not establish installed UI acceptance.
