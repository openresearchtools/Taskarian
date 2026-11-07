# Taskarian on Omarchy

Taskarian's Linux releases include native `x86_64` and `aarch64` Arch packages
alongside the Debian installers. These are testing releases, not ready for
production. Packages retain the same browser, private Node/Pi runtime, container
helpers and guest packages as the corresponding Debian installer.

Install the repository setup package using the instructions in the
[Open Research Tools package repository](https://github.com/openresearchtools/apt/tree/main/pacman).
Then enable prereleases and install through the signed catalogue:

```sh
sudo openresearchtools-pacman-setup --apply --channel nightly
omarchy update
omarchy pkg add taskarian
```

The `nightly` catalogue follows published prereleases. Use `--channel stable` for
regular releases, or `--channel testing` for the combined view. Switching channels
does not automatically downgrade an installed version. The central publisher
refreshes hourly; newly uploaded packages appear after its next successful run.

Taskarian containers use their bundled rootless Podman, crun and helpers, with a
private image store, runtime directory and control sockets. A host Docker or
Podman socket is not required. The normal desktop user's systemd session,
`newuidmap`/`newgidmap`, subordinate UID/GID ranges and `/dev/fuse` must be usable.
Arch supplies the mapping tools in `shadow`. Taskarian reports missing runtime
prerequisites through its existing container controls.

For release maintainers, the main publisher converts its verified local Linux
DEBs before the initial upload. It does not download the new release again or
rebuild its compiled components. Debian and Arch packages share the release's
stable/prerelease flag and source/license index.

Arch/Omarchy packages additionally include the official Omarchy skill in bundled
Pi. Each publication resolves the latest stable upstream release once for both
CPU architectures, retaining all topic guides, the full MIT license, exact source
revision and checksums. The skill appears through Pi's normal package discovery;
its attribution is also available in Taskarian's component licenses. Debian and
Android installers do not contain this Arch packaging overlay.

## Installed verification

Versions `153.6.6-1` and `153.6.6-2` were installed from the signed `nightly` pacman catalogue on
an Omarchy ARM64 host with 16 KiB memory pages. The packaged private Node.js,
Podman, crun and helpers ran without a distribution-specific runtime patch.
The corresponding prerelease contains both native Arch architectures and the
Debian and Android installers; the central APT nightly catalogue also contains
all four Debian package architectures.

The installed desktop application was exercised in a separate Xvfb/Openbox
session with software rendering. Its display, X authority and input were
independent of the user's active desktop. Through Taskarian's normal controls,
verification covered:

- Creating and provisioning the stock ARM64 Debian container.
- Opening its graphical desktop and using the guest terminal through Display.
- Resizing, closing and reopening Display while preserving the guest session.
- Powering the container off and on, then reconnecting its graphical desktop.
- Enabling the guest Agent and opening its Files application with the normal
  trusted connection.
- Quitting Taskarian through its native menu and observing a clean application
  exit, stopped managed container and cleaned-up managed services.
- Reopening the installed application with the saved container retained.

These results cover the ARM64 installation and software display path. The
`x86_64` package passed packaging and payload integrity checks, but was not
installed on this ARM64 host. Hardware graphics acceleration was not exercised.

The `153.6.6-2` retest confirmed the guest's installed `taskarian-backend` version
was `153.6.6` through its graphical terminal, then repeated display resize,
close/reopen, power off/on and trusted Files access. The host's native Pi skill
loader discovered the packaged `omarchy` skill without diagnostics, and the
normal managed Pi package registration included it. Both CPU packages were
checked against their source DEBs: only the seven reviewed Pi/license metadata
files changed, with the skill/source/license additions recorded in provenance.

A separate stock Android 17 ARM64 Cuttlefish guest was exercised with QEMU/KVM,
8 virtual CPUs and 16 GiB RAM. The Android APK and native Termux backend were both
`153.6.6`, with the backend installed from the signed central APT nightly channel.
Normal app enrollment, saved Termux browser-control consent, Agent UI, ordinary
browser navigation/DOM access and the packaged graphical desktop/terminal worked.
The browser viewer uses localhost noVNC/websockify and QEMU's VNC console; the
QEMU backend does not supply Cuttlefish WebRTC video. The stock Android kernel
was retained. Native Wacom input with Android's per-device IDC configuration and
a lossless RFB pixel-format mapping were required for accurate pointer input and
correct colours. Guest data and enrollment survived the setup restart.
