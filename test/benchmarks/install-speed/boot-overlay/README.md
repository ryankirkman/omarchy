# Unattended experimental installer overlay

This helper adds a tiny benchmark appendix to a copy of the official live initramfs. The release ISO and its squashfs remain unchanged. It exists to test installer source changes before rebuilding a release image, with the same boot mechanism available for a matched control. This is benchmark tooling, not a production boot configuration.

The Linux kernel accepts concatenated compressed and uncompressed `newc` archives in one initramfs buffer ([kernel format specification](https://docs.kernel.org/driver-api/early-userspace/buffer-format.html)). We inspected the actual Omarchy 4.0.2 initramfs: `/init` calls `archiso_mount_handler /new_root`, then all `LATEHOOKS`, then `switch_root`; `/config` contains `LATEHOOKS="archiso_pxe_common plymouth"`. The appendix preserves that configuration and adds one hook. This hook copies a small payload into the writable live root before systemd starts, retaining the original entry point as `/root/.automated_script.benchmark-original.sh`.

The wrapper runs a preflight script on tty1 before the original entry point can start autoinstall. A failed preflight never starts the installer. A `control` image records a no-op preflight and runs the original script. A `candidate` image activates the supplied installer source and read-only image media first. A `builder` image runs its preflight and suppresses autoinstall for that boot. Builder SSH accepts a disposable public key; private keys belong only on the host.

## Generate an image

The host needs Python 3 and `zstd`; no host mounting or root capabilities are required. Extract `/arch/boot/x86_64/vmlinuz-linux-t2` and `/arch/boot/x86_64/initramfs-linux-t2.img` from the official ISO with `xorriso -osirrox on -indev ISO -extract SOURCE DESTINATION`. For release 4.0.2, the ISO SHA256 is `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8` and the extracted initramfs SHA256 is `6e3e15b983da69df4e18df2f1489fa854980b395b28546355d0f6dc13914694e`.

```bash
python test/benchmarks/install-speed/boot-overlay/make-initramfs.py \
  --initramfs /tmp/omarchy-bench/initramfs-linux-t2.img \
  --expected-initramfs-sha256 6e3e15b983da69df4e18df2f1489fa854980b395b28546355d0f6dc13914694e \
  --mode control --output /tmp/omarchy-bench/initramfs-control.img
```

Candidate mode additionally takes `--preflight-script test/benchmarks/install-speed/image/candidate-preflight.sh` and `--payload-dir TREE`. Place the sibling image tool's `activate-installer-overlay.sh` at `TREE/usr/local/lib/omarchy-benchmark/activate-installer-overlay.sh`. Attach its supplementary ISO as a read-only CD-ROM with label `OMARCHY_FAST_IMAGE`. Its qemu-img binary bundle, installer source archive and root image remain outside the initramfs; the candidate preflight mounts and verifies them. Do not attach real host disks.

For builder mode, use `--preflight-script test/benchmarks/install-speed/boot-overlay/builder-preflight.sh` and a payload containing `usr/local/lib/omarchy-benchmark/builder-key.pub`, generated with a new temporary SSH keypair. The VM runner supports `--mode builder --guest-user root --ssh-key KEY` and waits for this key before accepting mailbox commands. This mode does not count as a timed installation.

The output's adjacent `.manifest.json` records the original initramfs digest, config and hook digests, each payload file's content and permissions, and the appended and final image digests. Record the extracted kernel digest and the complete QEMU command line in each run manifest as well. All large generated images and mutable VM disks should live under `/tmp`.

### Optional package-prefetch experiment

`--disable-package-prefetch` exports the existing upstream `OMARCHY_NO_PREFETCH=1` switch before the original automated script starts. The default stays unchanged. This option works for either control or candidate, records `disable_package_prefetch` in the manifest, and changes the wrapper's content digest. It does not disable or bypass root-image verification: the candidate preflight still runs first, and the upstream installer still requires successful verification. Generate this as a separate candidate artifact and measure it against the ordinary candidate; do not silently replace the first candidate or claim a performance benefit before measurement. The experiment asks whether reading the old package mirror competes with root-image verification/restoration for storage bandwidth and page cache.

## Direct boot and the first installed boot

Use the official extracted kernel, the generated initramfs, and the same command line in both benchmark groups:

```text
archisobasedir=arch archisosearchuuid=2026-08-31-03-24-58-00 quiet splash xe.enable_panel_replay=0 initramfs_async=0 copytoram=n
```

Do not leave `-kernel/-initrd/-append` enabled for the installed system's reboot. OVMF's [PlatformBootManagerAfterConsole implementation](https://github.com/tianocore/edk2/blob/edk2-stable202402/OvmfPkg/Library/PlatformBootManagerLib/BdsPlatform.c) calls `TryRunningQemuKernel()` on every firmware boot before normal disk boot selection. That would enter the live installer again. Use QEMU's [`-no-reboot`](https://www.qemu.org/docs/master/system/qemu-manpage.html), then have the same supervisor relaunch the same target disk and NVRAM without direct-kernel arguments. Keep host monotonic timing continuous and record this transition. Apply the identical transition to control and candidate. Only accept the run after validating the installed root and package manifest.

Booting via a supplied kernel instead of the ISO's GRUB loader changes the fixture. Keep the unmodified official-ISO run as a separate reference. Compare candidates against fresh direct-boot controls, and disclose this boundary. Root-image verification and all required install phases must still run; this helper must not turn a failed verification into an install.

## Verification status

`python test/benchmarks/install-speed/boot-overlay/contract-test.py` checks the archive, fail-closed entry point, actual payload copying, and package-prefetch flag propagation. Building against the real official initramfs succeeded; the no-op control appendix is approximately 5 KiB.

The actual builder live boot passed in QEMU TCG with four CPUs and 8 GiB RAM. The hook preserved the original automated entry point byte-for-byte against the read-only squashfs copy and preserved `/root` mode `0750`. All eight payload files matched their expected SHA256 digests and permissions. The builder preflight completed, SSH became active, and autoinstall remained disabled. [The raw commands, results, fixture, and artifact digests are recorded here](results/builder-smoke.json).

Candidate activation and a subsequent installed-system boot remain separate integration gates. This successful builder smoke test establishes that the unattended injection works; it is not an installation speed result.
