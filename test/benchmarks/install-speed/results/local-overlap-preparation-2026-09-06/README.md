# Local combined payload preparation

The actual native preparation helper successfully staged the pinned fast-reboot payload through direct writes, required-index overlap and foreground animation. Both component contract suites passed: 13 index contracts and 14 dashboard/payload contracts. The final payload contains 18 files, with inherited files and modes preserved. These are local correctness checks, not installation timing samples.

`validation.json` retains each input/output manifest digest and the final preflight digest. The manifests preserve full output file inventories, exact target script/source pins and the activation chain. `source-sha256.json` pins the native integration and component source used for this check. The native contract's five focused checks also passed, including constructing and inspecting real tiny initramfs archives for all three early variants with one unchanged control.

The actual ISO checkout was PR145 commit `dbffaa6c65344d644627a023c28661e08382b8fa`. Input files came from the previously validated fast-reboot fixture; this check did not build or modify a supplemental ISO, run an installer or launch a VM. Separate component evidence covers the real-console and indexing-engine checks. Only fresh native control/candidate installations can establish the combined change's performance.
