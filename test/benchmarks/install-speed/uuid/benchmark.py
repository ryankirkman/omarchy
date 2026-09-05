#!/usr/bin/env python3
"""Benchmark real unmounted Btrfs UUID changes on copies of repository files.

This measures one installer stage, never a full Omarchy installation. It creates
ordinary temporary files only; it never mounts filesystems or opens host disks.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import statistics
import subprocess
import tempfile
import time


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = str(path.relative_to(root))
        entry = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        if path.is_symlink():
            entry.update(type="symlink", target=os.readlink(path))
        elif path.is_file():
            entry.update(type="file", size=info.st_size, sha256=file_digest(path))
        elif path.is_dir():
            entry.update(type="directory")
        else:
            raise RuntimeError(f"unsupported corpus file type: {relative}")
        entries[relative] = entry
    return entries


def superblock(image):
    output = run(["btrfs", "inspect-internal", "dump-super", "-f", str(image)]).stdout
    values = {}
    for key in ("fsid", "metadata_uuid", "dev_item.uuid", "dev_item.fsid"):
        found = re.search(r"^" + re.escape(key) + r"\s+([0-9a-f-]{36})", output, re.MULTILINE)
        values[key] = found.group(1) if found else None
    if not values["fsid"] or not values["metadata_uuid"]:
        raise RuntimeError(f"could not read Btrfs identity: {output}")
    flags = re.search(r"^incompat_flags\s+(0x[0-9a-f]+)", output, re.MULTILINE)
    values["incompat_flags"] = int(flags.group(1), 16) if flags else None
    return values


def build_corpus(source, destination):
    tracked = run(["git", "-C", str(source), "ls-files", "-z"]).stdout.split("\0")
    files = 0
    for relative in filter(None, tracked):
        original = source / relative
        target = destination / relative
        if not original.exists() and not original.is_symlink():
            raise RuntimeError(f"tracked corpus path absent: {original}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            target.symlink_to(os.readlink(original))
        elif original.is_file():
            shutil.copy2(original, target)
        else:
            raise RuntimeError(f"tracked corpus path is not a file: {original}")
        files += 1
    return files


def validate_filesystem(image, destination, expected):
    check = run(["btrfs", "check", "--readonly", "--check-data-csum", str(image)])
    destination.mkdir()
    run(["btrfs", "restore", "-m", "-S", "-x", str(image), str(destination)])
    actual = manifest(destination)
    if actual != expected:
        differences = [key for key in sorted(actual.keys() | expected.keys()) if actual.get(key) != expected.get(key)]
        raise RuntimeError(f"restored contents or metadata differ: {differences[:20]}")
    return {"btrfs_check": "pass", "restored_manifest": "identical", "check_output": check.stdout + check.stderr}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, help="Parent for reproducible temporary images; no device paths accepted")
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("at least two repetitions are required")
    source = args.source.resolve()
    for tool in ("mkfs.btrfs", "btrfstune", "btrfs", "git", "cp"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"required command is unavailable: {tool}")
    if args.scratch:
        args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omarchy-uuid-", dir=args.scratch, ignore_cleanup_errors=True) as temporary:
        workspace = Path(temporary)
        corpus = workspace / "corpus"
        corpus.mkdir()
        source_commit = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
        source_diff = run(["git", "-C", str(source), "diff", "HEAD", "--"]).stdout
        count = build_corpus(source, corpus)
        if source_commit != run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip() or source_diff != run(["git", "-C", str(source), "diff", "HEAD", "--"]).stdout:
            raise RuntimeError("source checkout changed while staging the corpus; retry on a stable checkout")
        expected = manifest(corpus)
        corpus_bytes = sum(item.get("size", 0) for item in expected.values())
        # DUP metadata and spare space remain at the filesystem defaults.
        image_bytes = max(1024**3, ((corpus_bytes * 2 + 512 * 1024**2 + 256 * 1024**2 - 1) // (256 * 1024**2)) * (256 * 1024**2))
        baseline = workspace / "baseline.btrfs"
        with baseline.open("wb") as stream:
            stream.truncate(image_bytes)
        run(["mkfs.btrfs", "-f", "-r", str(corpus), str(baseline)])
        original = superblock(baseline)
        initial_validation = validate_filesystem(baseline, workspace / "restore-original", expected)
        seen = {original["fsid"]}
        rows = []
        for repetition in range(args.repeats):
            order = ("full", "metadata") if repetition % 2 == 0 else ("metadata", "full")
            for mode in order:
                candidate = workspace / f"{mode}-{repetition}.btrfs"
                # A fresh regular file for each trial; copying is outside the
                # timer, identical between treatments, and preserves sparsity.
                run(["cp", "--reflink=never", "--sparse=always", str(baseline), str(candidate)])
                descriptor = os.open(candidate, os.O_RDWR)
                try:
                    os.fsync(descriptor)
                    started = time.perf_counter()
                    tune = run(["btrfstune", "-f", "-m" if mode == "metadata" else "-u", str(candidate)])
                    os.fsync(descriptor)
                    elapsed = time.perf_counter() - started
                finally:
                    os.close(descriptor)
                identity = superblock(candidate)
                if identity["fsid"] in seen:
                    raise RuntimeError("filesystem ID was retained or duplicated")
                seen.add(identity["fsid"])
                if mode == "metadata" and identity["metadata_uuid"] != original["fsid"]:
                    raise RuntimeError("metadata UUID did not preserve the original metadata identity")
                if mode == "metadata" and not identity["incompat_flags"] & 0x400:
                    raise RuntimeError("metadata UUID compatibility feature was not enabled")
                if mode == "full" and identity["dev_item.fsid"] != identity["fsid"]:
                    raise RuntimeError("full UUID change did not update the device metadata identity")
                if mode == "full" and identity["incompat_flags"] != original["incompat_flags"]:
                    raise RuntimeError("full UUID change unexpectedly altered compatibility features")
                validation = validate_filesystem(candidate, workspace / f"restore-{mode}-{repetition}", expected)
                row = {"repetition": repetition, "mode": mode, "elapsed_seconds": elapsed, "identity": identity, "validation": validation, "tune_output": tune.stdout + tune.stderr}
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("repetition", "mode", "elapsed_seconds")}), flush=True)
                candidate.unlink()
                shutil.rmtree(workspace / f"restore-{mode}-{repetition}")
        durations = {mode: [row["elapsed_seconds"] for row in rows if row["mode"] == mode] for mode in ("full", "metadata")}
        summaries = {mode: {"median_seconds": statistics.median(values), "mean_seconds": statistics.mean(values), "stdev_seconds": statistics.stdev(values), "min_seconds": min(values), "max_seconds": max(values)} for mode, values in durations.items()}
        result = {
            "schema_version": 1,
            "benchmark": "unmounted Btrfs UUID stage, cached local regular-file fixture",
            "full_install_measured": False,
            "limitations": ["Not a complete installer measurement", "No mount, two-clone simultaneous-mount, installed-boot, Limine, or Snapper rollback validation", "Repository corpus is smaller than a complete installed distribution", "Fixture cloning and validation occur outside timing and warm local filesystem caches", "Regular-file I/O rather than a physical block device or LUKS mapper"],
            "system": {"platform": platform.platform(), "cpu_count": os.cpu_count(), "machine": platform.machine(), "btrfs_version": run(["btrfs", "version"]).stdout.strip()},
            "source": {"path": str(source), "commit": source_commit, "tracked_diff_sha256": hashlib.sha256(source_diff.encode()).hexdigest(), "tracked_files": count, "bytes": corpus_bytes, "manifest_sha256": hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()},
            "image": {"bytes": image_bytes, "identity": original, "initial_validation": initial_validation},
            "results": rows,
            "summary": summaries,
            "stage_speedup_median": summaries["full"]["median_seconds"] / summaries["metadata"]["median_seconds"],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"summary": summaries, "stage_speedup_median": result["stage_speedup_median"]}), flush=True)


if __name__ == "__main__":
    main()
