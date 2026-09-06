#!/usr/bin/env python3
"""Independently verify host target files after the builder powers off cleanly."""

import argparse
import hashlib
import json
from pathlib import Path


MIB = 1024 ** 2


def digest(path, offset=0, count=None):
    result = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        stream.seek(offset)
        remaining = path.stat().st_size - offset if count is None else count
        while remaining:
            data = stream.read(min(remaining, MIB))
            if not data:
                raise RuntimeError("Short host read")
            result.update(data)
            remaining -= len(data)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    fixture = json.loads((directory / "fixture-manifest.json").read_text())
    runtime = json.loads((directory / "vm/manifest.json").read_text())
    results = json.loads((directory / "guest-evidence/results.json").read_text())
    if runtime.get("qemu_exit_status") != 0 or results["status"] != "passed":
        raise RuntimeError("Require passing guest evidence and an observed clean QEMU exit")
    if digest(directory / "source.qcow2") != fixture["source_sha256"]:
        raise RuntimeError("Host source changed")
    if digest(directory / "expected.raw") != fixture["expected_raw_sha256"]:
        raise RuntimeError("Host independent expected bytes changed")
    records = []
    for case in results["cases"]:
        path = directory / f"target-{case['case']}.raw"
        if path.stat().st_size != 96 * MIB or case["status"] != "passed":
            raise RuntimeError("Wrong target size or guest case status")
        record = {"case": case["case"], "path": str(path), "bytes": path.stat().st_size,
                  "allocated_bytes": path.stat().st_blocks * 512,
                  "image_sha256": digest(path, 0, 64 * MIB),
                  "trailing_sha256": digest(path, 64 * MIB, 32 * MIB),
                  "whole_device_sha256": digest(path)}
        for key in ("image_sha256", "trailing_sha256", "whole_device_sha256"):
            if record[key] != case["verification"][key]:
                raise RuntimeError(f"Post-poweroff host target differs: {case['case']}: {key}")
        if record["image_sha256"] != fixture["expected_raw_sha256"]:
            raise RuntimeError("Host target differs from independent expected image")
        records.append(record)
    evidence = {"status": "passed", "qemu_exit_status": 0, "targets": records,
                "host_source_unchanged": True,
                "scope": "Clean guest poweroff and full host-file readback; no host-crash durability claim",
                "unused_runner_target_allocated_bytes": (directory / "vm/target.qcow2").stat().st_blocks * 512}
    output = directory / "host-post-poweroff-verification.json"
    if output.exists():
        raise RuntimeError("Do not overwrite original verification evidence")
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"status": "passed", "targets_verified": len(records), "evidence": str(output)}))


if __name__ == "__main__":
    main()
