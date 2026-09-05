"""Reproduce a host-only, warm-page-cache comparison including process startup."""

import argparse
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time

from chunk_sha256 import build_manifest


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("image", type=Path)
  parser.add_argument("output", type=Path)
  parser.add_argument("--repeats", type=int, default=5)
  parser.add_argument("--arch-root", type=Path, help="Optional extracted ISO runtime containing usr/bin/sha256sum and its libraries")
  args = parser.parse_args()
  source = args.image.resolve()
  script = Path(__file__).with_name("chunk_sha256.py")
  with tempfile.TemporaryDirectory() as work:
    manifest = Path(work) / "chunks.json"
    build_manifest(source, manifest)
    baseline_digest = subprocess.check_output(["sha256sum", str(source)], text=True).split()[0]
    modes = {
      "host_sha256sum": ["sha256sum", str(source)],
      "host_openssl_sha256": ["openssl", "dgst", "-sha256", str(source)],
      "chunk_sha256_workers1": [sys.executable, str(script), "verify", str(source), str(manifest), "--workers", "1"],
      "chunk_sha256_workers2": [sys.executable, str(script), "verify", str(source), str(manifest), "--workers", "2"],
      "chunk_sha256_workers4": [sys.executable, str(script), "verify", str(source), str(manifest), "--workers", "4"],
    }
    if args.arch_root:
      runtime = args.arch_root.resolve()
      modes["arch_sha256sum"] = [str(runtime / "usr/lib/ld-linux-x86-64.so.2"), "--library-path", str(runtime / "usr/lib"), str(runtime / "usr/bin/sha256sum"), str(source)]
    trials = []
    rng = random.Random(20260905)
    for repeat in range(args.repeats):
      order = list(modes)
      rng.shuffle(order)
      for method in order:
        start = time.monotonic_ns()
        result = subprocess.run(modes[method], check=True, capture_output=True, text=True)
        elapsed = (time.monotonic_ns() - start) / 1e9
        if method in ("host_sha256sum", "host_openssl_sha256", "arch_sha256sum") and baseline_digest not in result.stdout:
          raise RuntimeError("baseline digest disagrees")
        record = {"repeat": repeat, "method": method, "seconds": elapsed}
        trials.append(record)
        print(json.dumps(record), flush=True)
    medians = {method: statistics.median(t["seconds"] for t in trials if t["method"] == method) for method in modes}
    output = {
      "scope": "Verification phase only; warmed page cache; Python candidate uses host runtime; not an install-speed result.",
      "fixture": {"path": str(source), "bytes": source.stat().st_size, "sha256": baseline_digest},
      "runtime": {"python": sys.version, "platform": platform.platform(), "affinity_cpu_count": len(os.sched_getaffinity(0)), "openssl": subprocess.check_output(["openssl", "version"], text=True).strip(), "sha256sum": subprocess.check_output(["sha256sum", "--version"], text=True).splitlines()[0]},
      "seed": 20260905,
      "repeats": args.repeats,
      "trials": trials,
      "medians_seconds": medians,
      "speedup_vs_host_sha256sum": {method: medians["host_sha256sum"] / elapsed for method, elapsed in medians.items()},
      "manifest_bytes": manifest.stat().st_size,
    }
    if "arch_sha256sum" in medians:
      output["speedup_vs_arch_sha256sum"] = {method: medians["arch_sha256sum"] / elapsed for method, elapsed in medians.items()}
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"medians_seconds": medians, "speedup": output["speedup_vs_host_sha256sum"]}), flush=True)


if __name__ == "__main__":
  main()
