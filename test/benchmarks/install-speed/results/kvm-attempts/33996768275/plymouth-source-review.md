# Plymouth source audit for the failed initial boot

This derived audit accompanies the 164 unchanged original artifacts from run `33996768275`. It identifies a conditional indefinite-wait mechanism, but establishes neither the root cause nor a blocked process stack. No Plymouth setting, timeout, feature, installation input, or guest was changed during this review.

## Exact inputs and source provenance

The official Omarchy 4.0.2 ISO has SHA-256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Package metadata and selected payload members were streamed from its embedded SquashFS and package archives with `unsquashfs -cat` and libarchive; no full image copy or installation was made.

| Package | Exact ISO version |
| --- | --- |
| Plymouth | `26.134.222-2` |
| systemd | `261.2-1` |
| NetworkManager | `1.58.1-1` |
| OpenSSH | `10.5p1-1` |
| omarchy-settings | `4.0.2-1` |

Selected extracted payload SHA-256 values provide reproducible anchors:

| Package member | SHA-256 |
| --- | --- |
| `plymouth/usr/lib/systemd/system/plymouth-start.service` | `2f28bb3734ae968e6b0a17f84caeebe304f1e3b71f17d03b3c00e021da7e2fe7` |
| `plymouth/usr/lib/systemd/system/plymouth-read-write.service` | `da37f940b98d08ba26b48a43dcaba270782c7b757a7c8acc55f19c76f363c403` |
| `systemd/usr/lib/systemd/system/getty@.service` | `1ad6c472588f703549caa35f3fd51076e30c49492497aecb8f8a8b19026965d1` |
| `systemd/usr/lib/systemd/system/serial-getty@.service` | `ee46ca7068724107df49ee3bdda25a9cf01ceced1e08cca36ab3085bcf6f167d` |

The [official Plymouth release tag](https://cgit.freedesktop.org/plymouth/tag/?h=26.134.222), tag object `1aacf818fd5d1b6a7695546eb08fcbc46893dd0b`, resolves to [upstream revision `d15928a8e06c217aad0f2e3579df23fec93dc009`](https://cgit.freedesktop.org/plymouth/commit/?id=d15928a8e06c217aad0f2e3579df23fec93dc009). Direct upstream source retrieval was unavailable. Nineteen upstream C files were read through the GitHub `jhnc-oss/plymouth` mirror at that exact revision, and each downloaded file's Git blob hash was checked against the API's returned blob ID. Representative verified IDs are:

| Upstream file | Git blob SHA-1 |
| --- | --- |
| `src/client/plymouth.c` | `82f7d9205482b411cd33cbfa4a2b36c6422f4dfd` |
| `src/ply-boot-server.c` | `a2c42ab73e4076eb4a34c47874276427d2319648` |
| `src/main.c` | `aac5125da94921d8e859e1f0c002127b83f97b0a` |
| `src/libply-splash-core/ply-terminal.c` | `1a9ec353cd152a1b37d47d7c66e1899f148a1f71` |

The packaged unit files and manuals are the actual ISO bytes. The C analysis uses the upstream release; Arch's complete downstream patch/build inputs were not reconstructed.

## Conditional blocking mechanism

The rescued journal records `plymouth-start.service` timing out during `ExecStartPost` after approximately 90 seconds. The exact packaged command is `-/usr/bin/plymouth show-splash`; the unit specifies `KillMode=mixed` and `SendSIGKILL=no`.

Separately, `plymouth-read-write.service` is a `Type=oneshot` unit with `ExecStart=-/usr/bin/plymouth update-root-fs --read-write`, `Before=sysinit.target`, and no configured timeout. systemd's exact packaged manual confirms that oneshot startup timeouts default to disabled. Ordinary services retain dependencies on `sysinit.target` and `basic.target`; NetworkManager and OpenSSH do not disable those defaults. Omarchy's manager drop-ins change the stop timeout and file-descriptor limits, not the startup timeout. Thus a hanging read-write request can hold ordinary networking and SSH startup indefinitely. UFW has `DefaultDependencies=no` and runs before `sysinit.target`, so its early completion is compatible with this mechanism. See also the [upstream systemd service documentation](https://github.com/systemd/systemd/blob/v261.2/man/systemd.service.xml).

The [Plymouth client](https://github.com/jhnc-oss/plymouth/blob/d15928a8e06c217aad0f2e3579df23fec93dc009/src/client/plymouth.c#L730) installs no request timeout for either command. The [server dispatches handlers synchronously before acknowledging them](https://github.com/jhnc-oss/plymouth/blob/d15928a8e06c217aad0f2e3579df23fec93dc009/src/ply-boot-server.c#L485). The splash path invokes plugin/display operations; the details plugin replays buffered output through a blocking terminal `write()`. The graphical path can perform synchronous DRM operations. The read-write handler prepares logging, including log-file operations. SIGTERM handling also requires event-loop dispatch, while the start unit disables escalation to SIGKILL. These are possible blocking paths, not an observed stack or proof that `plymouth-read-write.service` was the pending job.

## Terminal geometry and post-failure input

The [terminal geometry code](https://github.com/jhnc-oss/plymouth/blob/d15928a8e06c217aad0f2e3579df23fec93dc009/src/libply-splash-core/ply-terminal.c#L439) uses `TIOCGWINSZ`; it accepts a successful zero-row/zero-column result. The [text display clamps cursor positions against dimensions minus one](https://github.com/jhnc-oss/plymouth/blob/d15928a8e06c217aad0f2e3579df23fec93dc009/src/libply-splash-core/ply-text-display.c#L130), explaining how zero geometry produces `ESC[-1;-1f`. The three-square text animation has bounded loops; no zero-size infinite loop was identified.

The failed serial log has 14 negative-coordinate sequences. The accepted control and accepted candidate have none, but both contain `ESC[18t` and `ESC[6n` queries. Queries alone therefore do not identify the failure. No terminal-response read was found in the inspected splash/read-write callback paths. The release already includes the [incomplete-control-sequence read fix](https://github.com/jhnc-oss/plymouth/commit/45655f12fa2d5553ab4ba509f2e203c249191664). No `ForwardToConsole=yes` setting was found in the inspected package configuration or harness; the packaged journald manual documents console forwarding as disabled by default.

Both packaged getty templates retain default dependencies on `sysinit.target` and `basic.target`, and explicitly order after `systemd-user-sessions.service` and `plymouth-quit-wait.service`. The user-sessions unit additionally orders after `network.target`. The observed tty2 login and serial-getty marker appear only after post-failure Escape/TTY diagnostic input. That is consistent with input allowing boot jobs to progress, but remains an inference: these are ordering dependencies, not proof that every preceding service succeeded, and the evidence does not contain live jobs or pre-input process stacks. Diagnostic input also changes console visibility.

The unresolved questions are which jobs were pending before input, which syscall or callback was stalled, and whether input changed execution or only what was visible. A live job snapshot plus Plymouth daemon/client state is required before assigning a cause or choosing a fix. These post-failure observations do not make the failed installation an accepted timing sample.
