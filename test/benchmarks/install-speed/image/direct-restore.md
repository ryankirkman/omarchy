# Optional direct I/O for image restoration

This experimental candidate changes only the destination cache option of the image restore to `-t none`. Its [first complete native comparison](../results/kvm-attempts/34001279394/) passed all three installation pairs but missed the full-clock 2× target: 1.898331× conservatively and 1.968526× at the observed median. The bundle option does not change any existing default bundle.

The implementation builds on [gosuwachu's Omarchy ISO PR145](https://github.com/omacom/omarchy-iso/pull/145),
pinned to commit `dbffaa6c65344d644627a023c28661e08382b8fa`, with the existing
child-subvolume content fix applied first. Upstream code retains its attribution
and the license already included in the installer bundle.

```sh
python test/benchmarks/install-speed/image/prepare-bundles.py \
  /path/to/pinned/omarchy-iso /tmp/direct-restore-bundles --direct-restore
```

Use a fresh output directory. The optional installer overlay has a distinct
archive checksum and `direct_restore` provenance recording `target_cache: none`
and the input source checksum. The builder bundle is unchanged. Without the flag,
all six output files retain their original bytes.

## Source and behavior contract

| Stage | SHA-256 of `phases_impl.py` |
| --- | --- |
| Pinned upstream | `4088b7e930d2da7729f69c4506483d8e9c661a0488de913255c868f1154de977` |
| Existing child-mount fix, before this option | `8c802ec9ad8b94478ad16d4ca434fa6197741b4d1b3195b0a78d0c876b8682bf` |

`direct_restore.patch_source` accepts only the second source and requires exactly
one original restore-command anchor. Its sole source change adds `"-t", "none"`
to `_restore_root_image`'s `qemu-img convert` argument list. Changed source, a
missing child-mount fix, or repeated application fails preparation.

The full image checksum and `qemu-img check` still run before disk preparation.
The restore retains `-n`, ordinary handling of zero ranges, out-of-order writes,
and the existing worker count. It does not assert that the old device contains
zeros. Source caching, UUID regeneration, resizing, child-mount preservation,
package validation, release sync/unmount gates and installed reboot checks are
unchanged. A conversion failure still aborts the install; there is no application
retry that silently changes the selected cache mode.

## Why this is worth testing

[QEMU 8.2.2's converter](https://github.com/qemu/qemu/blob/v8.2.2/qemu-img.c#L2238-L2244)
defaults its output cache to `unsafe`. The Linux raw block-device backend
[disables efficient zero writes for buffered output](https://github.com/qemu/qemu/blob/v8.2.2/block/file-posix.c#L792-L800).
Direct destination I/O enables that path and avoids copying restored data into
the guest's block-device page cache. This may reduce zero-buffer traffic and
memory pressure during a multi-gigabyte restore. The native tooling bundles its
Ubuntu QEMU 8.2.2 executable; these code references are to the upstream base
version, so a real test must establish the packaged binary's behavior.

QEMU [aligns requests for the destination](https://github.com/qemu/qemu/blob/v8.2.2/qemu-img.c#L2732-L2746)
and allocates the conversion buffer with the destination's alignment.
[Unsupported zero offload falls back to actual zero writes](https://github.com/qemu/qemu/blob/v8.2.2/block/io.c#L1922-L1955),
preserving data on previously used targets. `none` enables direct I/O without
the `unsafe` mode's [no-flush flag](https://github.com/qemu/qemu/blob/v8.2.2/block.c#L1172-L1193).
QEMU [drains and flushes on close](https://github.com/qemu/qemu/blob/v8.2.2/block.c#L5192-L5202),
but that close path does not propagate the flush result. The existing checked
release synchronization and unmount gates therefore remain necessary.

This can also regress performance: subsequent filesystem reads may lose useful
cache residency, and flush/device behavior varies. Only a complete native cold
install comparison with the existing correctness gates can establish a gain.

## Validation

```sh
python test/benchmarks/install-speed/image/direct-restore-contract-test.py \
  --iso-source /path/to/pinned/omarchy-iso
python test/benchmarks/install-speed/image/root-image-mounts-test.py \
  --iso-source /path/to/pinned/omarchy-iso
```

The direct-restore contracts execute the actual patched restore function through
a real command shim, check successful arguments and nonzero-error propagation,
reject source/anchor drift, verify distinct opt-in provenance, and compare all
six default outputs with pre-change checksums. Every other archive member and
its metadata must remain identical.

Those contracts do not establish block-device correctness or speed. The [separate real block-device fixture](../direct-restore/results/local-tcg-2026-09-05/acceptance.json) has now passed all four combinations of 512-byte and 4 KiB logical sectors with zero offload enabled or disabled. Each case restored nonzero data, allocated zeros and sparse ranges onto a nonzero-prefilled device, verified the complete restored contents, and checked that the trailing 32 MiB remained unchanged. An actual read-only-device failure propagated without an application retry or fallback. After clean guest shutdown, independent host readback verified all four complete 96 MiB targets.

These block-device cases are functional results from QEMU 8.2.2 under software emulation, not native install-speed or physical-drive measurements. The later native trial passed full-install package/Qk, unique identity and standalone reboot validation in all six samples. Its candidate readiness bounds were 89.458–98.875 seconds across three fresh installs, versus 187.697–209.994 seconds for controls. The complete comparison remains below the strict full-clock 2× target; its guest-installer median ratio of 3.260017× is a separate metric.
