# Four native stock diagnostic calibrations pass

Run [34000377432](https://github.com/ryankirkman/omarchy/actions/runs/34000377432) on commit `80f95c07f4606accadfe208938d9a7ec56ad7083` completed all four requested fresh stock installations and independent media-free reboots. No candidate was built or compared, and no failure-console or rescue intervention was invoked.

| Calibration | Host boot-to-installed-SSH interval | Guest installer |
| --- | ---: | ---: |
| 01 | 192.284–196.726 s | 134.684 s |
| 02 | 181.700–186.197 s | 129.220 s |
| 03 | 182.084–186.571 s | 130.235 s |
| 04 | 183.596–188.041 s | 130.769 s |

Every original passes the strict comparator's raw-calibration reader: clean QEMU exit, uninterrupted installation, all installer phases complete, installed-root validation, source pages verified cold, and successful standalone reboot. Each has 941 identical package versions, 158 explicit packages and 941 complete zero-missing package-file count rows. Machine, SSH, pacman signing and Btrfs identities are distinct across all four installations.

All original small artifact files are preserved byte-for-byte. `artifact-seal.json` inventories their sizes and SHA256 hashes; `attempt.json` records the original workflow/commit, independently verified ZIP digest and validation results. The reader uses `allow_unsealed=True` because these are original diagnostic calibrations, which the native driver does not seal as paired comparison samples. No original file was modified or promoted into a performance comparison.

These four successful runs do not explain the intermittent boot failure seen in prior trials. They also do not change the prior result of one validated pair with a 2.19046x conservative speedup; the required three fresh pairs remain outstanding.
