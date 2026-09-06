# First validated native pair exceeds 2x; repetition fails

Run [33996768275](https://github.com/ryankirkman/omarchy/actions/runs/33996768275) on commit `e6a157e9e229f6079696726d4a61cc253e5533d5` produced one fully validated cold firmware pair for the image, no-prefetch, fast-reboot and early-verification candidate. The second candidate failed to reach its first installed SSH session, so the trial stopped before the remaining pairs or direct-restore variant.

All 164 original small artifact files are preserved unchanged. `attempt.json` records the independently verified ZIP SHA256, provenance and validation facts. The current strict comparator independently accepts the original calibration, control and first candidate, reproduces their comparison, and rejects the failed sample. All 60 original files covered by the two valid sample seals were rehashed.

## Valid first pair

| Measurement | Control | Candidate |
| --- | ---: | ---: |
| Host boot-to-installed-SSH interval | 201.291–205.676 s | 87.507–91.894 s |
| Guest installer duration | 144.481 s | 42.306 s |
| Packages / explicit packages / complete file-count rows | 941 / 158 / 941 | 941 / 158 / 941 |
| Independent media-free reboot | Passed | Passed |

Package versions, installation reasons and package file counts match exactly. Distinct machine, SSH, pacman signing and Btrfs identities are verified. Both source ISOs have zero resident host pages immediately before each VM starts; hardware, source-media topology and boot policy match between the paired samples. The supplementary image was manufactured and verified before install timing.

This pair's observed speedup is 2.23818x. Including measured polling uncertainty, the conservative lower bound is **2.19046x**. This is a valid single pair, not the required repeated result: `at_least_three_fresh_samples_per_revision` and `twofold_target_verified_for_this_fixture` both remain **false**. No failed sample was retried or substituted to complete the series.

## Failed next candidate

`03-candidate-pair02` did not reach initial installed SSH within 600 seconds. Its last probe exited 255 with a banner-exchange timeout. After the measurement was marked failed, tty2 showed the installed Omarchy login prompt. The QMP network table recorded forwarded TCP connections in `SYN_SENT` to `10.0.2.15:22`. Those observations identify absent network reachability while an installed console was present; they alone do not establish a package or boot failure.

The separate rescue guest successfully collected `installed-disk-diagnostics.json` from the failed target through a read-only block device. Original network/service journals and configuration diagnostics are retained in the `image-no-package-prefetch-fast-reboot-early-verify-failed-run-rescue` directory. The failing run remains under `failed-runs/` and outside all accepted comparisons.