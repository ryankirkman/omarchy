# Live console diagnostic fixture

`live-preflight.sh` is a disposable live-ISO fixture for exercising the actual
post-failure console path in `../../postfailure-console.py`. It rejects a host or
installed root, sets the benchmark hostname and fixture root password, uses a
plain Bash console prompt, starts `serial-getty@ttyS0`, and runtime-masks SSH.
The builder initramfs wrapper preserves the original automatic installer but
does not execute it. No SSH key is included in the initramfs payload.

Build the additional initramfs with the existing `boot-overlay/make-initramfs.py`
using `--mode builder --preflight-script` and the exact original initramfs hash.
Freeze the runner and its sibling console helper together before launch, because
the runner imports the helper when the readiness deadline expires. Use a fresh
run directory and ordinary `iso-vm.py run --mode builder --guest-user root`
with the official ISO, matching direct kernel/initramfs, four TCG CPUs, 8192 MiB,
`--timeout 240 --poll-interval 10`, and no authorized SSH key in the guest.
All writable VM state and large media stay under `/tmp`.

The expected runner outcome is an invalid timeout with exit status 1. Functional
acceptance additionally requires a real console password login, nonce-bound UID
0 challenge, the fixed read-only diagnostic command suite, preservation of the
original serial bytes, and a wholly zero unused target disk after QEMU exits.
A successful diagnostic must never turn this deliberate timeout into an accepted
installation sample. This fixture measures neither installation speed nor a
paired speedup.

The first real guest attempt is retained under
`results/local-tcg-2026-09-05-attempt-01`. The live fixture reached its readiness
marker and real serial login prompt, then the automatic helper refused the
original serial backend before sending any input. Its rejected QMP rows were not
recorded by that helper version, so the exact backend mismatch remains unresolved
in this attempt. The runner exited 1 with an invalid timeout, and post-exit
`qemu-img map` confirms the entire unused 40 GiB target is zero with no data
extents. The result is a failed diagnostic test, not a functional pass.

The fresh second attempt, `results/local-tcg-2026-09-06-attempt-02`, passed the
console functional checks with the corrected backend guard at `3a8993d`. It
reused the identical guest preflight and initramfs. The helper authenticated the
real root console, verified the nonce-bound UID 0 response, and retained all ten
fixed diagnostic command results. `critical-chain` returned 1 because systemd
reported bootup was not yet finished; the other nine commands returned 0. The
655-byte original serial log remained unchanged, and the target remained wholly
zero. The runner still exited 1 with `status=timeout` and
`validation_passed=false`. Both attempts preserve their original evidence and
frozen source bytes under individual SHA-256 seals.
