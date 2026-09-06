#!/usr/bin/python3
"""Check the real ISO coreutils verifier's existing failure contract."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--arch-root", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  root = args.arch_root.resolve()
  command = [str(root / "usr/lib/ld-linux-x86-64.so.2"), "--library-path", str(root / "usr/lib"), str(root / "usr/bin/sha256sum")]
  cases = []
  with tempfile.TemporaryDirectory(prefix="omarchy-checksum-contract-") as directory:
    base = Path(directory)
    image = base / "omarchy-root.btrfs.qcow2"
    manifest = base / "omarchy-root.btrfs.qcow2.sha256"
    image.write_bytes(b"Omarchy root image fixture\0" * 100)
    digest = subprocess.check_output(command + [image.name], cwd=base, text=True)

    def check(name, text, expected):
      manifest.write_text(text)
      process = subprocess.run(command + ["--check", "--strict", manifest.name], cwd=base, capture_output=True, text=True)
      if (process.returncode == 0) != expected:
        raise RuntimeError(f"{name} returned an unexpected status: {process.returncode}")
      cases.append({"case": name, "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr})

    check("valid checksum", digest, True)
    image.write_bytes(image.read_bytes() + b"corruption")
    check("corrupt image", digest, False)
    check("malformed manifest", "not a checksum\n", False)
    check("valid record plus malformed record", subprocess.check_output(command + [image.name], cwd=base, text=True) + "bad line\n", False)
    image.unlink()
    check("missing image", digest, False)
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(cases, indent=2) + "\n")
  print(f"{len(cases)} checksum contract cases passed")


if __name__ == "__main__":
  main()
