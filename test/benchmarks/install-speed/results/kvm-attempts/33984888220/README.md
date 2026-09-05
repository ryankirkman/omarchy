# Unvalidated first KVM calibration attempt

This failed attempt is excluded from all speed comparisons. It did not verify an installed-system boot or collect installed package manifests. The roughly 161 seconds before the supervisor error must not be described as a completed installation time.

[GitHub Actions run 33984888220](https://github.com/ryankirkman/omarchy/actions/runs/33984888220) used experiment commit `eeca85f55070caf5558142162ba224232adc3d2e`. Its standard public Ubuntu runner passed actual `KVM_CREATE_VM`, dependency installation and free-space gates. The verified official 4.0.2 ISO downloaded in approximately 16 seconds. The host reported 104 GiB free after setup and approximately 98 GB free at the last progress sample, ruling out ENOSPC as this failure's cause.

The live guest wrote approximately 6.15 GB to the fresh target. During its first reboot, the QMP socket closed before the supervisor observed QEMU's exit. `iso-vm.py` raised `RuntimeError: QMP disconnected` instead of taking its expected direct-kernel-to-installed-disk restart path. The final screenshot shows the guest shutdown console. This supports an EOF/process-exit race; no installed-disk success may be inferred from it.

The retained artifact is [9974908267](https://github.com/ryankirkman/omarchy/actions/runs/33984888220/artifacts/9974908267), SHA256 `5f16adb86d30c153029c4a21d507fe2062419728ee58277ae169822ab0af6545`. Its bytes were downloaded and checked before these small original evidence files were copied here. The artifact expires after seven days. The full host package inventory and job log were omitted from this compact record; private keys, cidata, VM disks and temporary download URLs are not included.

An earlier [workflow validation failure, run 33984691702](https://github.com/ryankirkman/omarchy/actions/runs/33984691702), started zero jobs because `runner.temp` was referenced in job-level `env`, where that context is unavailable. Moving path initialization into a runner step resolved it; that run contains no performance measurement either.
