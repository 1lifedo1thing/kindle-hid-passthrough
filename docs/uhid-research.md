# Building uhid.ko for BCM-era Kindles

8th-10th gen Kindle kernels shipped without CONFIG_UHID. To build a working .ko for a given firmware:

1. Grab the kernel source for that firmware from https://www.amazon.com/gp/help/customer/display.html?nodeId=200203720 (e.g. `Kindle_src_5.18.2_4434550025.tar.gz`).
2. Extract twice (outer tarball wraps `gplrelease/linux-4.1.15.tar.gz`).
3. Cross-compile with Linaro 4.9.4 (matches Amazon's gcc 4.9.1): https://releases.linaro.org/components/toolchain/binaries/4.9-2017.01/arm-linux-gnueabihf/
4. Apply Amazon's defconfig (`imx_v7_zelda_defconfig` for KO2/KOA3, `imx_v7_rex_defconfig` for PW4/Basic 4, `imx_heisenberg_defconfig` for Basic 2).
5. `scripts/config --module CONFIG_UHID && scripts/config --disable CONFIG_FB_MXC_EINK_V2_PANEL CONFIG_FB_MXC_EINK_PANEL`
6. `make ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- vmlinux modules` (the vmlinux pass is what populates Module.symvers with module_layout's CRC, without it the module gets "no symbol version for module_layout" at insmod time).
7. `make M=drivers/hid modules` produces `drivers/hid/uhid.ko`.

Verify with `readelf -A uhid.ko` (expect `Tag_CPU_arch: v7`) and `readelf -S uhid.ko | grep __versions` (expect non-zero size).

Filename for shipping: `uhid-{uname-r}-{trailing-build-number-from-/etc/version.txt}-{codename}.ko`.

## Kernel 3.0.35-lab126 (Oasis 1 / duet)

The Oasis 1 runs `3.0.35-lab126`, which predates mainline `uhid.c` (landed in
3.6). `drivers/hid/uhid.c` has to be backported, not just enabled. The
backported source and its build harness live in
[`uhid-duet-src/`](uhid-duet-src/). Key differences from the 4.1.15 recipe above:

- **Source tree.** Any `3.0.35-lab126` GPL release works; the config feeds the
  vermagic, not the exact build. The `imx60_duet_defconfig` (Kindle Oasis) tree
  is mirrored at https://github.com/katadelos/linux-3.0.35-lab126 (a re-upload of
  `Kindle_src_5.10.1.1_3358210034.tar.gz`).
- **uhid backport.** `uhid.c` is adapted from the 3.15+ driver down to the
  pre-3.15 `hid_ll_driver` (no `.raw_request`/`.output_report`). Only the event
  ABI the daemon uses is kept: `UHID_CREATE2` / `UHID_INPUT2` / `UHID_DESTROY`
  (plus legacy `CREATE`/`INPUT`). The host->device report paths (OUTPUT,
  GET_REPORT, SET_REPORT) are dropped.
- **HID core dependency.** The stock Oasis 1 kernel has no HID core at all
  (`# CONFIG_HID_SUPPORT is not set`; `hid_input_report` absent from
  `/proc/kallsyms`), so `hid.ko` (`hid-core.o` + `hid-input.o`, generic binding
  is built in on 3.0.35) is bundled next to uhid.ko and loaded first;
  `_ensure_hid_core()` insmods it when `/sys/bus/hid` is missing. Both are built
  as modules with `BT_HIDP`/`USB_HID` disabled so nothing `select`s `HID=y`.
  `hid.ko` also pulls in `debugfs_*`, which the device exports
  (`CONFIG_DEBUG_FS=y` in the defconfig).
- **Generic driver.** Pre-3.9 kernels have no transport-independent generic HID
  driver (usbhid/hidp each register their own `generic-*`, and `hid_match_one_id`
  requires an exact bus match). With those disabled, nothing binds a
  uhid-created device, so uhid.ko registers its own `generic-uhid` driver
  (matching `BUS_BLUETOOTH`/`USB`/`VIRTUAL`); without it the device is added to
  the hid bus but never probed, so `hidinput_connect()` never runs and no
  `/dev/input/eventX` appears.
- **No MODVERSIONS.** `# CONFIG_MODVERSIONS is not set`, so there is no
  `module_layout` CRC to match; the `vmlinux` pass is unnecessary
  (`make modules_prepare` is enough). Only the vermagic string must match:
  `3.0.35-lab126 preempt mod_unload ARMv7 p2v8`.
- **No devtmpfs.** `# CONFIG_DEVTMPFS is not set`, so `misc_register` does not
  create `/dev/uhid`. The daemon's `_ensure_uhid()` creates the node from
  `/sys/class/misc/uhid/dev` after insmod.
- **Toolchain.** kernel.org crosstool gcc 4.9.4 `arm-linux-gnueabi` builds it;
  its `cc1` needs `libmpfr.so.4` (Debian ships `.so.6`), and the kernel's host
  tools need a pre-14 host gcc (`HOSTCC=gcc-12 HOSTCFLAGS=-fcommon`) because
  gcc 14 makes implicit-function-declaration a hard error.

Verify with `readelf -A uhid.ko` (expect `Tag_CPU_arch: v7`) and
`modinfo uhid.ko` (expect vermagic `3.0.35-lab126 preempt mod_unload ARMv7 p2v8`).
