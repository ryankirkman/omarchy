# Native candidate failure after its first installed boot

Run [33994167582](https://github.com/ryankirkman/omarchy/actions/runs/33994167582)
on commit `c32d0873de0f2bc37ae3545f14f8e79846466db9` completed calibration,
manufactured and verified the root image, and validated its first control.
The first fast-reboot candidate passed first-boot package checks, then failed
its independent media-free reboot. The trial stopped at that failure; the
remaining pairs and early-verification candidate were not run.

All 130 original small artifact files are preserved unchanged. `attempt.json`
records the independently checked ZIP digest, source IDs and reviewed findings.
The image manufacturing step took 661.063 seconds outside installation timing.

## Valid control evidence

The current strict comparator independently accepts the original calibration
and first control. Each has 941 packages, 158 explicit packages, 941 complete
package file-count rows and a successful standalone reboot. The control's
30 original seal-file hashes were rechecked after download.

| Sample | Host boot-to-installed-SSH interval | Guest installer duration |
| --- | ---: | ---: |
| Calibration | 215.006–219.443 s | 163.487 s |
| Paired-series control | 217.180–221.708 s | 167.536 s |

The calibration lacks supplementary image media and therefore does not enter
the paired comparison. The control and candidate both have cold-source proofs
with zero resident source pages immediately before QEMU starts.

## Failed candidate, excluded from comparisons

The candidate reached the installed Btrfs root over SSH at host 112.784 seconds.
Its recorded guest installer duration was 64.358 seconds. All 941 package
versions, all 158 explicit package names and all 941 package file-count rows
match the control; `pacman -Qk` exited zero. These observations are retained to
diagnose the unfinished validation, **not as an accepted install timing**.

For the separate standalone gate, both CD media were removed, CIDATA USB
unplug completed, and the guest accepted `systemctl reboot`. The serial log
shows normal shutdown through filesystem unmounts, the kernel restarting, and
OVMF loading the installed Limine entry again. SSH disconnected and did not
become ready within the 600-second standalone deadline. The serial ends in
terminal-size queries without the next boot's network or service logs.

The run is explicitly `standalone-reboot-failed`; the strict comparator rejects
it. `failure-record.json` preserves it separately from valid sealed runs. The
initial-readiness probe and screen are stale for this later boot, and this
revision did not collect standalone timeout diagnostics or rescue paired
failures. The subsequent forensic-capture change addresses that evidence gap.

**There is no valid candidate comparison or verified 2x result from this run.**
No failed sample was retried or promoted into the accepted series.
