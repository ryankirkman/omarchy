# Fresh ISO VM benchmark

`iso-vm.py` runs the official ISO's existing unattended installation flow, then verifies that SSH reached the installed Btrfs root rather than the live ISO. It saves the installer's actual phase timings, the exact installed package/version inventory, explicit-package inventory, boot evidence, `pacman -Qk` output and exit status, screenshots, serial output, and all QEMU arguments. Feed independent completed runs to `compare-installs.py`.

Use an empty run directory under `/tmp` or another unsynced filesystem. A sync service can recursively copy a disk while QEMU writes it; that consumes storage and invalidates measurements. Each sample requires its own fresh disk and firmware variables. Never reuse an installed base as a fresh-install sample.

```bash
python3 test/benchmarks/iso-vm.py run \
  --iso /tmp/omarchy-bench/downloads/omarchy-4.0.2.iso \
  --iso-source ../omarchy-iso \
  --toolchain /tmp/omarchy-bench/toolchain \
  --run-dir /tmp/omarchy-bench/baseline-01 \
  --cpus 4 --memory 8192 --accelerator tcg --keep-running
```

The ISO source checkout supplies the official `build_cidata()` integration-test function. The configuration uses a fresh 40 GiB target, 2 GiB ESP, Btrfs with `compress=zstd`, no encryption, and a disposable `omarchy` account/password with a generated SSH key. These are disposable VM credentials. The image is installed from its own bundled mirror. Default package names are `omarchy` and `omarchy-settings`; overrides are available for development images.

The toolchain prefix may contain extracted Ubuntu packages; no privileged host installation is needed. It expects Ubuntu OVMF firmware filenames under `usr/share/OVMF` and the SeaBIOS virtio VGA ROM under `usr/share/seabios`. It needs `qemu-system-x86_64`, `qemu-img`, OVMF, SeaBIOS, `mkfs.vfat`, `mcopy`, `jq`, Bash, OpenSSH, OpenSSL, and Python 3.11 or later. Pillow converts screenshots to PNG when available. Download packages with `apt-get download`, then extract with `dpkg-deb -x`; in containers without `setgroups`, `apt-get -o APT::Sandbox::User=root download` selects the already-running user. No package manager changes are made to the host.

## Supervision and evidence

The runner supervises QEMU without daemonizing it. All TCP QMP and SSH operations happen inside that same process/network namespace. This matters in environments where separate shell calls cannot connect to each other's local listeners. Use a filesystem mailbox from another process:

```bash
python3 test/benchmarks/iso-vm.py request \
  --run-dir /tmp/omarchy-bench/baseline-01 \
  --json '{"action":"screenshot"}'
```

The command prints the response filename. Read it once the supervisor has processed the request. Other requests include:

```json
{"action":"qmp","execute":"query-status"}
{"action":"qmp","execute":"stop"}
{"action":"qmp","execute":"cont"}
{"action":"ssh","command":"pacman -Q","sudo":false,"timeout":120}
{"action":"qmp","execute":"quit"}
```

Each intervention is recorded. Pauses, resets, and snapshot restores before collection set `measurement_interrupted=true`; such a run must not support a speedup claim. Generic QMP supports additional block devices for follow-up experimentation after benchmark collection. SSH commands should be read-only until collection is complete. Screenshots do not run OCR and have negligible guest impact. The progress JSON records host wall time, target allocation and remaining storage every 30 seconds. An unattended successful install reboots itself; no UI scripting changes the install workflow.

`manifest.json` reaches `installed-and-booted` only after checking `/` is the target disk's Btrfs filesystem and collecting package-file validation. `validation_passed` is separately false if `pacman -Qk` fails; `compare-installs.py` rejects that run. The installed root must have the normal two-partition fixture layout. Results under TCG establish only this emulated fixture's performance, not a physical-machine speedup. Keep hardware, host load, disk/cache settings, package names and versions, ISO cache conditions, and encryption constant. Compare at least three fresh runs per revision before claiming the 2× goal.

With `--keep-running`, the validated guest remains available for subsequent read-only diagnostics or image-building work. Those later changes cannot be counted as part of the baseline timing and must never be used to falsify its collected state. Shut it down explicitly via the mailbox when finished. Retain small evidence files in git; keep VM disks, credentials, firmware variables and ISO files outside git.

## Matched overlay experiments

The runner accepts `--kernel`, `--initrd`, and `--append` for a matched control and candidate using the same official kernel and command line. Both must use this boot strategy: OVMF tries a QEMU-supplied kernel on every boot. In install mode the first QEMU process therefore uses `-no-reboot`; when the installer reboots, the supervisor starts another QEMU process with the same target and NVRAM, removing the direct-kernel inputs. The host monotonic clock continues across that restart, so its overhead is included for both revisions. Kernel/initrd hashes, command line, configuration hash, overlay hash, and both process argument lists are recorded. Any extra device arguments can be passed as a JSON argv array with `--extra-qemu-args-json`.

For accurate readiness uncertainty, the runner records the start of the last failed SSH probe and the end of the first successful probe. The interval between these observations brackets installed SSH readiness; the nominal poll interval alone is not an adequate uncertainty bound. Guest installer timings remain separate from the complete host boot/install/reboot total.

`--mode builder` deliberately does not produce a valid install benchmark. It waits for root SSH by default, records `builder-ssh-ready`, and serves the mailbox indefinitely. `--ssh-key` can copy an existing disposable benchmark private key into that run; `--guest-user` overrides the login name. Use a builder overlay that disables the system installer, and attach its dedicated blank build disk with explicit device identity.

All supplemental read-only `-drive` files supplied in extra QEMU arguments are hashed and recorded with their drive ID, format, cache setting and attached device interface. Install mode rejects writable extra drives. Use the same supplemental media and topology in control and candidate, even when control does not consume the prebuilt root image. The official ISO and supplementary media are fully read for SHA256 verification before the VM starts, providing the same explicit cache-preconditioning procedure in both groups. These measurements are therefore not cold-host-cache claims. Initrd/kernel hashes additionally cover embedded overlay scripts and portable binaries.

## Repeated matched installs

`install-speed/repeat-installs.py` orchestrates at least three fresh installs of each revision. Supply two JSON files containing complete `iso-vm.py run` argv arrays. The driver replaces each template's `--run-dir`, removes `--keep-running`, and uses install mode. With three pairs and the default first revision, run order is control/candidate, candidate/control, control/candidate. Both templates must attach the same read-only supplementary media with matching hardware, cache settings and boot strategy. Prepare and validate the image before launching the series.

```bash
python3 test/benchmarks/install-speed/repeat-installs.py \
  --control-launch /tmp/omarchy-bench/control-launch.json \
  --candidate-launch /tmp/omarchy-bench/candidate-launch.json \
  --run-root /tmp/omarchy-bench/matched-series \
  --vm-state-root /tmp/omarchy-bench \
  --evidence-root test/benchmarks/install-speed/results/matched-series \
  --pairs 3
```

Use `--plan-only` to inspect the order and normalized templates without starting a VM. Keep every other VM under the same `--vm-state-root` and shut it down first: the driver refuses manifests that still report a live builder/install, and an advisory lock prevents two repeat drivers sharing that state root. It runs one VM at a time. These guards do not replace coordination with unrelated VM launchers elsewhere on the host.

After each clean runner/QEMU exit, the real comparator validates installed boot, complete phases, package files, package versions/reasons, identities, and measurement provenance. An explicit whitelist of small evidence files is copied into `evidence-root/runs/<sample>`; `seal.json` records every file's SHA256 and source-code hashes, and copies are verified before the driver unlinks only that sample's `target.qcow2`. No recursive disk cleanup occurs. Failed runs retain their disk for investigation. SSH private keys, CIDATA credentials, ISO images, firmware variables and disk images are excluded from sealed evidence.

`series.json` records the sample order and progress; `comparison.json` contains the latest full comparison. Exit **0** means all samples completed and the conservative full host-clock 2× target passed. Exit **2** means a valid complete comparison below that target; preserve its artifacts and investigate the remaining bottleneck. Other failures return **1**, and interruption returns **130**. CI should always upload the small evidence directory, including when the target is unmet.

`--resume` verifies sealed samples and continues the original plan without rerunning completed installs. Source hashes, templates and ordering must remain identical. Unsealed existing runs are retained rather than overwritten. Interrupt handling requests guest poweroff and then terminates the dedicated child process group if necessary; allow 45 seconds after SIGTERM before forcibly stopping the driver. No interrupted target is reclaimed. Contract verification used synthetic fixtures for alternating order, six-run acceptance, target-unmet exit, resume, tamper rejection, failed-disk retention and existing-VM exclusion; those invented fixture durations are not performance results.
