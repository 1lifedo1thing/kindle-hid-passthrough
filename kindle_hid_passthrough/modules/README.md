# Bundled kernel modules

Some Broadcom-era Kindles ship a kernel with `CONFIG_UHID` disabled and no
`/dev/uhid`. For those we bundle a prebuilt `uhid.ko` and `insmod` it at startup.
MediaTek Kindles (11th gen onward) have UHID in-kernel and need nothing.

Those same kernels usually disable `CONFIG_INPUT_UINPUT` as well, so we bundle
`uinput.ko` too. It is optional. The passthrough never opens `/dev/uinput`, it
only matters to external tools such as kindle-button-mapper that inject key
events, so a missing `uinput.ko` is a warning while a missing `uhid.ko` leaves
HID unusable.

Modules are matched by exact filename:
`{uhid,uinput}-{uname-r}-{trailing-build-from-/etc/version.txt}-{codename}.ko`.
A single model can need more than one module across firmware updates (different
`uname-r` or build). See `../../docs/uhid-research.md` for the build recipe.

## Coverage matrix

Legend: ✅ have it · ⚠️ built, not yet device-tested · ❌ missing · n/a in-kernel

| Model | Codename | Chip | Kernel (`uname -r`) | uhid.ko status | uinput.ko status |
|-------|----------|------|---------------------|----------------|------------------|
| Oasis (2016) | duet | BCM4343 | 3.0.35-lab126 | ❌ needs uhid backport (kernel predates mainline uhid) | ❌ |
| Basic 2 (2016) | heisenberg | BCM4343 | 3.10.53-lab126 | ⚠️ build 409749 | ⚠️ build 409749 |
| Oasis 2 (2017) | zelda | BCM4343 | 4.1.15-lab126 | ✅ builds 409745, 443455 | ✅ build 443455 · ⚠️ build 409745 |
| Paperwhite 4 (2018) | rex | BCM4343 | 4.1.15-lab126 | ✅ builds 435186, 476967 · ⚠️ builds 337283, 360278 | ⚠️ builds 337283, 435186, 476967 |
| Basic 3 (2019) | rex | BCM4343 | 4.1.15-lab126 | ✅ builds 435186, 476967 · ⚠️ build 360278 | ⚠️ builds 435186, 476967 |
| Oasis 3 (2019) | zelda | BCM4343 | 4.1.15-lab126 | ✅ builds 409745, 443455 | ✅ build 443455 · ⚠️ build 409745 |
| Paperwhite 5 (2021) | — | MediaTek | — | n/a (native `/dev/uhid`) | n/a |
| Basic 4 (2022) | — | MediaTek | — | n/a | n/a |
| Scribe (2022) | — | MediaTek | — | n/a | n/a |
| Basic 5 (2024) | — | MediaTek | — | n/a | n/a |
| Paperwhite 6 (2024) | — | MediaTek | — | n/a | n/a |
| Scribe 2 (2024) | — | MediaTek | — | n/a | n/a |
| Colorsoft (2024) | — | MediaTek | — | n/a | n/a |
| Scribe 3 (2025) | — | MediaTek | — | n/a | n/a |
| Scribe CS (2025) | — | MediaTek | — | n/a | n/a |

`build` is the trailing number from `/etc/version.txt`. Kernel and build can vary
across firmware updates, so a covered model may still hit an unbuilt build. When a
Kindle hits a missing module the daemon logs the exact `.ko` filename plus its
model, codename, kernel, and `/etc/version.txt`, which is everything needed to
build it.

A module's ABI keys on `uname -r`, not the version.txt build in its filename, so
when the exact build isn't bundled the daemon tries other builds of the same
kernel and codename and lets `insmod`'s own version check decide. A firmware bump
that keeps the kernel ABI loads on an existing module; only a real `module_layout`
change forces a fresh build.
