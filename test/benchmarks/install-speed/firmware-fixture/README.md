# Firmware boot fixture

`repack-iso.py` prepares an alternative to direct QEMU `-kernel/-initrd` boot. It puts an existing benchmark initramfs into a copy of the pinned Omarchy 4.0.2 ISO, preserving the release's firmware and GRUB boot chain. This is experimental benchmark tooling; it is not an optimized production ISO or a speed result.

## Inspected release entrypoints

The release SHA256 is `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Its hidden 24,117,248-byte FAT EFI El Torito image starts at ISO LBA 3,029,103. Its embedded GRUB configuration locates `/boot/2026-08-31-03-24-58-00.uuid` and loads the external `/boot/grub/grub.cfg`. That file defaults immediately to `/arch/boot/x86_64/vmlinuz-linux-t2` and `/arch/boot/x86_64/initramfs-linux-t2.img`. BIOS Syslinux loads the same paths.

Repacking replaces only the initramfs. `xorriso -boot_image any replay` reconstructs the original BIOS/UEFI records and retains the original EFI image. Creation/modification timestamps and the ISO UUID stay `2026083103245800`, because the unchanged kernel command line searches for that UUID. The original kernel command line does not include the direct fixture's additional `copytoram=n`; do not mix those fixtures in a comparison.

## Build and verify

Create the control, candidate, or builder initramfs with [the existing overlay generator](../boot-overlay/README.md). Use a machine with sufficient disk space: each output is another approximately 6.2 GB ISO. The repacker checks available space and retains an additional 8 GiB by default. Store large outputs under `/tmp` locally, or the native runner's temporary work directory.

```bash
python test/benchmarks/install-speed/firmware-fixture/repack-iso.py \
  --source /tmp/omarchy-bench/downloads/omarchy-4.0.2.iso \
  --initramfs /tmp/omarchy-bench/initramfs-benchmark-control-v3.img \
  --output /tmp/omarchy-bench/omarchy-control-firmware.iso
```

The helper accepts only the pinned original release, requires the adjacent initramfs provenance manifest, and never overwrites an output. After repacking it hashes every regular file through ISO extents, including the multi-extent squashfs. All original contents must match byte-for-byte except the supplied initramfs and ISOLINUX's layout-dependent boot-info-table at bytes 8–63. It additionally requires a byte-identical embedded FAT EFI image, identical BIOS/UEFI boot entry parameters, and unchanged volume identity. A failed output has no success manifest and is retained for diagnosis. The manifest deliberately records `verified-content-not-yet-boot-tested` until a real boot establishes that separate gate.

The focused contract uses the official firmware metadata to generate two small, deliberately unbootable ISO fixtures. It runs the real repacker and rejects deliberate squashfs and EFI corruption, without duplicating the 6 GB release:

```bash
python test/benchmarks/install-speed/firmware-fixture/contract-test.py \
  --source /tmp/omarchy-bench/downloads/omarchy-4.0.2.iso
```

For the extracted local toolchain, set `LD_LIBRARY_PATH=/tmp/omarchy-bench/toolchain/usr/lib/x86_64-linux-gnu` and pass `--xorriso /tmp/omarchy-bench/toolchain/usr/bin/xorriso` to either command.

The real xorriso contract passed on September 5, 2026: 91 regular files verified, volume identity preserved, and both corruption cases rejected. The 24,117,248-byte EFI image retained SHA256 `c9ed43cb902a30992cc4f43e2086ae54b83f868e22b9195f30e5d41a48103b84`. The miniature output was 59,113,472 bytes. This checks repacking and verification only; it is not a real firmware boot or a full installation.

## Matched installation protocol

The native driver supports this protocol with `--boot-method firmware`. Both control and candidate boot their repacked ISO through OVMF/GRUB with identical device topology: target disk at boot index 1, installer CD-ROM at index 2, matching CIDATA and supplemental media. There are no direct kernel arguments and no QEMU exit/relaunch on the installer reboot. Measure from initial QEMU start to the first successful installed-system SSH probe, retaining the existing uncertainty bounds and full phase/package checks.

Then require a separate standalone boot acceptance gate outside the installation clock. In the same running QEMU process, eject the installer and supplemental CD-ROMs and disconnect the CIDATA USB device, recording each acknowledged action. Give the USB device an explicit QEMU device ID so `device_del` can remove it; a nonremovable disk backend may reject `eject`. Request a normal guest reboot and require successful SSH into the installed Btrfs root, changed kernel boot ID, unchanged machine identities, and all source media absent. Exact package versions, reasons and complete file checks are collected before this additional reboot. Preserve the disk, NVRAM, CPU, memory, and QEMU process across this gate. Accept the timed sample only after this gate succeeds, and report its duration separately.

Do not race CD-ROM ejection against QMP `RESET`, pause the guest to remove media during the timed reboot, or make a successful reboot depend on manually choosing a boot entry. Do not combine samples from this fixture with the direct-kernel fixture. Keeping the firmware path normal is the experimental variable; standalone validation must not reintroduce the cold QEMU relaunch being investigated.
