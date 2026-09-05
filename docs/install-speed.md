# Install speed experiments

The optimization target is a complete successful installation into a fresh VM disk, with the same package names and versions, a successful installed-system boot, and no missing package files. Component benchmarks are useful for finding waste, but do not satisfy that target. The initial source baseline is `e8e92c5092c9bbbf3d7fc5240f8551fd1eeaced9` on the fork's `quattro` branch.

## Scope and ownership

This repository supplies the target's configuration and setup commands. Current ISO orchestration belongs to [omacom/omarchy-iso](https://github.com/omacom/omarchy-iso). The package extraction and filesystem deployment changes needed for large complete-install gains therefore span both repositories. [The upstream research record](install-speed-upstream.md) distinguishes other authors' measured results from the experiments performed here.

Upstream ISO [PR #145](https://github.com/omacom/omarchy-iso/pull/145), building on [PR #113](https://github.com/omacom/omarchy-iso/pull/113), already reports a much faster approach: construct the invariant package filesystem at ISO build time, restore its compressed block image, then configure the machine and user. Its authors report 63.961 seconds versus 14.526 seconds in a KVM fixture. These are their results, not a benchmark result produced by this fork. The inspected candidate is pinned at `dbffaa6c65344d644627a023c28661e08382b8fa`.

## Changes enabled in this fork

Package presence predicates submit all requested names in one read-only pacman query. Package installation also verifies a successful transaction in one query; if that verification fails, it still identifies the first missing package. Empty arguments, transaction failures and false-success package-manager exits retain their behavior. The optimization does not change which packages are installed or parallelize package-manager writes.

Setup logging formats timestamps with Bash's built-in clock formatter rather than launching two `date` processes per setup leaf. Child-shell isolation, exit statuses, the log's timestamp format, output and failure records remain intact.

| Measured component | Before | After | Ratio |
| --- | ---: | ---: | ---: |
| Five-package add check and verification, already installed | 484.6 ms | 107.2 ms | 4.52× |
| Sixteen-package add check and verification, already installed | 1,362.5 ms | 125.9 ms | 10.82× |
| Logging around 80 trivial setup leaves | 934.5 ms | 429.6 ms | 2.18× |

These measurements ran real executables, with alternating before/after order, and preserve raw samples in `test/benchmarks/results/`. The package test uses real pacman 6.0.2 against a private synthetic database with 1,500 entries. It does not install packages. The logging test deliberately uses trivial leaves to isolate its overhead. Shared CPU contention produced visible outliers and some regressions in single-target/missing-first cases; those observations are retained, not discarded. None of these numbers is a complete-install result.

Reproduce with Bash 5, Python 3, Git and pacman:

```bash
python3 test/benchmarks/pkg-query.py --baseline-ref e8e92c5092c9bbbf3d7fc5240f8551fd1eeaced9 --output /tmp/package-query.json
python3 test/benchmarks/install-logging.py --baseline-ref e8e92c5092c9bbbf3d7fc5240f8551fd1eeaced9 --output /tmp/install-logging.json
bash test/shell.d/pkg-query-test.sh
bash test/shell.d/logging-test.sh
```

## Further experiments

The [Btrfs UUID experiment](../test/benchmarks/install-speed/uuid/README.md) measures a faster filesystem identity change after block-image restoration. It achieved 4.12× for that step on a real small Btrfs image, saving about 87 ms. Full data-checksum checking and restored-content/metadata comparisons passed. Its patch remains opt-in because simultaneous clone mounts, installed boot and snapshot rollback have not been validated.

The [checksum comparison](../test/benchmarks/install-speed/checksum-findings.md) rejected replacing the ISO's SHA-256 checker with OpenSSL. Both actual Arch binaries already use OpenSSL; complete-image measurements showed essentially no gain. The shipping checksum/failure behavior is unchanged. A separate [chunk-verification prototype](../test/benchmarks/install-speed/hash/README.md) changes the manifest format and remains experimental; its modest host-only result does not justify default integration.

## Complete-install measurement

Download the official 4.0.2 ISO and its checksum from [the release](https://github.com/omacom/omarchy/releases/tag/v4.0.2). The image used here is 6,227,752,960 bytes, SHA-256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Record exact Omarchy and ISO revisions and ISO digests for each treatment. Fresh disk and firmware state, vCPU count, memory, acceleration, package inputs and I/O cache policy must match between treatments. Alternate run order; retain unsuccessful runs as failures rather than timings.

The primary full-install clock is host monotonic time from QEMU start to the first verified installed-system SSH response. Retain the actual failed/successful probe bracket. The existing guest `/var/log/omarchy-install-timing.json` records installer duration and phases separately. An optimization must not appear faster by moving work into boot, hiding verification time, postponing required setup, using a warmed installed disk, changing security parameters, or reducing installed functionality.

The [supervised VM runner](../test/benchmarks/iso-vm.md) uses the official unattended installation inputs and collects evidence after verifying that the installed Btrfs root has booted. Keep mutable VM disks under `/tmp`, outside synced checkouts: copying an actively growing disk can exhaust storage and invalidate a run. The runner records interventions and rejects unexpected pauses as valid measurements.

The comparison tool consumes run directories containing `manifest.json`, `install-timing.json`, `validation.json`, `package-manifest.txt`, and `package-explicit.txt`. It rejects unbooted, incomplete, interrupted, reused, different-hardware and different-package samples, or failed package-file verification. Package versions, explicit/dependency installation reasons and per-package file counts must match. The file-count check catches damaged package databases that otherwise report a misleading successful `pacman -Qk` with zero files. Encryption, filesystem, unattended configuration and I/O settings must match; repetitions of a revision must use identical ISO and overlay digests. Guest installer time and complete boot-to-SSH time are separate measurements, because a candidate must not appear faster by moving verification into boot.

```bash
python3 test/benchmarks/compare-installs.py \
  --baseline /tmp/baseline-1 /tmp/baseline-2 /tmp/baseline-3 \
  --candidate /tmp/candidate-1 /tmp/candidate-2 /tmp/candidate-3 \
  --output /tmp/install-comparison.json
bash test/shell.d/install-comparison-test.sh
```

The available development environment is an Ubuntu container with no KVM device and no mount privileges. A disposable QEMU VM using software emulation exercises the real installer locally. Its results remain labeled as software-emulation measurements. The [native experiment](../test/benchmarks/install-speed/native-ci/README.md) also runs on standard public GitHub runners after an actual KVM creation check. No independently measured twofold complete-install result is recorded in this document yet.

## First successful calibration install

The official 4.0.2 ISO completed a real fresh installation on September 5, 2026: QEMU 8.2.2, four emulated CPUs, 8 GiB RAM, a fresh 40 GiB qcow2 disk, fresh UEFI variables, Btrfs and no encryption. All 14 installer phases succeeded. The installed `/dev/vda2[/@]` root booted, all 941 installed packages were inventoried, and `pacman -Qk` returned zero. [The retained evidence](../test/benchmarks/install-speed/results/iso-calibration-4.0.2-2026-09-05/) includes original timings and validation records.

| Calibration measurement | Seconds | Share of installer time |
| --- | ---: | ---: |
| Installing Arch + Omarchy | 927.58 | 71.3% |
| Finalizing Limine boot | 229.88 | 17.7% |
| Finalizing user | 64.33 | 4.9% |
| Configuring system | 56.05 | 4.3% |
| Remaining phases | 22.55 | 1.7% |
| Complete guest installer | 1,300.38 | 100% |
| VM start to first installed SSH response | 1,597.45 | Separate host clock |

This is a calibration result, not a paired speedup measurement. It uses the ISO's original firmware/GRUB boot path. The experimental candidate needs an appended-initramfs overlay, so its final comparison requires fresh controls with the same boot fixture. The native driver supports direct-kernel boot and a verified derived ISO that preserves the release firmware/GRUB path. The native workflow now selects the firmware path and verifies cold source pages for every control and candidate. Its separate acceptance gate requires another reboot in the same QEMU process after all installation media has been removed; this gate cannot change the original installation clock. This earlier runner also did not record the newer readiness-bound and media-preconditioning fields; those must not be retroactively invented. Post-boot identity and service diagnostics are marked as later collection.

The profile supports moving invariant package installation into the reusable image and overlapping independent finalization work. Package installation alone accounts for over fifteen minutes here; boot finalization remains a substantial cost after that is removed. The candidate must retain the same package versions and installation reasons, complete all per-machine setup, pass file checks and boot independently without installation media.

## Native firmware control and current candidate tests

[Actions run 33989571580](https://github.com/ryankirkman/omarchy/actions/runs/33989571580) completed a valid native KVM calibration, image build and first paired-series control. Both installations used four vCPUs, 8 GiB RAM, fresh unencrypted 40 GiB targets and normal firmware boot. Source-page residency was verified as zero before timing. All 941 package-file count rows and the separate reboot with installation media removed passed. [The retained evidence](../test/benchmarks/install-speed/results/kvm-attempts/33989571580/) preserves the original records and hashes.

| Valid native sample | Host readiness interval | Guest installer |
| --- | ---: | ---: |
| Calibration, no supplementary media | 194.206–198.631 s | 137.201 s |
| First control, matched supplementary media | 186.644–189.075 s | 131.542 s |

The calibration and paired control have different media topology; they are not interchangeable samples. The reusable image build took 787.080 seconds outside installation timing. Its exact 941-package inventory, package-file checks, full Btrfs data checksums, compressed-image check and complete logical comparison passed.

The first native candidate failed, leaving no completed comparison or speedup result. Its older artifact lacks the precise failure diagnostic. A [separate local smoke test](../test/benchmarks/install-speed/results/local-image-smoke-2026-09-05/) found that a fresh log subvolume hid package-owned directories from the restored image. The restore correction seeds each image-backed child mount from its source subtree before mounting it. The corrected media retains the same sealed root-image bytes. A fresh software-emulated rerun has since passed package-file and standalone reboot gates; it provides functional validation without a matched speedup comparison. This local diagnosis does not establish the native failure's cause.

[Follow-up run 33991603136](https://github.com/ryankirkman/omarchy/actions/runs/33991603136) failed during initial calibration, before building an image or measuring any candidate. SSH remained unreachable at the 1,800-second deadline, although the post-failure tty2 screenshot showed the installed Omarchy login prompt. The [preserved failure evidence](../test/benchmarks/install-speed/results/kvm-attempts/33991603136/) does not establish the SSH failure's cause. A separate read-only rescue failed to mount the target and recovered no network state or service journal. Updating the rescue's obsolete `nologreplay` spelling improves diagnostics; it does not establish or fix the original SSH cause. This attempt supplies no accepted installation timing.

The next selected native trial focuses on package prefetch disabled plus the [release-gated reboot experiment](../test/benchmarks/install-speed/fast-reboot/README.md), then the same candidate with [early image verification](../test/benchmarks/install-speed/early-verifier/README.md). Each receives three fresh alternating control/candidate pairs with firmware boot and verified cold source pages; the early variant has its own matched early-activation control. Full image verification remains inside the complete host clock. The trial explicitly selects `--install-timeout 600`, while the driver default remains 1,800 seconds. A deadline failure remains invalid and its evidence is retained. No paired speedup result is claimed yet.
