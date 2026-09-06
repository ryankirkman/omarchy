# Invalid native KVM calibration attempt

[Actions run 33985673711](https://github.com/ryankirkman/omarchy/actions/runs/33985673711) used commit `3a11ab79b84d4f982ca0e54602b83f18e323ab1a` on September 5, 2026. The installer QEMU exited cleanly and the supervisor relaunched the fresh installed disk at 163.30 host seconds. OVMF loaded its Limine entry. SSH never became ready before the 1,800-second whole-run timeout. The image build and measured pairs did not start.

The preserved screenshot and serial log show an unencrypted-volume warning and failed resume attempt, followed by terminal-size queries. Those same warnings also appear in the successful TCG calibration, so they do not establish this failure's cause. No installed package manifest, phase timings, or boot validation was collected. This attempt is excluded from all speed comparisons.

The original artifact ZIP was downloaded and its SHA256 checked against GitHub's artifact digest, recorded in `attempt.json`. These are the original small diagnostics; private SSH keys and disposable disks are excluded.
