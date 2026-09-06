# Use image verification as the image prefetch pass

This optional downstream patch applies to [omacom-io/omarchy-iso PR #145](https://github.com/omacom-io/omarchy-iso/pull/145), pinned to `dbffaa6c65344d644627a023c28661e08382b8fa`. The upstream contributors implemented the prebuilt Btrfs/qcow2 installer and background source-image verification. This patch changes only speculative prefetch selection; it is not the source of the image-install architecture. The upstream MIT notice is preserved in [../image/LICENSE.omarchy-iso](../image/LICENSE.omarchy-iso).

The pinned live entry point still looks for `omarchy-root.btrfs.zst`, although the installer restores `omarchy-root.btrfs.qcow2`. After waiting for verification, it consequently warms package archives. On image installs, those reads can compete with restoration for media bandwidth and displace cached image pages. This is a hypothesis to measure, not an established speedup. Verification reads the whole qcow2, but memory pressure may evict some of it; the patch does not promise that every later read hits the cache.

## Apply and enable

In a checkout of the pinned ISO source:

```bash
git apply --check /path/to/experimental-verify-only-prefetch.patch
git apply /path/to/experimental-verify-only-prefetch.patch
```

Build the ISO with that entry point, then set exactly `OMARCHY_EXPERIMENTAL_VERIFY_ONLY_PREFETCH=1` in the live entry point's inherited environment before it starts. With a qcow2 payload present, the speculative warmer returns immediately. The independent checksum service continues normally. Package-only media keep the existing mirror prefetch policy, including its memory budget. Unset or other values preserve the pinned behavior; the existing `OMARCHY_NO_PREFETCH=1` still disables prefetch for every medium.

Image presence selects the cache policy and does not certify the image. The patch leaves both source-verification gates intact: the full-disk orchestrator still verifies the checksum and qcow2 structure before installation, and the free-space configurator still waits for source verification before partitioning. A missing checksum, failed hash, or invalid qcow2 must still stop installation. No package selection, target writes, encryption parameters, or installed setup changes.

## Focused validation

Run from the Omarchy checkout containing this patch:

```bash
python3 test/benchmarks/install-speed/prefetch/contract-test.py /path/to/omarchy-iso
```

The test reads the exact source commit using Git, applies the patch to a temporary copy, checks Bash syntax, and executes the actual prefetch function against disposable files. It covers image and package-only media, default behavior, explicit opt-in, the global disable switch, low memory, and unfinished/failed verification. Source paths are redirected into the fixture; it never invokes the installer or touches a target disk. It does not modify the supplied checkout.

The unchanged upstream `test/unit/wait-root-image-verify-test.sh`, `test/unit/free-space-gate-test.sh`, `test.unit.test_root_image.VerifyRootImageStreamTest`, and `test.unit.test_root_image.PrepareInstallTargetTest` cover the verification verdict and ordering separately. Run those in the pinned ISO checkout as part of evaluating the patch. On September 5, 2026, all six prefetch tests, both shell gate tests, and all seven selected Python verification/ordering tests passed. These tests are correctness checks, not installation measurements.

## Measure separately from the running experiment

The native benchmark's `OMARCHY_NO_PREFETCH=1` treatment already tests whether removing speculative reads helps its image-backed fixture. This new conditional patch is not injected into that running fixture. If the treatment wins, validate this exact patch in a later complete-image build and paired installs; its package-only behavior differs intentionally from the global-disable treatment. Keep verification inside the full boot-to-installed-readiness timing, retain identical package versions/reasons and media/cache settings, and report cold removable-media results separately from cache-conditioned VM runs.
