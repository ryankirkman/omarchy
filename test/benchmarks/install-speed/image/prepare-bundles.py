#!/usr/bin/python3
"""Bundle pinned upstream installer code and builder inputs for a live-ISO test."""
import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

PIN = "dbffaa6c65344d644627a023c28661e08382b8fa"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("iso_checkout", type=Path)
parser.add_argument("output_directory", type=Path)
args = parser.parse_args()
actual = subprocess.check_output(["git", "-C", str(args.iso_checkout), "rev-parse", "HEAD"], text=True).strip()
if actual != PIN:
    parser.error(f"expected upstream {PIN}, got {actual}")
if subprocess.check_output(["git", "-C", str(args.iso_checkout), "status", "--porcelain", "--untracked-files=no"], text=True).strip():
    parser.error("upstream checkout has tracked edits; a clean reproducible source is required")
args.output_directory.mkdir(parents=True, exist_ok=True)
local = Path(__file__).resolve().parent


def write_bundle(name, paths):
    target = args.output_directory / name
    if target.exists():
        parser.error(f"refusing to overwrite {target}")
    entries = []
    with tarfile.open(target, "w") as archive:
        for source, archive_name in sorted(paths, key=lambda pair: pair[1]):
            data = source.read_bytes()
            info = tarfile.TarInfo(archive_name)
            info.size = len(data)
            info.mode = 0o755 if source.suffix == ".sh" or source.name.startswith("omarchy-") else 0o644
            info.uid = info.gid = info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
            entries.append({"path": archive_name, "sha256": hashlib.sha256(data).hexdigest()})
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".manifest.json").write_text(json.dumps({"upstream_commit": PIN, "files": entries, "sha256": digest}, indent=2) + "\n")
    target.with_suffix(".tar.sha256").write_text(f"{digest}  {target.name}\n")


build_paths = [(args.iso_checkout / "builder" / name, name) for name in ("image.packages", "archinstall.packages")]
build_paths += [(local / name, name) for name in ("build-root-from-iso.sh", "select-image-packages.py", "validate-image-manifest.py")]
write_bundle("builder-bundle.tar", build_paths)

airootfs = args.iso_checkout / "configs/airootfs"
overlay_paths = [(path, str(path.relative_to(airootfs))) for path in (airootfs / "usr/share/omarchy-iso/orchestrator").glob("*.py")]
for name in ("omarchy-iso-install", "omarchy-iso-cleanup-disk", "omarchy-release-install-target", "omarchy-wait-root-image-verify"):
    overlay_paths.append((airootfs / "usr/local/bin" / name, "usr/local/bin/" + name))
overlay_paths.append((airootfs / "etc/systemd/system/omarchy-root-image-verify.service", "etc/systemd/system/omarchy-root-image-verify.service"))
write_bundle("installer-overlay.tar", overlay_paths)
print(f"Wrote reproducible bundles at {args.output_directory}")
