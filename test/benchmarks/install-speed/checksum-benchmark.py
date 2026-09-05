#!/usr/bin/python3
"""Compare the exact SHA-256 implementations shipped on an Omarchy ISO.

Extract usr/bin/{sha256sum,openssl}, usr/lib/{ld-linux-x86-64.so.2,
libc.so.6,libcrypto.so.3,libssl.so.3} from its airootfs.sfs into --arch-root.
The extracted loader lets those unmodified binaries run on the host kernel
with their matching libraries, without chroot or a writable root filesystem.
This measures hashing, not installation or a cold USB medium.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import re
import resource
import statistics
import subprocess
import time


def read_optional(path):
  try:
    return Path(path).read_text().strip()
  except OSError:
    return None


def version(command):
  result = subprocess.run(command, capture_output=True, text=True, check=True)
  return result.stdout.strip()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--image", type=Path, required=True)
  parser.add_argument("--manifest", type=Path, required=True)
  parser.add_argument("--arch-root", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--rounds", type=int, default=3)
  parser.add_argument("--include-host", action="store_true")
  args = parser.parse_args()
  if args.rounds < 1:
    parser.error("--rounds must be positive")
  image = args.image.resolve()
  arch = args.arch_root.resolve()
  manifest = args.manifest.read_text()
  match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(image.name) + r"\n", manifest)
  if not match:
    parser.error("manifest must contain exactly one standard SHA-256 line for the image")
  expected = match.group(1)
  loader = [str(arch / "usr/lib/ld-linux-x86-64.so.2"), "--library-path", str(arch / "usr/lib")]
  commands = {
    "arch_sha256sum": loader + [str(arch / "usr/bin/sha256sum"), str(image)],
    "arch_openssl": loader + [str(arch / "usr/bin/openssl"), "dgst", "-sha256", "-r", str(image)],
  }
  if args.include_host:
    commands["host_sha256sum"] = ["sha256sum", str(image)]
  metadata = image.stat()
  result = {
    "recorded_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "scope": "exact SHA-256 phase on native host CPU, not install time or VM time",
    "image_name": image.name,
    "image_bytes": metadata.st_size,
    "expected_sha256": expected,
    "cache_policy": "read the complete file before timing; warm intended, no cache eviction or global cache dropping",
    "kernel": platform.platform(),
    "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")), None),
    "cpu_affinity": sorted(os.sched_getaffinity(0)),
    "cpu_quota": read_optional("/sys/fs/cgroup/cpu.max"),
    "memory_limit": read_optional("/sys/fs/cgroup/memory.max"),
    "initial_load_average": os.getloadavg(),
    "versions": {
      "arch_sha256sum": version(loader + [str(arch / "usr/bin/sha256sum"), "--version"]),
      "arch_openssl": version(loader + [str(arch / "usr/bin/openssl"), "version", "-a"]),
      "host_sha256sum": version(["sha256sum", "--version"]),
    },
    "runs": [],
  }
  with image.open("rb") as stream:
    while stream.read(8 * 1024 * 1024):
      pass
  names = list(commands)
  for round_index in range(args.rounds):
    order = names if round_index % 2 == 0 else names[::-1]
    for name in order:
      before = resource.getrusage(resource.RUSAGE_CHILDREN)
      start = time.perf_counter()
      process = subprocess.run(commands[name], capture_output=True, text=True, check=True)
      elapsed = time.perf_counter() - start
      after = resource.getrusage(resource.RUSAGE_CHILDREN)
      actual = process.stdout.split()[0]
      if actual != expected:
        raise RuntimeError(f"{name} did not reproduce the expected digest: {actual}")
      run = {
        "round": round_index + 1,
        "implementation": name,
        "elapsed_seconds": elapsed,
        "user_seconds": after.ru_utime - before.ru_utime,
        "system_seconds": after.ru_stime - before.ru_stime,
        "input_blocks": after.ru_inblock - before.ru_inblock,
        "mib_per_second": metadata.st_size / (1024 ** 2) / elapsed,
        "sha256": actual,
      }
      result["runs"].append(run)
      print(json.dumps(run), flush=True)
      args.output.parent.mkdir(parents=True, exist_ok=True)
      args.output.write_text(json.dumps(result, indent=2) + "\n")
  final_metadata = image.stat()
  if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ino) != (final_metadata.st_size, final_metadata.st_mtime_ns, final_metadata.st_ino):
    raise RuntimeError("image changed while benchmarking")
  result["median_seconds"] = {
    name: statistics.median(run["elapsed_seconds"] for run in result["runs"] if run["implementation"] == name)
    for name in names
  }
  result["median_cpu_seconds"] = {
    name: statistics.median(run["user_seconds"] + run["system_seconds"] for run in result["runs"] if run["implementation"] == name)
    for name in names
  }
  result["arch_openssl_speedup"] = result["median_seconds"]["arch_sha256sum"] / result["median_seconds"]["arch_openssl"]
  args.output.write_text(json.dumps(result, indent=2) + "\n")
  print(json.dumps({"median_seconds": result["median_seconds"], "arch_openssl_speedup": result["arch_openssl_speedup"]}), flush=True)


if __name__ == "__main__":
  main()
