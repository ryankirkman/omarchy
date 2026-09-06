# Duplicate mise-wrapper pass: source audit

This read-only audit identifies repeated user setup work, not an implemented optimization or measured native gain. Wait for the current finalization-overlap trial's substep evidence before changing this path: in run `34001279394`, the existing user branch finished before the boot branch, so removing user work could save **no full installation time**. Moving indexing onto the user branch may change that relationship.

The three accepted candidates report these monotonic whole-step durations in `no-prefetch-fast-reboot-early-verify-direct-restore-repetitions/runs/*candidate*/install-timing.json`:

| Sample | Finalizing user | Finalizing Limine boot |
| --- | ---: | ---: |
| `02-candidate-pair01` | 6.372 s | 10.976 s |
| `03-candidate-pair02` | 5.903 s | 10.375 s |
| `06-candidate-pair03` | 5.200 s | 9.699 s |

The ordered login/SSH/DNS tail adds less than 0.4 seconds to each user step. These are not individual script timings. No candidate target installer text logs were found in the retained artifact; the original logger's second-resolution wall-clock stamps would not establish precise leaf timings anyway. No guest wall clock was subtracted from a host clock.

## Exact ISO source

The source is `omarchy 4.0.2-1` in the official Omarchy 4.0.2 ISO, SHA256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Selected members were streamed from `var/cache/omarchy/mirror/offline/omarchy-4.0.2-1-any.pkg.tar.zst` without unpacking the image; each regular file's bytes matched its package MTREE SHA256 record. The installer source pin is [PR145 commit `dbffaa6c65344d644627a023c28661e08382b8fa`](https://github.com/omacom-io/omarchy-iso/tree/dbffaa6c65344d644627a023c28661e08382b8fa).

| Package member | SHA256 |
| --- | --- |
| `usr/bin/omarchy-provision-user` | `03c70bf7378b6aff47fe10254fa6d12a2a059837df3e1dfda8a35dd168076cf5` |
| `usr/share/omarchy/install/user/all.sh` | `32ff1a56bb50ae3ca7a8eeabb24947a6cbdcbf66e112c69894fd6870a0872742` |
| `usr/share/omarchy/install/user/mise.sh` | `a1d38b21d665d88069e88bfd15e9e99287e80e4af31cab11fd9c6cbd1c3db6ff` |
| `usr/bin/omarchy-refresh-applications` | `38828b969c10f48c426754d7b70a3d5fcefe9fb912e7e0b320da180a7d0ed67c` |
| `usr/bin/omarchy-mise-install` | `a99edee09a3f34752a730801061775bbc5688a3b29f01e60b4e811ef0ce1d0d8` |

`omarchy-provision-user` sources `user/all.sh`, whose final command is `run_logged "$OMARCHY_INSTALL/user/mise.sh"`, then immediately invokes `omarchy-refresh-applications`. Refresh copies desktop declarations and invokes `bash "$OMARCHY_PATH/install/user/mise.sh"` again before updating the desktop database. Those desktop files are not inputs to the wrapper generator, and no intervening parent environment, PATH, HOME or umask change occurs.

The ISO leaf contains **13 literal wrapper calls**. Each helper reads HOME and its package/command/bin arguments, then performs `mkdir`, `rm`, `cat` and `chmod`; it does not install or execute mise tools at this point. With valid first-pass outputs and stable inputs, the second pass writes the same wrapper contents and executable modes. It still changes inode/timestamp metadata and can retry a transient failed write.

The first leaf runs under `bash -eE` and propagates helper failure. The second runs under plain Bash, and refresh ultimately returns the desktop-database command's status, which can mask an earlier error. The helper itself lacks `set -e`, so a successful final `chmod` can also mask an earlier `mkdir`, `rm` or `cat` error. A skip path therefore needs stronger first-pass output/error guarantees; deleting the first logged pass or skipping all of refresh would lose required behavior.

## Current fork and decision boundary

At fork commit `af52df7c70b85236bc74536b55ec56ab010db52d`, the same duplicate chain remains, but `user/mise.sh` has 15 ordinary wrapper calls plus `omarchy-install-hermes-cli || true`. Hermes checks package/runtime readiness and can reconcile or remove an existing runtime. A successful first leaf can coexist with a failed or partial Hermes operation, so skipping the whole second leaf is not justified by the ISO's wrapper-only argument. No decision to change those semantics has been made.

The boot audit found no independently safe removal: the later `limine-install` invocation also creates fallback/backup EFI artifacts and runs boot hooks, beyond the earlier primary-loader deployment. Available whole-branch timings do not demonstrate a duplicate UKI build. No source, VM, native job or benchmark input was changed by this audit.
