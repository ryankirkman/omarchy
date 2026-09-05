# Optional release-gated dashboard experiment

`image-no-package-prefetch-fast-reboot` adds the dashboard from [Omarchy ISO PR145](https://github.com/omacom-io/omarchy-iso/pull/145), pinned to `dbffaa6c65344d644627a023c28661e08382b8fa`. The existing `upstream-image` and `image-no-package-prefetch` variants retain the release ISO's original dashboard and are unchanged. This option is excluded from the native driver's defaults and does not select or trigger a workflow.

The pinned PR dashboard first syncs the installation target, disables its swap, unmounts its filesystem tree, closes its LUKS mappers if present, and syncs again through `omarchy-release-install-target`. Only a successful release allows its guest `systemctl reboot -ff` path. A failed release retains the normal graceful guest reboot. The full animation and static finish frame remain; no setup or validation moves to a later boot. No host reset or QMP reset is used.

This is an upstream improvement omitted by the initial benchmark overlay, not a new image-restoration optimization. Credit belongs to the pinned Omarchy ISO source, distributed under the MIT license with copyright attributed to Anton Hvornum. The staged payload includes the exact upstream license and source hashes. The dashboard remains byte-identical to that pin. This experiment adds one safety guard to the release helper: both bare `sync` calls become `sync || exit 1`, so either failure prevents immediate reboot. The patch is checked against the exact source hash and expected two call sites; source drift fails preparation.

The independent standalone reboot gate remains mandatory when selecting firmware mode. It asks the installed system for an ordinary reboot with all installation media absent and checks its root, identities and changed boot ID. The image verifier, package inventory, package-file checks and fresh-instance identity requirements remain unchanged. Speed must be measured from initial VM start through the first successful installed SSH observation, including target release, animation and reboot; installer phase timing alone cannot establish this variant's benefit.

## Prepare and measure

The native driver can explicitly select this candidate after the original image comparison:

```bash
python3 test/benchmarks/install-speed/native-ci/run-native-experiment.py \
  --repo "$GITHUB_WORKSPACE" --work "$BENCH_WORK" --evidence "$BENCH_EVIDENCE" \
  --boot-method firmware --source-cache cold \
  --variants image-no-package-prefetch-fast-reboot
```

Each invocation requires a fresh work directory and performs three new alternating control/candidate pairs. It records `fast-reboot.manifest.json`, an initramfs manifest, and `no-prefetch-fast-reboot-repetitions/comparison.json`. The candidate uses the same validated root image and supplemental media as control. Dashboard preparation is outside timing; activation and all guest work remain inside it. Selecting multiple variants preserves each result separately and consumes an additional derived ISO per firmware candidate, about 6.2 GB each; the repacker's free-space reserve still applies.

`prepare-payload.py` reads the actual Git objects at the pinned commit, so unrelated checkout changes cannot silently alter the selected dashboard. Its preflight checks payload SHA256 sums, activates the original image overlay, and then installs the selected dashboard and guarded helper. That order matters: the ordinary overlay also contains a release helper and would overwrite a guard installed earlier. `cmp` checks both final live scripts before the installer can start.

## Focused verification

```bash
python3 test/benchmarks/install-speed/fast-reboot/contract-test.py \
  --iso-source /tmp/omarchy-iso-fast
```

The contracts execute the actual pinned dashboard decision functions and the guarded release helper against temporary command stubs. They check sync/swapoff/unmount/sync ordering, successful plain and encrypted release, sync failures at both sites, failed unmount, failed or unanswerable mapper close, and immediate/graceful command fallbacks. A separate sandbox activation check proves the guard survives ordinary overlay extraction and rejects a corrupted payload before activation. No disk is mounted or modified, and no real reboot occurs. These are correctness checks, not speed measurements; a complete fresh installation and standalone reboot must still pass before reporting a result.
