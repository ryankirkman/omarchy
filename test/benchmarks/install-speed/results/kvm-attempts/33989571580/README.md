# Verified cold firmware control and image build; candidate failed

[Actions run 33989571580](https://github.com/ryankirkman/omarchy/actions/runs/33989571580) used commit `65d0548fdfcaca2577f68393391ab2f01f0d7a4f` on September 5, 2026. The native image build passed, as did its stock calibration and the first stock sample in the planned paired series. The first candidate runner exited 1, so the series stopped without a valid comparison. No 2× result is claimed.

| Valid sample | Host readiness lower bound | Host readiness upper bound | Guest installer |
| --- | ---: | ---: | ---: |
| Calibration, no supplementary media | 194.206288 s | 198.630617 s | 137.201144 s |
| First control, matched supplementary media | 186.644281 s | 189.075120 s | 131.542083 s |

Both samples used native KVM with 4 vCPUs, 8192 MiB RAM, normal firmware boot, and fresh unencrypted 40 GiB Btrfs targets. The host verified zero resident source pages before timing: the control ISO for calibration, and both control and supplementary image ISOs for the paired-series control. Both samples passed all 14 installer phases, all 941 package-file count rows, and an independent reboot with installation media removed. The current strict comparator accepted both original evidence sets, and every hash in the first control's original seal was rechecked after download. The calibration and paired control have different media topology and must not be treated as interchangeable samples.

The paired control spent 113.812746 seconds installing Arch + Omarchy, 9.3973 seconds finalizing Limine, 4.0413 seconds finalizing the user, and 3.1472 seconds configuring the system. Complete phase timings are retained. Its single sample does not establish a distribution or speedup.

The source image was built in 787.080051 seconds, outside per-install timing. Its 941-package set has no package delta from calibration. Package-file checks and full Btrfs data checksums passed; native compression passed `qemu-img check`, a whole logical comparison to the raw source, and SHA256 verification. The compressed file is 3,669,433,344 bytes with a logical size of 6,174,015,488 bytes. Its exact embedded ISO extent was hashed and verified again. `root-image.json`, `image-media-verification.json`, and `image-build/` retain the original validation records.

The candidate's last public progress sample was at 94.628984 seconds with a running VM and approximately 4.82 GB of allocated target storage. The runner later exited 1. These progress observations are not a completed-install time. The older repetition driver retained failed-run details only in the ephemeral VM work directory, so the artifact has no candidate package checks, phase timings, or precise exception. The separate local test reproduced newly mounted log subvolumes hiding package-owned paths, but this native artifact does not prove that was its failure. Commit `58638e6` adds bounded export of invalid failed-run evidence without sealing it as valid or reclaiming its disk.

`calibration/` and `control-pair01/` contain original captured files. The original control seal still records its original runner paths, while its file hashes remain valid here. `candidate-progress.log` extracts the candidate's public progress lines without inventing missing diagnostics. Artifact and full build-log digests are recorded in `attempt.json`.
