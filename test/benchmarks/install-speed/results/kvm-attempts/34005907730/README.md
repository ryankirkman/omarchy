# Three valid overlap pairs narrowly miss the conservative 2x bound

Run [34005907730](https://github.com/ryankirkman/omarchy/actions/runs/34005907730) on commit `dac5b17d12d0fac41052e1695620e3b3784a5063` completed all three fresh pairs for the image, no-prefetch, fast-reboot, early-verification, direct-restoration and finalization-overlap candidate. Every installation and independent media-free reboot passed. The workflow returned the performance-target miss status; there were no failed installations, retries or post-failure diagnostic interventions.

| Pair | Control boot-to-installed-SSH interval | Candidate boot-to-installed-SSH interval |
| --- | ---: | ---: |
| 1 | 161.862–166.325 s | 71.053–75.465 s |
| 2 | 151.205–155.584 s | 70.453–74.847 s |
| 3 | 150.897–155.341 s | 70.543–74.897 s |

Median observed speedup is **2.077305366x**. The conservative lower bound is **1.999569537x**, calculated from the fastest control lower bound (150.896607851 s) divided by the slowest candidate upper bound (75.464546259 s). The three-fresh-samples gate passes, but `twofold_target_verified_for_this_fixture` remains **false**. The candidate upper bound exceeds half the fastest control lower bound by 0.0162423335 s. This narrow miss is retained without rounding it into a passing result or substituting samples.

The current strict comparator independently accepts all six originals and reproduces host timing, identity, sample-count and target verdicts exactly. All 180 original sealed file hashes were checked. Each installation has 941 matching package versions, 158 explicit packages and 941 complete zero-missing package-file count rows. Fresh disk/NVRAM, source pages verified cold, uninterrupted installation, installed-root validation, clean QEMU exit, unique machine/SSH/pacman/Btrfs identities and standalone reboot pass throughout. The separate raw calibration also passes and does not enter this comparison.

## Finalization profile

| Candidate | Boot branch | Ordered user substeps, including index | Index substep | Animation guest uptime |
| --- | ---: | ---: | ---: | --- |
| 02 | 9.082 s | 7.849 s | 2.426 s | 40.39–44.64 s |
| 03 | 9.411 s | 8.825 s | 3.340 s | 40.39–44.67 s |
| 06 | 9.220 s | 8.742 s | 3.425 s | 40.07–44.21 s |

The boot branch remains longer than the sum of the ordered user substeps in every candidate. All new `Indexing installed files` substeps succeed. Each animation begins with the `finalizing` marker and ends with exit status zero, using the same live guest boot ID. Animation markers report guest uptime rounded to centiseconds; they are not host timestamps. The table describes this candidate's measured execution and does not estimate a causal saving by subtracting another runner's timings.

Candidate guest installer durations were 30.718–31.203 s; controls were 103.369–115.589 s. Guest phases exclude live and installed boot, and the controls report wall-clock durations while the candidates report monotonic durations. The full-install verdict uses only host monotonic intervals with actual polling uncertainty. Image manufacturing took 619.064 s before install timing under the documented release-artifact model.

All original small artifact files and per-sample seals are preserved unchanged. `artifact-seal.json` inventories original sizes and SHA256 hashes; `attempt.json` contains workflow/commit provenance, the independently verified ZIP digest, reproduced validation, exact readiness brackets and finalization/animation details. Previous trials' samples are not reused.
