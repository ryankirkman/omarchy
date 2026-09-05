# Upstream installation-speed research

Research date: 2026-09-05. This report records primary GitHub sources read during the installation-speed investigation. Performance figures in this document are upstream authors' measurements, not results independently reproduced in this checkout. Open/closed states describe the retrieval date.

## Repository boundary

The operating-system installer, ISO builder, and disposable QEMU install harness live in [omacom/omarchy-iso](https://github.com/omacom/omarchy-iso), formerly `omacom-io/omarchy-iso`, on its `quattro` default branch. This `omarchy` repository supplies system and user setup, configuration, hardware scripts, and package manifests. Whole-install image restoration therefore requires corresponding changes to the ISO project. Keep an exact upstream source pin and a reproducible downstream patch when experimenting from this fork.

## Direct performance work

| Source | Evidence | Implication |
| --- | --- | --- |
| [ISO PR #113: prebuilt Btrfs root](https://github.com/omacom/omarchy-iso/pull/113) | The author reports roughly 40 package extractions per second, about 34 seconds in the package phase, a 1–32 vCPU sweep flat above four cores, and no benefit from combining seven pacstrap calls into three. A same-VM comparison reports 43 seconds versus 24 seconds with the same 942-package set. | Move invariant package extraction to ISO build time. Merely increasing CPU count or combining package transactions does not remove the serial extraction bottleneck. |
| [ISO PR #145: compressed qcow2 and parallel finalization](https://github.com/omacom/omarchy-iso/pull/145) | Reports official 4.0.2 at 63.961 seconds versus 14.526 ± 0.610 seconds, approximately 4.4×, on the same 8-vCPU, 8-GiB QEMU/KVM fixture. Every install used a new 40-GiB raw target, ISO and target `cache=none`, 941/941 packages, and a successful boot. | Use a prebuilt Btrfs filesystem carried in zstd-compressed qcow2, parallel block restoration, and independent finalization branches. This work belongs to the upstream contributors and must be credited. |
| [PR #145 compression controls](https://github.com/omacom/omarchy-iso/pull/145#issuecomment-5508154117) | A reviewer compared the parent Btrfs send stream with outer zstd, parallel pzstd, and no outer compression. Installs remained approximately 37.3–37.9 seconds. | Serial `btrfs receive` replay, rather than decompression, dominated that design. The qcow2 block restore removes this serialization. |
| [PR #145 compressed clusters and boot timing](https://github.com/omacom/omarchy-iso/pull/145#issuecomment-5508825798) | Compressed qcow2 reduced ISO size while slightly improving restore time. One measured 15.7-second installer interval was 33.0 seconds from VM boot to completion after including preinstall image verification. | Record both installer time and boot-to-complete time. Source verification can dominate on slow media; a projected 3.8-GB read at 33 MB/s alone takes about 115 seconds. That projection is not a hardware measurement. |
| [PR #145 out-of-order restore experiment](https://github.com/omacom/omarchy-iso/pull/145#issuecomment-5509588777) | Three runs per configuration report unpack time 6.49 ± 0.25 seconds versus 5.64 ± 0.07 seconds with `qemu-img convert -W`. | Out-of-order completion is appropriate for this fresh non-backed target, because disjoint output offsets do not depend on completion order. |
| [ISO PR #108: squashfs root image](https://github.com/omacom/omarchy-iso/pull/108) | Reports a cheap Intel 120U laptop improving from 67 seconds to 43 seconds by replacing package extraction with parallel `unsquashfs`. | Alternative image format with a smaller ISO and more modest measured gain. Review discussions identify ACL, crypttab, unsupported-layout, accessibility, and process-cleanup hazards to preserve in any implementation. |
| [ISO PR #134: leaderboard plan](https://github.com/omacom/omarchy-iso/pull/134) | Proposes schema-versioned monotonic timing, run IDs, observed hardware/install classes, and signed opt-in results. | Useful benchmark design, but this is a plan rather than an implemented timing service. |
| [ISO PR #154](https://github.com/omacom/omarchy-iso/pull/154) and [#155](https://github.com/omacom/omarchy-iso/pull/155) | Add unattended QEMU CIDATA boot support and reusable configurator outputs for the standard disposable 40-GiB target. | Useful reproducibility support for repeated full installations. |

### Exact PR #145 reproduction pin

- Source: `AdamMusa/omarchy-iso`, branch `perf/deterministic-sub-30-install`, commit `dbffaa6c65344d644627a023c28661e08382b8fa`.
- The PR is stacked on #113; its overall performance improvement includes its parent's image work.
- Build command reported by the author: `./bin/omarchy-iso-make --keep-pkg-cache --no-boot-offer`.
- Reported artifact: `omarchy-2026.09.03-x86_64-quattro.iso`, 7,019,702,272 bytes, SHA-256 `2047c2eb8e6645623b1b4228e16ad5bbe58d8dfcc255f8508a9ba2e68bf3a7c4`.
- Reported embedded qcow2: 3,667,152,896 physical bytes and 6,174,015,488 virtual bytes, SHA-256 `5aed9aa081d4db56451144d2252c3528818b821e80f0b8b70893bfb028d3d6f9`.
- Example author invocation: `./bin/omarchy-iso-test release/omarchy-2026.09.03-x86_64-quattro.iso --install-only --no-preview --memory 8192 --disk-format raw`.
- [Upstream evidence release](https://github.com/AdamMusa/omarchy-iso/releases/tag/sub-30-install-evidence-20260901).

The source builds zstd-compressed qcow2 with 1-MiB clusters, restores using `qemu-img convert -O raw -n -W` with twice the CPU count capped at 16 coroutines, retains the fresh LUKS mapper across filesystem setup to avoid two redundant Argon2id passes, and runs boot/UKI finalization concurrently with ordered user/login/SSH/Tailscale/DNS setup in separate mount namespaces. The encrypted interactive comparison is an incremental 37.560 seconds to 32.204 seconds; the published 4.4× whole-install comparison is unencrypted. These are VM-specific measurements, not guarantees for real hardware or USB media.

## Other relevant issues and constraints

| Source | Observation | Constraint or opportunity |
| --- | --- | --- |
| [Omarchy issue #6330](https://github.com/omacom/omarchy/issues/6330) | A Taipei reporter measured `extra.db` at 8.7 MB in 61 seconds, about 142 KB/s, and reports 30–40+ minute updates with Cloudflare `DYNAMIC` responses. No comments or independent confirmation were present. | Caching immutable versioned archives at the CDN could improve online bootstrap; this requires upstream infrastructure access. Do not substitute live Arch mirrors for staged stable packages without proving package-set compatibility. |
| [Omarchy issue #9064](https://github.com/omacom/omarchy/issues/9064) | Reports stable mirror databases several days behind Arch. | Distinguish intentional staging from a broken mirror; faster downloads must not silently alter the package snapshot being measured. |
| [Omarchy issue #7704](https://github.com/omacom/omarchy/issues/7704) | An apparent missing offline package was traced to a corrupt media read: the offline repository and writable pacman cache share a directory, so `--noconfirm` accepts deletion of an invalid package from the source. Another reporter reproduced this with `nvidia-utils`. | Keep source artifacts recoverable and integrity failures explicit. Repeated attempts in the same live overlay otherwise fail after the source package has been deleted. The thread also shows incomplete configuration can leave SDDM enabled before working login setup. |
| [Omarchy PR #3977](https://github.com/omacom/omarchy/pull/3977) | Closed without merge; maintainer rejected default `MAKEFLAGS=-j$(nproc)` because some builds have broken parallel dependencies and some machines lack memory for one worker per core. | Unbounded parallel compilation is not a safe general installer optimization. |
| [Omarchy PR #9880](https://github.com/omacom/omarchy/pull/9880) | Removes an obsolete Apple SPI DKMS package while retaining the initramfs drop-in for the in-tree `applespi` module. | Avoiding unnecessary failed builds is useful for matching MacBooks, but the keyboard modules needed at the LUKS prompt must remain. This is a narrow hardware improvement. |

## Correctness and benchmark requirements

- Validate the source image, supported layout, and available capacity before destructive operations, including the configurator's free-space/protected flow.
- Preserve the package inventory and versions; package count alone cannot prove equivalent contents. Compare manifests and package-owned files as well as successful boot.
- Preserve per-install machine identity, SSH host keys, pacman master key, Btrfs UUID, capabilities, ownership, modes, ACLs, nested subvolumes, accessibility support, and hardware-specific drivers.
- Require the selected hardware kernel's UKI. A generic Linux UKI must not falsely validate a T2 installation.
- Independent finalization branches must use isolated mount namespaces and avoid shared package-manager, boot-image, or configuration mutations.
- Leave LUKS parameters and encryption strength unchanged. Reusing an already-open fresh mapper removes redundant computation without weakening the password KDF.
- Use identical fresh target format, cache mode, CPU, RAM, package snapshot, and source medium for baseline and candidate. Report repeated-run dispersion and both installer-only and boot-to-complete measurements; separate encrypted and unencrypted classes.
- Earlier source revisions were corrected for shared pacman master keys, omitted documentation, validation ordering, and hardware kernel checks. Do not cherry-pick an early image implementation without its fixes.
- Treat upstream reports as leads until independently reproduced. A microbenchmark improvement must not be described as a full-install improvement.
