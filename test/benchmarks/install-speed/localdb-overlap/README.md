# Optional locate-index overlap

This isolated component moves the required `updatedb` call from the end of system configuration to the end of the existing user finalization branch. The original script, logging, errors and index generation remain. Both finalization branches must finish before boot validation and factory snapshot creation; nothing moves to first boot. Existing image producers, payloads, native defaults and workflow selections are unchanged by this directory.

The three accepted direct-restore candidates from native run `34001279394` spent 7.23–7.61 seconds configuring the system. Their boot branch took 9.70–10.98 seconds and their ordered user branch took 5.57–6.76 seconds, leaving 4.08–4.22 seconds between branch completion times. Those are measured phase spans, not a measured `updatedb` duration or a speed claim. The new `Indexing installed files` substep uses the existing branch monotonic clock and records success or failure in installation timing evidence. Only a complete fresh comparison with all existing validation gates can establish a benefit.

## Exact source and behavior

The input is the existing direct-restore phases module, SHA256 `8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458`, derived from [Omarchy ISO PR145 at `dbffaa6c65344d644627a023c28661e08382b8fa`](https://github.com/omacom-io/omarchy-iso/tree/dbffaa6c65344d644627a023c28661e08382b8fa). The output SHA256 is `6914997592990435c723688e594ed189192e961423324b505a66be0de1948128`. The producer requires exact source and unique replacement anchors; drift fails preparation.

Before target system setup and again before late indexing, the injected code verifies all four regular target files below. It also requires the original system executable's execute permission. The files are from the official Omarchy 4.0.2 ISO's `omarchy 4.0.2-1` package; their bytes were checked against that package's MTREE SHA256 records.

| Target file | SHA256 |
| --- | --- |
| `/usr/bin/omarchy-apply-system` | `403b83536e69812667464bd188d0a4f90466e867f5192a30710b90d26bdd93ca` |
| `/usr/share/omarchy/install/post-install/all.sh` | `c1b482de88d9ae94cc3c94a468c2d59b8209f7cfc59c5d711318e5c5716113ab` |
| `/usr/share/omarchy/install/post-install/localdb.sh` | `55df186f6436d92c8bb7e0c9721b596b2a63813053782c636384db4fa00d6f57` |
| `/usr/share/omarchy/install/helpers/logging.sh` | `61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27` |

System setup executes its original body through `bash -c`, retaining the original `$0`, arguments, environment, `set -euo pipefail` and calls. Only its single source of `post-install/all.sh` is expanded to that exact file's contents without the localdb call. No package-owned target file is changed. The later root command sources the unchanged logging helper and uses its original `run_logged` on the unchanged localdb script. A pending-state check prevents duplicate indexing. Deferred provisioning retains the original serial system command and makes the late step a no-op.

Both the original system command and the late root command use `_run_target_setup_command`, including its existing private `arch-chroot`, target-log bind and teardown. Their outer offline-cache and `/opt/packages` bind mounts are the same. The preceding user command has finished and retired its log bind before the late root command starts; concurrent boot finalization has its own private mount namespace and does not bind that log.

The exact packaged `/etc/updatedb.conf` SHA256 is `d00796741e2194032d0185b40de70ff5c8a11fda416a70434eb0aa2020981f91`. It prunes transient filesystem types including proc, sysfs, devtmpfs, tmpfs and iso9660, and paths including `/var/cache`, `/var/lib/pacman/local`, `/tmp`, `/mnt`, `/var/run` and `/var/tmp`; names `.git`, `.hg` and `.svn` are also pruned. The unchanged earlier `install/config/locate.sh`, SHA256 `5e449dcd9db5e0bbf963742e712098b2b4726a20e8fca4f3d56cec1063fb6d21`, sets `PRUNE_BIND_MOUNTS=no` and adds `/.snapshots` in both schedules, preserving mounted Btrfs child coverage. The late index intentionally includes user/provisioning paths created after the old scan. Its bytes need not equal the earlier index. Neither `/boot` nor vfat is pruned: concurrent UKI/menu creation can expose temporary or replaced filenames to this non-snapshot traversal. The old earlier index also did not represent final boot files. No prune setting changes, and no exact index-byte equality is claimed; validation still waits for the completed index and completed boot branch, and `@factory` is created afterward.

## Prepare and verify

Pass an already prepared direct-restore payload; every existing payload file is copied unchanged and checked against its complete manifest. The component adds a small phases module and reuses the existing supplemental image. Its preflight invokes the copied direct-restore preflight once, verifies the resulting direct phases hash, installs its own phases, and checks the final bytes. The manifest records complete file hashes, modes and sizes, the base manifest, runtime source guards, and the selected preflight hash.

```bash
python3 test/benchmarks/install-speed/localdb-overlap/prepare-payload.py \
  --iso-source /tmp/omarchy-iso-fast \
  --base-payload /path/to/direct-payload \
  --output /path/to/localdb-payload

python3 test/benchmarks/install-speed/localdb-overlap/contract-test.py \
  --iso-source /tmp/omarchy-iso-fast
```

The tiny `fixtures/runtime` files preserve the four exact official-package sources for reproducible shell contracts, including the original logger rather than the separately optimized repository logger. They retain Omarchy's copyright attribution to David Heinemeier Hansson and are distributed under the repository's [MIT license](../../../../LICENSE). The original ISO SHA256 is `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. All 13 focused contracts passed. They do not establish installed filesystem or performance equivalence; a full native comparison remains required.

`guest-contract.py` accepts the patched phases file, the same runtime fixtures and explicit `--updatedb`/`--plocate` executable paths. It runs the unchanged wrapper and logger with other setup commands confined to stubs, but uses real indexing/query programs on a temporary tree and database. It checks a file created after early setup, exactly-once indexing, unchanged fixture scripts and genuine failure propagation when the database output parent is absent. Explicit temporary index roots and pruning overrides are test containment only; installed pruning remains unchanged. The live ISO does not contain these programs, so a disposable guest check can extract only their exact offline-package executables into `/tmp`, without installing packages. Expected hashes for `plocate 1.1.24-2` are `04c4523df38b126bd1fe020d43f7b1ae932ba27567bf48eac24ba5422bff9936` for `updatedb` and `d3c76e8c42b8627f9e4187d39ce6b41593717e063a2eec1df3754b629c3c7409` for `plocate`. The check records executable hashes, fails on missing dependencies/options, and cleans its temporary index data. It uses no installed target and is a functional check, not an install-speed measurement.

The [original shared-guest functional result](evidence/shared-guest-functional.json) passed with these exact engine and phase hashes. Real indexing ran once and the query found the new user file; the real missing-output-directory error returned status 1 through the unchanged logger. Both cases preserved fixture bytes/modes and removed their temporary index trees. The [cleanup record](evidence/shared-guest-cleanup.json) verifies the exact input hashes and removal of the remaining test bundle and extracted engines. No package was installed and no target disk or system database was changed.
