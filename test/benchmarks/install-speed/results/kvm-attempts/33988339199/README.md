# Native KVM calibration followed by a failed image build

[Actions run 33988339199](https://github.com/ryankirkman/omarchy/actions/runs/33988339199) used commit `94977d6352f24591fecad0cbffd63c090b076b33` on September 5, 2026. The stock installation completed, booted its fresh Btrfs target with all installation media removed, passed `pacman -Qk`, and powered off cleanly. All 14 phases completed. The preserved inventory has 941 packages and 158 explicit packages; the current strict comparison parser independently accepted all 941 complete package-file count rows.

| Calibration measurement | Seconds |
| --- | ---: |
| Host VM start to installed SSH, lower observation bound | 176.622550 |
| Host VM start to installed SSH, upper observation bound | 181.046758 |
| Guest installer total | 129.390998 |
| Installing Arch + Omarchy | 111.729034 |
| Finalizing Limine boot | 9.360530 |
| Finalizing user | 3.964475 |
| Configuring system | 3.203406 |

This is one calibration using KVM, 4 vCPUs, 8192 MiB RAM, an unencrypted 40 GiB fresh target, direct live-kernel boot followed by installed firmware boot, and SHA-preconditioned source pages. It is not a repeated comparison and does not establish any speedup. Do not compare it to the slower TCG calibration to claim an optimization or mix it with later cold-cache/firmware fixtures.

The native image builder installed the 941-package set and normalized package reasons, then its unit exited with status 1 at approximately 766 seconds. The original driver saved the unit status and outer log but copied redirected package validation diagnostics only after a successful build. Therefore the precise failed native paths were not retained. The separate local builder found cleanup deleting package-owned paths; commit `bd08eb3` fixes that independently reproduced failure. This evidence is consistent with the same failure, without proving it. No image compression or paired trials ran.

Original calibration files are preserved under `calibration/`. The original artifact ZIP and full guest-build log digests are recorded in `attempt.json`; only the final package-reason lines are duplicated here. The revised driver retains an explicit whitelist of small partial output files on failed builds and continues to reject a failed unit even if a completion marker is present.
