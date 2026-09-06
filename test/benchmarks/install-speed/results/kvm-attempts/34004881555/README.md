# Overlap preparation stops at a host contract guard

Run [34004881555](https://github.com/ryankirkman/omarchy/actions/runs/34004881555) on commit `260cff8ab46303abbf56691944a8a8adceaac4ac` stopped during candidate preparation, before any paired installation. The host animation-overlap contract asserted that `/dev/ttyS0` must be absent or not a character device, to prevent benchmark markers from writing to a host serial port. The GitHub runner had such a device, so the guard failed at `animation-overlap/contract-test.py:396`. The original traceback is retained in `animation-overlap-contract.log`.

This is a host contract portability failure. **No pairs or candidate speed result were produced.** The successful stock calibration required no failure-console capture or disk rescue. No failed installation sample was retried or promoted.

The stock calibration passes the current strict raw-calibration reader: host readiness interval **200.622–204.986 s**, guest installer **143.095 s**, 941 package versions, 158 explicit packages and 941 complete zero-missing package-file count rows. Installed-root validation, verified-cold source pages, uninterrupted installation, clean QEMU exit and the independent media-free reboot all pass. This calibration is separate from paired comparison samples.

Image manufacturing completed in **522.056 s**, outside install timing, before the host contract failed. Its original build, package, checksum and media-verification records remain in this artifact. The guard failure supplies no evidence against the overlap runtime behavior and does not establish a performance improvement.

All 76 original small artifact files (584,632 bytes) are preserved unchanged. `artifact-seal.json` records each original size and SHA256; `attempt.json` records workflow/commit provenance, the independently verified ZIP digest and raw calibration validation. The current full-series speed result remains the separate direct-restoration trial's 1.89833x conservative lower bound.
