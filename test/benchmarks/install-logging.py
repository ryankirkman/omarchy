#!/usr/bin/env python3
"""Measure setup logging overhead. This is deliberately NOT an install timer."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import tempfile
import time


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--baseline-ref", required=True)
  parser.add_argument("--samples", type=int, default=7)
  parser.add_argument("--leaves", type=int, default=80)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  if args.samples < 3 or args.leaves < 1:
    parser.error("at least three samples and one leaf are required")
  repo = Path(__file__).resolve().parents[2]
  relative = "install/helpers/logging.sh"
  baseline_sha = subprocess.check_output(
    ["git", "rev-parse", "--verify", f"{args.baseline_ref}^{{commit}}"], cwd=repo, text=True
  ).strip()
  baseline = subprocess.check_output(
    ["git", "show", f"{baseline_sha}:{relative}"], cwd=repo
  )
  candidate = (repo / relative).read_bytes()
  samples = {"baseline": [], "candidate": []}
  with tempfile.TemporaryDirectory(prefix="omarchy-logging-bench-") as directory:
    work = Path(directory)
    (work / "baseline.sh").write_bytes(baseline)
    (work / "candidate.sh").write_bytes(candidate)
    leaf = work / "leaf.sh"
    leaf.write_text("printf '%s\\n' 'leaf output'\n")
    runner = work / "runner.sh"
    runner.write_text('''#!/bin/bash
set -euo pipefail
source "$1"
for ((i=0; i<$3; i++)); do
  run_logged "$2"
done
''')
    env = os.environ.copy()
    env["OMARCHY_LOG_TO_STDOUT"] = "1"
    env.pop("OMARCHY_INSTALL_DEBUG", None)
    reference = None
    for iteration in range(args.samples + 2):
      order = ("baseline", "candidate") if iteration % 2 == 0 else ("candidate", "baseline")
      for label in order:
        started = time.perf_counter()
        result = subprocess.run(
          ["bash", str(runner), str(work / f"{label}.sh"), str(leaf), str(args.leaves)],
          env=env, check=True, capture_output=True, text=True
        )
        elapsed = time.perf_counter() - started
        normalized = re.sub(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", "[timestamp]", result.stdout)
        if normalized.count("leaf output\n") != args.leaves:
          raise RuntimeError("not every setup leaf executed")
        if reference is None:
          reference = normalized
        if normalized != reference:
          raise RuntimeError("baseline and candidate produced different logs")
        if iteration >= 2:
          samples[label].append(elapsed)
  medians = {label: statistics.median(values) for label, values in samples.items()}
  report = {
    "schema_version": 1,
    "kind": "logging_microbenchmark",
    "scope": "Real Bash subprocesses and logging; trivial leaves isolate logging overhead. Not full installation.",
    "baseline_commit": baseline_sha,
    "source_sha256": {
      "baseline": hashlib.sha256(baseline).hexdigest(),
      "candidate": hashlib.sha256(candidate).hexdigest(),
    },
    "platform": platform.platform(),
    "bash_version": subprocess.check_output(["bash", "--version"], text=True).splitlines()[0],
    "leaves_per_sample": args.leaves,
    "warmup_pairs": 2,
    "sample_seconds": samples,
    "median_seconds": medians,
    "speedup": medians["baseline"] / medians["candidate"],
    "identical_log_content_except_timestamps": True,
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(report, indent=2) + "\n")
  print(json.dumps(report, indent=2))


if __name__ == "__main__":
  main()
