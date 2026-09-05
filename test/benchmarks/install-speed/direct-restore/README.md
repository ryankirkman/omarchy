# Real block-device direct restore check

This is a bounded correctness test for the opt-in `-t none` change. It runs the
actual, AST-extracted `_restore_root_image` from the reviewed prepared source
(`8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458`) with
real `os` and `subprocess` modules. It does not invoke `_install_root_image`,
install a filesystem, or measure an installation speedup.

`block-test.py prepare` makes a 64 MiB logical zstd-compressed QCOW2 source with
nonzero data, ordinary allocated zero clusters, sparse holes, and nonzero data
at 512-byte boundaries. Host `qemu-img check`, allocation-map assertions, and a
full raw/QCOW2 comparison verify the source. Its bytes have an independent
Python generator in `guest-test.py`.

Four fresh 96 MiB virtio block devices cover the following matrix:

| Logical and physical sectors | Device zero/discard support |
| --- | --- |
| 512 bytes | Enabled |
| 512 bytes | Disabled |
| 4096 bytes | Enabled |
| 4096 bytes | Disabled |

The guest checks actual kernel queue capabilities, serials, size, sector size,
writability, and absence of partitions, mounts, holders, slaves, or swap before
any write. Each target is filled with `0xa7`, flushed, and fully read back. The
real restore then runs once; the test flushes and compares every source byte
and every untouched trailing byte. A final read-only destination must produce
the helper's real `RuntimeError` with one conversion attempt and unchanged
device bytes. No error is repaired or retried. An observation-only Python audit
hook records the actual conversion command, including `-t none` and the absence
of `--target-is-zero`.

All generated images, keys, NVRAM and output belong below `/tmp`. The added
source/expected/target images total 484,442,112 logical bytes. The existing
`iso-vm.py` builder supervisor also creates its usual unused 40 GiB sparse target
(about 200 KiB allocated); this test never writes that device. Existing official
ISO, builder initramfs, kernel, and fixed supplemental ISO are reused read-only.
The portable QEMU 8.2.2 bundle is verified before execution. Its binary SHA-256
must be `634320b91165669917123e8e79cce1c4d00cee0a4aa4d662d7c0a8186479b3fb`.

Prepare with `block-test.py prepare --directory FRESH_TMP_DIRECTORY --toolchain
TOOLCHAIN --upstream PINNED_PR145_CHECKOUT`. Run `block-test.py run --help` for
the required existing boot inputs. It records the exact launch and delegates to
the unchanged ISO supervisor. Keep that process alive in one durable execution
session; separate tools need its filesystem SSH/QMP mailbox. In another session,
run `exercise.py --directory FRESH_TMP_DIRECTORY`. It waits for builder SSH,
stages the exact guest script and source, verifies and extracts the portable
tool, runs a transient guest unit, and collects small evidence. It leaves the VM
available for review; power it off through the supervisor's SSH mailbox after
collecting diagnostics. Then run `verify-stopped.py --directory
FRESH_TMP_DIRECTORY` to independently read and hash the complete host target
files after the supervisor has observed QEMU's clean exit.

Limits: the source is a read-only block device containing the QCOW2 file bytes,
not a regular ISO file. The targets are real guest block devices backed by
disposable host raw files. This covers QEMU/Linux conversion semantics for the
recorded sector/offload matrix; it makes no claim about physical hardware,
power-loss durability, a mounted filesystem, Btrfs finalization, bootability, or
native installation performance. The ordinary full-install benchmark retains
those separate acceptance gates.

The [recorded local TCG test](results/local-tcg-2026-09-05/acceptance.json) passed
all four cases and the real read-only failure case. Both guest verification and
complete host-file readback after clean poweroff agreed. The kernel reported
zero/discard maxima of 2,147,483,136 bytes when enabled and zero when disabled.
Two setup errors stopped at SHA guards before any tool execution or target
write: reversed CD enumeration and an incorrect tar prefix depth. Their original
requests, failures and corrections remain in the sealed evidence. The actual
restore helper and guest byte-check script were unchanged throughout the run.
