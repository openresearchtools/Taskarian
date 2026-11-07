# Product packaging

Release compilation runs in GitHub Actions; the 8 October local setup also uses
an external data-drive workspace and cached Podman builders. Dispatch `Complete Taskarian candidates`
on `main` to build ARM64 and x86_64 Android APKs, Linux amd64/arm64 packages,
and native Termux aarch64/x86_64 packages. The main workflow coordinates
authentication/search/private-Node components, Termux and source bundles. It
dispatches the exact product commit to these builders:

- `openresearchtools/taskarian-build-arm64`: complete Linux ARM64 `.deb`.
- `openresearchtools/taskarian-build-amd64`: complete Linux AMD64 `.deb`.
- `openresearchtools/taskarian-build-android`: signed ARM64 Android APK.
- `openresearchtools/taskarian-build-android-x86_64`: signed x86_64 Android APK.

Each builder has its own compiler cache and uploads its installable candidate to
its Actions run. Those artifacts can be downloaded manually immediately. The
main workflow collects each target as soon as its upload appears; it does not
wait for the other builders. Complete release assembly still requires all six
binaries. Builders publish permanent versioned component prereleases; the main
publisher owns the installer release and APT publication.

`BUILD_REPOS_TOKEN` exists only as an encrypted Actions secret in the product
repository. Its fine-grained scope is Actions read/write on those four builders.
Builders use their own job token to read the public product's component artifacts;
both Android builders also need the four existing Android signing secrets.
Never put credential values in source or workflow files.

The builder workflow sources are `.github/builders/linux.yml` and `android.yml`.
Deploy each to `.github/workflows/build.yml` on its builder's `main` when changing
the build recipe. The builder repositories contain workflow configuration only;
all application source remains here and is checked out by exact commit.

The Linux browser workflow retains the external build directories, Mozilla
bootstrap toolchains, compiler cache and optional gkrust warm-up from WildBuzzard.
Completed browser components are reused from permanent architecture-owned
releases when their scoped inputs match. Ordinary restoration downloads runtime,
license and small metadata files; complete matching sources and compiler backups
remain in those releases. Agent-only changes reassemble the Linux package without
rebuilding unchanged Gecko. Reuse preserves the actual producer and verified
ABI/checksums. Historical input refinements require the existing checksummed
equivalence receipts; original manifests and binary identities remain unchanged.
A later application failure does not discard an independently verified and
published component.

Android first checks for an exact-source APK or resource checkpoint with the
same product version and actual paired Termux package metadata. A complete
application hit skips compilation and immutable republication; a resource-only
hit can finish the missing application publication.

Otherwise, `android-geckoview-component.py` restores the complete GeckoView Maven
module: AAR Java classes, manifest, resources, `omni.ja` and native libraries,
plus upstream POM and Gradle `.module` dependency/capability metadata. Its inputs
include the native engine identity and actual GeckoView Java/resource owners.
The official engine version, such as `153.6.0esr`, stays separate from the
Taskarian maintenance version. The original module build ID/source and separate
native engine producer remain recorded through reuse, with full matching source
and license references.

A complete GeckoView hit skips its build and substitutes the exact local Maven
module through Gradle's native dependency APIs. App configuration and required
generated sources are prepared for ordinary application assembly. When the
complete module is absent but the native engine matches, upstream artifact-build
resource/staging tiers and the single GeckoView Maven publication create it;
sample and AndroidTest APK tasks are omitted. Changed native inputs use their
normal compiler owner and cache.

Gradle dependency downloads and task outputs use separate Actions cache keys;
the multi-gigabyte Mozilla bootstrap directory is not cached there. Gradle
validates task inputs before reuse. Application code changes still run the
required Java/Kotlin, resource, packaging and potentially R8 tasks. Pass worker
and cache options through `GRADLE_FLAGS`, since `mach` replaces `GRADLE_OPTS`.
The focused engine-only workflow verifies the real release task plan contains
no GeckoView build tasks, then exercises native dependency/source/license tasks
using the published app AARs without producing a new APK candidate.

Focused verification on 7 November 2026 passed on
[Android ARM64](https://github.com/openresearchtools/taskarian-build-android/actions/runs/42379867390)
and [Android x86_64](https://github.com/openresearchtools/taskarian-build-android-x86_64/actions/runs/42379869329).
Both restored previously published complete GeckoView modules, skipped native
Gecko/resource/Maven production and APK assembly, and passed the release consumer
plan plus actual native dependency/source/license tasks. These build checks do
not replace installed-app acceptance.

Android packaging-only changes can reuse the entire compiled APK module through
`android-apk-module.py`. Its conservative identity retains browser code/resources,
build definitions, the actual native-leaf inputs and all Android dependency pins;
the product maintenance version and paired backend metadata are assembly inputs.
The collector captures the actual resolved source and legal inventories only
after its normal APK checks. A small permanent component indexes that receipt and
the existing browser-resources APK, without uploading a second APK. The large
receipt stays outside normal application/installer downloads.

On a matching module, native AAPT2 converts the manifest through Android's
official protobuf API and changes only `versionName`. A native round trip must
preserve every other XML field. Current companion notices, package assembly
provenance and update/bootstrap assets are regenerated through the existing
license and pairing owners. Every retained non-metadata ZIP entry must match its
original producer byte-for-byte; old signatures are removed. The existing
collector still performs real alignment, signing, source/native verification and
final artifact checks. `build-manifest.json` distinguishes assembly source/run
from the immutable compiled APK producer and its permanent component reference.
The package manager and About version use the current manifest version; compiled
BuildConfig source/date remain the producer's values. Reuse admits only the
reviewed disabled-reporting consumers of BuildConfig.VERSION_NAME and preserves
the existing versionCode policy. New functional consumers require an ordinary
build and review of that contract.

There is no historical APK seeding: older artifacts lack the captured resolved
inventory required by this path. The first ordinarily required full APK producer
under the new collector establishes its module; no full candidate is dispatched
merely to populate it. Exact-source artifact restoration remains independent.

Private Node jobs likewise restore a complete runtime and corresponding-source
artifact before consulting download caches or upstream package pools. Reuse
requires a successful Node assembly/upload job (or a verified prior restore) in
a completed main-branch candidate or Node workflow, identical committed recipe
and retained-source trees, and the same runner/install/assembly commands.
The verifier checks both archives and sidecars, lock identities, complete source
archives, npm source and full notices. A checksummed receipt preserves the
original producing revision/run and archive hashes through later restores;
the release source inventory retains it. Changing cache policy does not change
the component recipe, pins, binaries or original producer. Changed assembly
inputs still require the ordinary pinned build, including its download errors.

`build.py` accepts `linux|termux`, `--architecture`, `--auth-archive`,
`--search-archive`, `--node-archive` and `--output`; Linux also requires `--browser-dir`.
Each component requires its `SHA256SUMS` and target metadata. The Firefox-aligned
product version comes from `browser/taskarian/config/version.txt`; internal npm
metadata uses its three-component SemVer equivalent. Native architecture and
Termux 16 KB ELF alignment are checked before creating the package.

The full Linux package installs the `taskarian-podman` AppArmor conffile for
its private `/usr/lib/taskarian/machines/bin/podman` executable. This adapts
upstream's named-unconfined Podman `userns` policy for the existing Ubuntu
24.04-based runtime, using ABI 4.0 for AppArmor 4/5. The configure script reloads
only this profile when AppArmor is active; it preserves administrator edits,
local overrides and disable/complain choices. AppArmor remains an optional host
facility; no sysctl, host-wide policy or distro Podman profile is changed.
Reload failures are reported, and existing kernel/uidmap/device prerequisites
still apply. Termux and container backend-only packages do not install it.
`linux/apparmor/` retains the exact upstream source pin, original policy, full
license and adaptation notice; the offline component licenses and release source
archive include them. Normal removal retains configuration and loaded policy;
purge removes only this profile's configuration/cache, following `dh_apparmor`.

`release.py` collects two signed `com.taskarian` APKs and four complete
native packages plus corresponding sources. A release must match the exact
successful candidate run. Runtime/device validation happens outside the repository. Prereleases are published
for that testing; a completed build does not certify device acceptance. Logs, probes and test profiles are not shipped.

The main publisher also creates native Arch Linux/Omarchy packages for `x86_64`
and `aarch64` from its already downloaded and verified Linux `amd64` and `arm64`
DEBs. `arch/release.py` and its reviewed rules preserve the application payload,
bundled Podman/crun helpers, guest DEBs, licenses and component provenance. Native
package dependencies and a pacman install hook replace Debian package metadata
and reviewed maintainer actions. No component compilation or second installer
download is needed. Both package formats and Arch provenance are uploaded in the
same publication, with the complete asset inventory covered by `SHA256SUMS`.

The existing `openresearchtools/apt` manifest already tracks Taskarian. Its signed
pacman catalogue indexes these native Arch assets: stable releases go to `stable`,
prereleases to `nightly`. See [Omarchy installation](../../docs/omarchy-installation.md).

GitHub requires [each release asset to be smaller than 2 GiB](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
The collector preserves smaller source archives unchanged and splits larger ones
into ordered 1 GiB binary parts without recompression or removing any sources or
notices. `release.json` keeps `sources` as the flat downloadable asset list and
adds `sourceArchives`, recording each original archive's name, byte count,
SHA-256 and ordered parts with their sizes, hashes and release URLs. APK/DEB
names, bytes and metadata remain unchanged; an oversized installable package
fails collection instead of being split.

The complete-candidate artifact contains only uploadable files, including the
parts, `SOURCE-ARCHIVES.md`, per-archive `.parts.sha256` and original `.sha256`
files. Its `SHA256SUMS` covers every attached file. The publish workflow verifies
these checksums and rejects oversized assets before `gh release create` uploads
the directory. Download `SOURCE-ARCHIVES.md` from the release and run its explicit
`gh release download`, `sha256sum`, ordered `cat` and final checksum commands in
a new directory. Only extract the reconstructed archive after its original hash
passes. Direct archive downloads remain usable as before. The APT publisher
selects whole `.deb` release assets by filename and reads their package headers;
the APK updater uses `apps` in `release.json`. Those binaries and the manifest's
`packages`/`apps` records are unchanged; source parts never enter those paths.

The publisher's **prerelease** checkbox defaults on for now. On publishes to
nightly APT; off publishes a regular release to stable APT. The stable catalogue
excludes prereleases, and the nightly catalogue includes only prereleases. Install the
platform's stable keyring first and then its `openresearchtools-nightly` or
`openresearchtools-termux-nightly` package to opt in. Both sources remain enabled
with equal priority, so a newer stable package can replace a nightly. Removing
the nightly setup package disables that source and leaves stable enabled.

Every published version is a testing release. `release.py` includes the
README's warning SVG and an explicit production warning in `release-notes.md`.
Run **Prepare next release candidate** on main to select a product version greater
than every published regular release and prerelease. It preserves a newer,
unpublished committed version; otherwise it increments the maintenance point on
the same Firefox major/minor line using the existing Firefox version helper.
It synchronizes the Agent npm version, commits changes before dispatching the
complete candidate build, and does not publish. The manual **Publish testing
release** workflow takes the successful complete-candidate run, the prerelease
choice and those exact notes. Publication rejects a duplicate or older version
across both channels. Any manual publication must also use the notes file
with `gh release create --notes-file`; never replace it with autogenerated notes.

The Android workflow accepts native-auth, native-search and private-Node run IDs when dispatched
alone. Its full Gecko build starts immediately; final Gradle assembly waits for
the actual Termux component notices. Temporary server payloads supply license
texts only and never enter the APK. Droid credentials stay in GitHub secrets and
an external temporary file removed after signing.

Both channels use the exact committed product version for `.deb` versions and
`v<product>` tags, for example prerelease `153.4.1`, prerelease `153.4.2`, then
regular release `153.4.3`. There is no nightly version suffix or separate stable
sequence: with both sources enabled, APT takes the higher version from either.
The channel is GitHub's prerelease flag; candidate binaries are not relabeled
or promoted by changing their compiled version at publication.

From the 24 October 2026 rollback baseline onward, direct Android APK releases
keep version code `2024000000 + abiBits`, where ARM64 uses 2 and x86_64 uses 6.
Do not increment this code: Android's normal installer permits replacing an APK
with the same code and signing identity, so a user can return to this baseline
without uninstalling or clearing saved app data. Update discovery compares the
numeric two- or three-part product `versionName`; it never offers the installed
version again merely because a build code differs. Build/source identities stay
in the existing manifests and matching Termux package pins. Retain published
baseline packages in signed APT catalogues. This defines package rollback from
this baseline forward; installed acceptance must also check that the tested
versions preserve user configurations, models, chats and explicit runtime choices.


Private Node components are built by `node-native.yml` for Linux amd64/arm64 and
native Termux aarch64/x86_64. `node/build.py` consumes hash-pinned official Node
LTS or Termux binaries and publishes `node-runtime-<platform>-<arch>.tar.gz`,
`node-source-<platform>-<arch>.tar.gz` and SHA256SUMS. Its download cache defaults
to `~/.cache/Taskarian/sources` and can be moved with `TASKARIAN_NODE_CACHE`.

Termux launchers use upstream termux-exec's
`TERMUX_EXEC__SYSTEM_LINKER_EXEC__MODE=disable` for Taskarian and its children.
This preserves the actual Node ELF in `process.execPath`, including under
`RUN_COMMAND` and terminal environments with `LD_PRELOAD`. The supported
launchers re-execute a system shell once when selecting this mode, because
termux-exec caches its mode in an already-preloaded process. The supported
GitHub/F-Droid Termux app targets SDK 28 and permits direct native execution;
a variant whose Android policy forbids it is unsupported. The executable check
does not silently substitute Android's linker or a system Node runtime.
The payload contains the actual Node ELF, npm/npx wrappers bound to that ELF,
private npm sources and native libraries, target/version/source metadata and
full license texts. `build.py` rejects mismatched targets and missing notices;
Node/npm are no longer external package dependencies. Runtime libraries that
remain external are declared by the component manifest.

The four remote builders require a `node_run` input along with `auth_run` and
`search_run`. Deploy the updated builder workflows before dispatching candidates.
Android embeds the matching companion Node notices in its offline inventory.
`sources.py --node-sources DIR --cache DIR` retains all four verified Node source
archives alongside product/npm source. Source caches and build outputs stay
outside the checkout. Release assembly rejects packages without Node provenance
or missing Node source artifacts.
