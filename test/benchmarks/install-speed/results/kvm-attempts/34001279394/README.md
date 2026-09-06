# Three valid native pairs miss the conservative 2x target

Run [34001279394](https://github.com/ryankirkman/omarchy/actions/runs/34001279394) on commit `21fa88e2cc72ed2c89bb95e01ceba5c0d5699969` completed all three fresh pairs for the image, no-prefetch, fast-reboot, early-verification and direct-restoration candidate. Every installation and independent media-free reboot passed. The workflow failed because the conservative full-install speedup was below 2x; no installation failed, no sample was retried, and no failure-console or rescue intervention was invoked.

| Pair | Control boot-to-installed-SSH interval | Candidate boot-to-installed-SSH interval |
| --- | ---: | ---: |
| 1 | 205.577–209.994 s | 94.051–98.606 s |
| 2 | 189.680–194.108 s | 94.436–98.875 s |
| 3 | 187.697–192.121 s | 89.458–94.005 s |

Median observed speedup is **1.96853x** (194.108 s / 98.606 s). Including actual polling uncertainty, the conservative lower bound is **1.89833x** (fastest control lower bound / slowest candidate upper bound). The three-samples-per-revision gate passes, but `twofold_target_verified_for_this_fixture` remains **false**. For this series's fastest control bound, the slowest candidate would need to finish by 93.849 s, approximately 5.027 s earlier, to meet the conservative threshold.

The current strict comparator independently accepts all six sealed originals and reproduces the recorded host timing, identity, sample-count and target verdicts. All 180 original sealed file hashes were rechecked. Each installation has 941 identical package versions, 158 explicit packages and 941 complete zero-missing package-file count rows. Fresh target/NVRAM, verified-cold source pages, uninterrupted measurement, clean QEMU exit and independent media-free reboot pass throughout. Machine, SSH, pacman signing and Btrfs identities are distinct across all six installations. The separate raw calibration also passes its validation gates and does not enter the comparison.

Candidate guest installer durations were 40.087–41.530 s: image restoration took 21.788–22.023 s, system configuration 7.228–7.608 s, and final boot/user setup 9.701–10.977 s. Control guest durations were 131.906–145.913 s. These phase durations exclude live boot and installed boot; controls report guest wall time and candidates report guest monotonic time. The full-install verdict uses host monotonic intervals only. Image manufacturing took 782.082 s before install timing under the documented release-artifact model.

All original small artifact files and per-sample seals are retained unchanged. `artifact-seal.json` inventories their exact sizes and SHA256 hashes; `attempt.json` records workflow/commit provenance, the independently verified ZIP digest and the reproduced validation results. This complete series does not reuse the single 2.19046x pair from an earlier trial.
