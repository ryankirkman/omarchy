# Foreground animation overlap experiment

This opt-in component patches only [the PR145 dashboard](https://github.com/omacom-io/omarchy-iso/blob/dbffaa6c65344d644627a023c28661e08382b8fa/configs/airootfs/usr/local/bin/omarchy-install-dashboard), whose exact source SHA-256 is `4871faded220498542e1d01a0cbae3f98c21ea5b4eea6bab94fa9e62b415ad89`. The upstream authors own the original dashboard, full laseretch effect and release protocol. No full-install benefit is established for this experiment.

The dashboard already launches the installer as a child. When its existing polling loop first observes `Finalizing boot and user setup`, this patch renders the entire original foreground effect while that child keeps working. The temporary frame says “Finalizing Omarchy” and “Keep the install medium connected”; it makes no success or media-removal promise. The effect command, frame rate, canvas and eight-second stuck-process limit are unchanged. The early frame restores the ordinary progress layout and hidden cursor afterward. Deferred provisioning retains its existing no-celebration path.

A global flag is set only when the actual effect is attempted. The ordinary successful completion path still waits for the child, checks its status, releases the target in the foreground, warms the reboot binaries, draws the final static completion frame, retains the existing unattended one-second static frame and then follows the existing reboot gate. It runs the original post-release effect if the finalization phase was never observed. Effect failure is logged and does not change the installer's status. There is no new background worker, session, process group or target write.

`OMARCHY_BENCHMARK_ANIMATION` begin/end lines record the live boot ID and `/proc/uptime` in the dashboard log and serial console. Begin identifies neutral or completion mode; end records the effect's exit status. These candidate-only observations stay inside the complete host installation clock. The eight-second timeout is not an observed effect duration or a claimed saving.

`prepare-payload.py` accepts a complete previously verified payload and its recorded preflight, preserving every inherited byte and mode. Its own preflight first runs that inherited activation exactly once, then verifies the original pinned live dashboard before replacing it. It never patches installer phases or rebuilds the supplemental image. The root driver can compose the existing direct-restore payload, a separate finalization payload, and this component without changing existing default variants.

```bash
python3 test/benchmarks/install-speed/animation-overlap/prepare-payload.py \
  --iso-source /tmp/omarchy-iso-fast \
  --base-payload /tmp/localdb-overlap-payload \
  --base-preflight test/benchmarks/install-speed/localdb-overlap/preflight.sh \
  --output /tmp/animation-overlap-payload
```

Ten focused dashboard behavior contracts and four payload contracts passed. They cover the complete unchanged effect command, once-only execution, progress restoration, a child failing during the effect, missing or late artwork, deferred provisioning, the phase-unseen fallback, effect errors and the inherited release/reboot gates. Run them with `python3 test/benchmarks/install-speed/animation-overlap/contract-test.py --iso-source /tmp/omarchy-iso-fast`.

The [sealed actual-console proof](results/local-tcg-visual-01/assessment.json) retains the original and patched dashboards, all four scenario logs, 83 QMP frames and four short recordings. In the same 160-column by 50-row live TCG guest, the original success screen, candidate finalization/progress/success transitions, candidate child failure and phase-unseen fallback passed visual review. The real `ttfx 0.3.2` candidate effects returned status zero and took 4.39–4.46 seconds by same-boot uptime markers; none hit the existing eight-second cap. The success effect completed entirely inside the scripted child window. The child-failure case returned status 17 without calling target release or showing a completion/removal promise.

This fixture uses a harmless child that writes only temporary state, a recorded release stub and disabled automatic reboot. The fresh 40 GiB placeholder disk remained entirely unallocated guest data, and the VM powered off cleanly. The QMP recordings sample approximately one frame per second and establish appearance and transitions, not animation smoothness at the unchanged 260-fps effect setting. Any speed claim still requires fresh matched native installs with the existing package, identity, independent reboot and failure gates; these scripted fixture durations are not installation measurements.
