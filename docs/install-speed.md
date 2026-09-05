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

Primary timing is the existing guest `/var/log/omarchy-install-timing.json`, from installer start through successful completion, with every phase recorded. Separately record host monotonic time from ISO boot to the first verified installed-system SSH response and the readiness polling interval. An optimization must not appear faster by moving work into boot, hiding verification time, postponing required setup, using a warmed installed disk, changing security parameters, or reducing installed functionality.

The [supervised VM runner](../test/benchmarks/iso-vm.md) uses the official unattended installation inputs and collects evidence after verifying that the installed Btrfs root has booted. Keep mutable VM disks under `/tmp`, outside synced checkouts: copying an actively growing disk can exhaust storage and invalidate a run. The runner records interventions and rejects unexpected pauses as valid measurements.

The comparison tool consumes run directories containing `manifest.json`, `install-timing.json`, `validation.json`, `package-manifest.txt`, and `package-explicit.txt`. It rejects unbooted, incomplete, interrupted, reused, different-hardware and different-package samples, or failed package-file verification. Package versions and explicit/dependency installation reasons must match. Encryption, filesystem, unattended configuration and I/O settings must match; repetitions of a revision must use identical ISO and overlay digests. Guest installer time and complete boot-to-SSH time are separate measurements, because a candidate must not appear faster by moving verification into boot.

```bash
python3 test/benchmarks/compare-installs.py \
  --baseline /tmp/baseline-1 /tmp/baseline-2 /tmp/baseline-3 \
  --candidate /tmp/candidate-1 /tmp/candidate-2 /tmp/candidate-3 \
  --output /tmp/install-comparison.json
bash test/shell.d/install-comparison-test.sh
```

The available development environment is an Ubuntu container with no KVM device and no mount privileges. A disposable QEMU VM using software emulation is being used to exercise the real installer. Its results must remain labeled as software-emulation measurements, especially on a shared CPU host. No independently measured twofold complete-install result is recorded in this document yet.
