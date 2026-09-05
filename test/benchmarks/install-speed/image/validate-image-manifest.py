#!/usr/bin/python3
"""Require image packages to match the baseline; normalize only install reasons."""
import json
import sys
from pathlib import Path

baseline, output = map(Path, sys.argv[1:])


def inventory(path):
    result = {}
    for line in path.read_text().splitlines():
        name, version = line.split()
        if name in result:
            raise SystemExit(f"Duplicate package: {name}")
        result[name] = version
    if not result:
        raise SystemExit(f"Empty package inventory: {path}")
    return result


reference = inventory(baseline / "package-manifest.txt")
image = inventory(output / "image-package-manifest.txt")
different = {name: {"image": version, "baseline": reference.get(name)} for name, version in image.items() if reference.get(name) != version}
if different:
    raise SystemExit("Image would change the baseline package set: " + json.dumps(different, sort_keys=True))
explicit = set((baseline / "package-explicit.txt").read_text().splitlines())
if not explicit <= reference.keys():
    raise SystemExit("Explicit baseline names are absent from its complete inventory")
(output / "image-explicit-packages.txt").write_text("".join(name + "\n" for name in sorted(explicit & image.keys())))
(output / "image-package-delta.json").write_text(json.dumps({"baseline_packages": len(reference), "image_packages": len(image), "remaining_packages": {name: reference[name] for name in sorted(reference.keys() - image.keys())}, "reasons_source": "baseline pacman -Qqe"}, indent=2) + "\n")
