# Btrfs filesystem UUID experiment

This is a new measured optimization candidate for the image-restore installer in [upstream ISO PR #145](https://github.com/omacom/omarchy-iso/pull/145). It measures the filesystem-identity step only. No complete installation, boot, mounted filesystem, or snapshot rollback was measured here, and the experiment remains disabled by default.

## Result

On a real unmounted Btrfs image populated from 1,773 tracked Omarchy repository files (76,237,951 bytes), changing the filesystem ID through `metadata_uuid` was **4.12× faster for this stage** than rewriting it through every metadata block. The median saving was **87.0 ms** on this small fixture. Do not multiply this ratio by total installation time or extrapolate it to a full distribution.

| Method | Repetitions | Median | Mean ± sample SD | Range |
| --- | --- | --- | --- | --- |
| Baseline `btrfstune -f -u` | 3 | 114.9 ms | 129.1 ± 45.5 ms | 92.3–179.9 ms |
| Experimental `btrfstune -f -m` | 3 | 27.9 ms | 25.7 ± 7.5 ms | 17.4–31.9 ms |

The recorded fixture used Btrfs tools 6.6.3 and a 1-GiB sparse regular file. Treatments alternated order between repetitions. Every trial began with a fresh copy of the same baseline image; copying happened outside the timer. The timer includes the real `btrfstune` command and an explicit `fsync` of the image. These are local file operations with warmed caches, not physical-disk or LUKS timings. Raw records and verification output are in `results-local.json`.

The benchmark checked each restored image with `btrfs check --readonly --check-data-csum`, recovered its files using `btrfs restore`, and compared the entire restored tree's SHA-256 digests, sizes, modes, ownership, symlink targets, and directory structure with the original corpus. Every check passed. All six changed filesystem IDs differed from the original and from each other. The experiment retained the original metadata identity and enabled the corresponding incompatibility feature; the baseline retained its original feature flags and rewrote its device metadata identity.

## Why this can be faster

The existing installer calls `btrfstune -f -u` after restoring its block image, correctly preventing two installed disks from sharing one filesystem ID. That command visits and rewrites metadata blocks. The [Btrfs project's manual](https://btrfs.readthedocs.io/en/latest/btrfstune.html) documents `-m` as the faster alternative: it changes the mount-visible filesystem identity using a superblock feature, retaining the old identity inside metadata blocks. The feature requires Linux 5.0 or later and supports mounting by the new UUID. It does not weaken file checksums or encryption, but it changes filesystem compatibility, which is why the patch is opt-in.

Full `-u` leaves the unused `metadata_uuid` superblock field zero on this fixture; it is not expected to copy the new filesystem ID into that unused field. The benchmark checks actual device identity and feature flags instead. This distinction was verified against real command output before recording the successful results.

## Reproduce the stage measurement

Install `btrfs-progs`, Python 3.11 or newer, Git, and GNU coreutils. Run from this repository's root:

```bash
python3 test/benchmarks/install-speed/uuid/benchmark.py \
  --repeats 5 \
  --output test/benchmarks/install-speed/uuid/results-new.json
```

The benchmark uses `mkfs.btrfs --rootdir` on a temporary regular file and never mounts it or opens a host block device. It copies tracked files from the source checkout and records its commit, tracked diff hash, file count, and content manifest hash. A different repository can be supplied with `--source`; any such result must report that changed corpus. `--scratch` selects a parent directory for temporary files. The environment's extracted Debian toolchain was provided by the sibling benchmark bootstrap, so those binaries are not committed here.

The process needs enough free space for an image and restored corpus copies. If restored ownership cannot be applied as an ordinary user, use a disposable privileged environment; never replace the regular-file target with a host disk merely to run the benchmark.

## Opt-in installer patch

`experimental-fast-uuid.patch` applies to `omacom/omarchy-iso` at upstream PR #145 commit `dbffaa6c65344d644627a023c28661e08382b8fa`:

```bash
git apply --check /path/to/experimental-fast-uuid.patch
git apply /path/to/experimental-fast-uuid.patch
```

The default remains `btrfstune -f -u`. Set exactly `OMARCHY_EXPERIMENTAL_FAST_UUID=1` in the live installer's inherited environment to exercise `-m`; other values preserve the default. This switch does not change the package manifest, source-image verification, target capacity check, LUKS parameters, or boot validation.

Focused validation of the patched installer: `python3 -m unittest test.unit.test_root_image.InstallRootImageTest` passed all 11 tests, including explicit opt-in and non-opt-in cases. Running the entire `test_root_image` module produced one failure in the unchanged hasher-progress test (`test_mirrors_hasher_read_position`, observed `[]`, expected `[0.25]`). No cause or pass is claimed for that unrelated test.

Before considering a default change, run complete encrypted and unencrypted installations using the exact final ISO, mount two independently installed clones simultaneously, verify distinct mount-by-UUID behavior, boot their configured kernels/UKIs, run Btrfs scrub and package/content comparisons, and exercise Snapper rollback. Check any Limine path that reads Btrfs directly; modern kernel support alone does not prove bootloader support. Preserve the default until those gates pass.

## Further independent audit findings

The same PR's `configs/airootfs/root/.automated_script.sh` still warms `omarchy-root.btrfs.zst`, although the payload is now `omarchy-root.btrfs.qcow2`. It consequently skips the image and warms large hardware packages after verification. That can evict verified image pages and compete with restore for USB bandwidth; the effect has not been timed here. Fixing the stale name alone is not enough because qcow2 restoration no longer follows a sequential Btrfs send stream. Compare no prefetch with a bounded, hardware-specific delta prefetch and stop/reap background warmers at the handoff to installation.

Time `qemu-img convert`, UUID replacement, mount/resize, subvolume setup, and machine-identity initialization separately: upstream currently combines them under `unpack root image`, which can conceal where a further improvement helps. Keep the source checksum and structural qcow2 validation ahead of disk modification. In particular, do not use `--target-is-zero` on an arbitrary reused device merely because the filesystem is new; a freshly formatted or newly encrypted mapping does not establish that every unread output byte is zero.
