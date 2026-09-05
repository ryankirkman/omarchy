#!/usr/bin/python3
"""Mirror pinned PR #145's package selection using the OFFICIAL ISO's lists."""
import re
import sys
from pathlib import Path

bundle, iso = map(Path, sys.argv[1:])
packages = set()
for path in (bundle / "archinstall.packages", bundle / "image.packages", iso / "omarchy-base.packages"):
    packages.update(line.strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#"))
targets = {"OMARCHY_RUNTIME_PACKAGE": "omarchy", "OMARCHY_SETTINGS_PACKAGE": "omarchy-settings", "OMARCHY_NVIM_PACKAGE": "omarchy-nvim"}
target_file = iso / "package-targets"
if target_file.exists():
    for line in target_file.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in targets:
            targets[key.strip()] = value.strip().strip("\"'")
packages.update(targets.values())
packages.difference_update({"linux-t2", "amd-ucode", "intel-ucode", "sof-firmware", "alsa-firmware", "tailscale"})
if not packages or any(not re.fullmatch(r"[a-zA-Z0-9@_+.-]+", package) for package in packages):
    raise SystemExit("Invalid or empty image package selection")
print("\n".join(sorted(packages)))
