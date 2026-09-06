"""Time real pacman queries through baseline and candidate package helpers.

This is a component benchmark, not a complete install benchmark. It generates a
private, synthetic libalpm database and only permits read-only `pacman -Q` calls.
No packages are installed, package operations have no artificial delays, and
both revisions query the same database. Paired samples alternate revision order.

Example:
  python3 test/benchmarks/pkg-query.py --baseline-ref e8e92c5 --output /tmp/pkg-query.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time


HELPERS = ("omarchy-pkg-add", "omarchy-pkg-present", "omarchy-pkg-missing")


def timed_run(command: list[str], env: dict[str, str], expected: int) -> float:
    started = time.perf_counter_ns()
    result = subprocess.run(command, env=env, capture_output=True)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if result.returncode != expected or result.stdout or result.stderr:
        raise RuntimeError(
            f"unexpected result from {command}: exit={result.returncode}, "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--pacman", default=shutil.which("pacman"))
    parser.add_argument("--packages", type=int, default=1500, help="private database size")
    parser.add_argument("--repetitions", type=int, default=15)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.pacman:
        parser.error("a real pacman executable is required; pass --pacman PATH")
    if args.packages < 16 or args.repetitions < 3 or args.warmups < 0:
        parser.error("require at least 16 packages, 3 repetitions, and 0 warmups")

    root = Path(__file__).resolve().parents[2]
    baseline_commit = subprocess.check_output(
        ["git", "rev-parse", args.baseline_ref], cwd=root, text=True
    ).strip()
    candidate_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    pacman = str(Path(args.pacman).resolve())
    pacman_version = subprocess.check_output([pacman, "--version"], text=True).strip()

    with tempfile.TemporaryDirectory(prefix="omarchy-package-benchmark-") as directory:
        work = Path(directory)
        db = work / "db"
        (db / "local").mkdir(parents=True)
        (db / "local" / "ALPM_DB_VERSION").write_text("9\n")
        packages = [f"benchmark-package-{index:04d}" for index in range(args.packages)]
        for package in packages:
            path = db / "local" / f"{package}-1.0-1"
            path.mkdir()
            (path / "desc").write_text(
                f"%NAME%\n{package}\n\n%VERSION%\n1.0-1\n\n"
                "%DESC%\nOmarchy query benchmark fixture\n\n"
                "%ARCH%\nx86_64\n\n%REASON%\n0\n\n%VALIDATION%\nnone\n"
            )

        wrapper_bin = work / "wrappers"
        wrapper_bin.mkdir()
        wrapper = wrapper_bin / "pacman"
        wrapper.write_text(
            '#!/bin/bash\n'
            '[[ ${1:-} == "-Q" ]] || { echo "benchmark permits queries only" >&2; exit 99; }\n'
            'exec "$PACKAGE_BENCH_PACMAN" --config /dev/null --dbpath "$PACKAGE_BENCH_DB" "$@"\n'
        )
        wrapper.chmod(0o755)

        environments = {}
        source_hashes = {}
        for revision in ("baseline", "candidate"):
            source_hashes[revision] = {}
            binary_dir = work / revision / "bin"
            binary_dir.mkdir(parents=True)
            for helper in HELPERS:
                path = binary_dir / helper
                if revision == "baseline":
                    path.write_bytes(subprocess.check_output(
                        ["git", "show", f"{baseline_commit}:bin/{helper}"], cwd=root
                    ))
                else:
                    shutil.copyfile(root / "bin" / helper, path)
                path.chmod(0o755)
                source_hashes[revision][helper] = hashlib.sha256(path.read_bytes()).hexdigest()
            environments[revision] = {
                **os.environ,
                "PATH": f"{binary_dir}:{wrapper_bin}:{os.environ['PATH']}",
                "PACKAGE_BENCH_PACMAN": pacman,
                "PACKAGE_BENCH_DB": str(db),
                "LC_ALL": "C",
            }

        # Prove the fixture is understood by libalpm before timing the helpers.
        result = subprocess.run(
            [pacman, "--config", "/dev/null", "--dbpath", str(db), "-Qq"],
            check=True, capture_output=True, text=True,
        )
        if set(result.stdout.splitlines()) != set(packages):
            raise RuntimeError("pacman did not recognize the private package database")

        workloads = []
        for helper in HELPERS:
            for count in (1, 3, 5, 16):
                workloads.append((helper, packages[:count], "all-present"))
            if helper != "omarchy-pkg-add":
                workloads.append((helper, ["benchmark-absent", *packages[:4]], "missing-first"))

        rows = []
        for helper, targets, scenario in workloads:
            expected = int((helper == "omarchy-pkg-missing") == (scenario == "all-present"))
            # pkg-add succeeds for the all-present workload, like pkg-present.
            command = [helper, *targets]
            samples = {"baseline": [], "candidate": []}
            for index in range(args.warmups + args.repetitions):
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                for revision in order:
                    elapsed = timed_run(command, environments[revision], expected)
                    if index >= args.warmups:
                        samples[revision].append(elapsed)
            baseline_ms = statistics.median(samples["baseline"])
            candidate_ms = statistics.median(samples["candidate"])
            rows.append({
                "helper": helper,
                "targets": len(targets),
                "scenario": scenario,
                "baseline_median_ms": baseline_ms,
                "candidate_median_ms": candidate_ms,
                "speedup": baseline_ms / candidate_ms,
                "samples_ms": samples,
            })

        report = {
            "benchmark": "package-helper real-pacman queries on a synthetic local database",
            "scope": "component only; no package installation or whole-install speed claim",
            "baseline_commit": baseline_commit,
            "candidate_head": candidate_commit,
            "candidate_source": "working tree",
            "helper_sha256": source_hashes,
            "kernel": platform.platform(),
            "pacman_version": pacman_version,
            "database_packages": args.packages,
            "cache_state": "warm after private fixture validation and per-workload warmups",
            "repetitions": args.repetitions,
            "warmups": args.warmups,
            "results": rows,
        }
        output = json.dumps(report, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output)
        print(output, end="")


if __name__ == "__main__":
    main()
