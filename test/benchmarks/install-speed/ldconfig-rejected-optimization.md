# Rejected experiment: skipping first-boot linker cache rebuild

Status: source review only, 2026-09-05. No skipped-service benchmark was run, and no native time saving or install speedup is established. No update stamps or systemd units were changed. This note is separate from timing results.

The image candidate reached its installed system's `ldconfig.service` even though its image package transaction and PostTransaction hooks had completed. Reject suppressing that service for the current candidate: the available evidence explains its execution but does not prove all of its first-boot work redundant.

## Exact source fixture

The reviewed files came from the official `omarchy-4.0.2.iso`, SHA-256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Relevant package versions were systemd `261.2-1`, glibc `2.44+r24+g16be1518495f-1`, and pacman `7.1.0.r9.g54d9411-2`. The candidate installer was pinned to omacom/omarchy-iso PR #145 commit `dbffaa6c65344d644627a023c28661e08382b8fa`.

These source file SHA-256 values were checked against the exact packages' MTREE records:

| Path in the installed root | SHA-256 |
| --- | --- |
| `usr/lib/systemd/system/ldconfig.service` | `8f84fde0c695fa061222114f56c651dcba7e2976dc9278f3660d13f90f1f12c4` |
| `usr/share/libalpm/hooks/11-glibc-ldconfig.hook` | `fdf2879654541a1ad006703f4ff5524e0f61b4d2c05e5de93ddd8484c56b15fc` |
| `usr/share/libalpm/hooks/35-systemd-update.hook` | `1090b7b1edba2042298b609a77bbe122982ca936208408fb79d77b33a2f3c27a` |
| `usr/share/libalpm/scripts/systemd-hook` | `90e00dd01359565be85a8cdd93b424b4c1c89563300d16eaf0e7eedc349bf9fc` |

The saved [hook plan](results/image-recovery-4.0.2-2026-09-05/hook-plan.json) and [successful hook replay](results/image-recovery-4.0.2-2026-09-05/hook-replay.log) preserve the image transaction evidence.

## Why the service still runs

The relevant PostTransaction order is `11-glibc-ldconfig.hook`, `20-systemd-sysusers.hook`, `21-systemd-tmpfiles.hook`, `25-systemd-catalog.hook` / `25-systemd-hwdb.hook`, then `35-systemd-update.hook`, with other hooks interleaved. The first executes `ldconfig -r .`; the last invokes `systemd-hook update`, whose implementation executes `touch -c /usr` to arm `ConditionNeedsUpdate`.

The pinned `ldconfig.service` has two OR conditions: `ConditionNeedsUpdate=|/etc` and `ConditionFileNotEmpty=|!/etc/ld.so.cache`. A nonempty cache therefore does not suppress the service while `/etc` still needs updating. Its command is `ldconfig -X`; it orders after `local-fs.target`, `systemd-confext.service`, and `systemd-tmpfiles-setup.service`, and before `sysinit.target` and `systemd-update-done.service`. The unit explicitly accounts for confext or tmpfiles introducing linker configuration.

Image hook completion does not establish equivalence with that boot state. The image tmpfiles hook uses `systemd-tmpfiles --create`; the boot service uses `--create --remove --boot --exclude-prefix=/dev` and can consume boot credentials. Per-machine package changes also remain possible after image restoration.

## Why update stamps are too broad

The pinned `systemd.unit(5)` documents `ConditionNeedsUpdate` for `/etc` or `/var`, comparing `/usr` with the corresponding `.updated` state. Completing `/etc/.updated` changes eligibility for `systemd-sysusers.service` and `systemd-hwdb-update.service` as well as ldconfig; other conditions, such as sysusers credentials, can still require execution. Completing `/var/.updated` changes journal catalog update eligibility. The boot tmpfiles service has no `ConditionNeedsUpdate` gate and continues to run.

The pinned `systemd-update-done.service(8)` documents an offline `systemd-update-done --root=…` interface, supported since systemd 258. It updates both stamps, including their stored timestamp content. That is a supported completion mechanism, but it declares broader post-update work complete. The current evidence does not justify that declaration for every applicable service and subsequent target change. A blanket kernel `systemd.condition_needs_update=` override has the same scope problem; masking ldconfig also removes its missing-cache fallback.

Reconsider only with evidence that accounts for the complete update consumers, boot configuration inputs, and per-machine changes, followed by a fresh full-install validation and measured native comparison. Completed image hooks alone are insufficient grounds to skip the service.
