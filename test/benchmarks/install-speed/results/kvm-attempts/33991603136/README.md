# Native calibration failure: installed SSH unavailable

Run [33991603136](https://github.com/ryankirkman/omarchy/actions/runs/33991603136)
failed on commit `d931066f6e9e79983acf49bdf3dcc9cd219e721e` before image
manufacture or candidate trials. This directory preserves all 33 original small
artifact files unchanged. `attempt.json` records the independently verified ZIP
checksum and artifact provenance.

The cold-source proof reports zero resident source pages immediately before
QEMU started. The original installer ran, wrote the installed disk, and rebooted
through OVMF into the installed Limine entry. At the 1800-second deadline, SSH
had not become reachable: the last probe exited 255 with `Connection timed out
during banner exchange`.

After marking the measurement failed, the harness captured the original screen,
pressed Escape, then switched to tty2. `calibration/timeout-after-tty2.png` shows
an installed `Omarchy 7.1.9-arch1-2 (tty2)` login prompt for
`omarchy-benchmark`. The guest therefore reached a usable console; the retained
evidence does not identify the cause of absent SSH. The encryption/resume
warnings also occur on successful calibration boots and do not establish a
boot failure here. No network state or service journal was recovered.

The separate read-only rescue guest reached SSH, but its collector failed to
mount `/dev/vdb2` as read-only Btrfs (exit 32). The exception omitted the mount
command stderr, so its cause remains unknown. No write or filesystem repair was
attempted. The rescue records are retained under `calibration-rescue/`.

**This is not a valid install timing or speed comparison.** No packages, complete
phase trace, first-boot validation, standalone boot validation, or candidate
measurements were collected from this attempt. No 2x claim follows from it.
