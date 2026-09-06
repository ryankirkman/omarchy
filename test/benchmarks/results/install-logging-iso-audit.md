# Logging clock candidate for the exact 4.0.2 ISO

This read-only audit, completed on 2026-09-06, identifies a small future candidate. The next combined locate-index/animation-overlap trial deliberately retains the original logging helper. No native installation saving has been measured for this clock change.

The official `omarchy-4.0.2.iso` has SHA256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Source inspection used its cached `var/cache/omarchy/mirror/offline/omarchy-4.0.2-1-any.pkg.tar.zst`, with package files checked against the original MTREE metadata. All 78 packaged install shell files were inspected; differing checkout files were read from the original package rather than substituted.

| Source | SHA256 |
| --- | --- |
| Original `/usr/share/omarchy/install/helpers/logging.sh` | `61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27` |
| Existing optimized `install/helpers/logging.sh` | `1d8151adb150bc1dfe930b30e7039978591add500a132b9951152d7a8a23d715` |
| Original `install/hardware/all.sh` | `3e1ebc051ae142af13e3b3eabf132fffda5bae41bc9948702ba3ca7d326f6251` |
| Original `/usr/bin/omarchy-provision-user` | `03c70bf7378b6aff47fe10254fa6d12a2a059837df3e1dfda8a35dd168076cf5` |
| Original ISO orchestrator `phases_impl.py` | `e75c17efbaba464f976ef88719a8ca726f7fedbda5c6260b813eb97eac81713e` |

The original helper is byte-identical to the logging benchmark baseline at commit `e8e92c5092c9bbbf3d7fc5240f8551fd1eeaced9`. The minimal code change is the existing optimized helper diff: format timestamps with Bash `printf -v ... '%(%Y-%m-%d %H:%M:%S)T' -1` and read `EPOCHSECONDS`, preserving inherited start fields. The installed package inventory records Bash 5.3.15-1. Child-shell isolation, debug mode, stdout/file routing, log text, failure return codes and errexit restoration remain unchanged; existing logging contracts cover those paths.

## Exact successful setup call count

| Stage | `run_logged` calls |
| --- | ---: |
| Config | 12 |
| Hardware | 35 |
| Login | 1 |
| Post-install | 3 |
| User | 12 |
| Total | **63** |

The system entry point dispatches 51 calls and invokes `start_install_log` and `stop_install_log` once each. User finalization dispatches another 12 calls without log lifecycle calls. There are no nested `run_logged` calls inside the leaves. Hardware conditionals occur inside their wrappers. Deferred provisioning omits user finalization, so the table describes successful normal provisioning.

The original ISO orchestrator supplies nonempty `OMARCHY_START_TIME`, `OMARCHY_START_EPOCH` and `OMARCHY_LOG_TO_STDOUT=1` at source lines 1091–1101. Therefore the original helper invokes external `date` **128 times**: 126 leaf timestamps and two stop timestamps; start invokes none. The optimization removes those processes while retaining 63 Starting records, 63 Completed records, setup start/completion records and the duration summary. Locate-index overlap relocates one wrapper call without changing this total.

## Measured scope and integration

The existing [local microbenchmark](install-logging.json) measured 80 trivial leaves on Bash 5.2.21: median 934.530 ms before and 429.586 ms after, a 504.944 ms reduction. Normalized logs matched. Alternating raw samples retain scheduling outliers. This is local logging overhead evidence, not native install timing; it does not establish a multi-second gain or close the current full-install target gap.

The locate-index component verifies the original logging SHA256 in `_localdb_overlap_sources` before system setup and again before late indexing. Replacing that helper requires an explicit new combined source pin and corresponding validation; otherwise the existing guard correctly rejects the changed helper. This audit changes neither helper nor guard and introduces no test run or workflow selection.
