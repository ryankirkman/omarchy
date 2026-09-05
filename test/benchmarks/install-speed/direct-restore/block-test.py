#!/usr/bin/env python3
"""Prepare and supervise a small, disposable real-device restore correctness VM.

All mutable artifacts live under /tmp. Existing installation media are reused
read-only. This has no install timing or performance acceptance criterion.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
MIB = 1024 ** 2
SOURCE_SIZE = 64 * MIB
TARGET_SIZE = 96 * MIB
PHASES_SHA = "8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458"
CASES = [(512, True), (512, False), (4096, True), (4096, False)]


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def guarded_directory(path):
    path = path.resolve()
    if not path.is_relative_to(Path("/tmp")) or path == Path("/tmp"):
        raise ValueError("Mutable fixture directory must be below /tmp")
    return path


def tool_environment(toolchain):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(toolchain / "usr/lib/x86_64-linux-gnu")
    env["QEMU_MODULE_DIR"] = str(toolchain / "usr/lib/x86_64-linux-gnu/qemu")
    return env


def command(argv, env, log, *, parsed=False):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    log.append({"argv": [str(item) for item in argv], "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode:
        raise RuntimeError(f"Command failed: {argv}: {result.stderr}")
    return json.loads(result.stdout) if parsed else result.stdout


def prepare(args):
    directory = guarded_directory(args.directory)
    directory.mkdir(parents=True, exist_ok=False)
    commands = []
    try:
        env = tool_environment(args.toolchain)
        image = args.toolchain / "usr/bin/qemu-img"
        io = args.toolchain / "usr/bin/qemu-io"
        raw = directory / "expected.raw"
        with raw.open("xb") as file:
            file.truncate(SOURCE_SIZE)
            file.write(b"\x3d" * (8 * MIB))
            file.write(bytes(range(256)) * (8 * MIB // 256))
            file.seek(40 * MIB)
            file.write((b"\x6b" * 4096 + b"\0" * 4096) * (8 * MIB // 8192))
            file.seek(63 * MIB + 512)
            file.write(b"\x95" * (4096 - 512))
            file.seek(SOURCE_SIZE - 512)
            file.write(b"\xe1" * 512)
            file.flush()
            os.fsync(file.fileno())
        source = directory / "source.qcow2"
        command([image, "convert", "-c", "-f", "raw", "-O", "qcow2", "-o",
                 "compression_type=zstd,cluster_size=1048576", raw, source], env, commands)
        # Normal writes, without -z or detect-zeroes, force allocated zero data.
        command([io, "-f", "qcow2", "-c", "write -P 0 16M 8M", source], env, commands)
        info = command([image, "info", "--output=json", source], env, commands, parsed=True)
        mapping = command([image, "map", "--output=json", source], env, commands, parsed=True)
        command([image, "check", source], env, commands)
        command([image, "compare", "-f", "raw", "-F", "qcow2", raw, source], env, commands)
        if info["virtual-size"] != SOURCE_SIZE or source.stat().st_size % 512:
            raise RuntimeError("Invalid source geometry")
        def coverage(start, end, predicate):
            matched = sum(max(0, min(end, item["start"] + item["length"]) - max(start, item["start"]))
                          for item in mapping if predicate(item))
            return matched == end - start
        if not coverage(16 * MIB, 24 * MIB,
                        lambda x: x.get("data") and x.get("present") and not x.get("zero") and not x.get("compressed")):
            raise RuntimeError("Allocated zero source region was not materialized")
        if not coverage(24 * MIB, 40 * MIB, lambda x: x.get("zero") and not x.get("data")):
            raise RuntimeError("Expected sparse hole not present")
        if not any(x.get("compressed") for x in mapping):
            raise RuntimeError("No compressed source clusters")
        sys.path.insert(0, str(HERE.parent / "image"))
        from root_image_mounts import patch_source as mount_fix
        from direct_restore import patch_source as direct_fix
        upstream = args.upstream / "configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py"
        phases = direct_fix(mount_fix(upstream.read_bytes()))
        if hashlib.sha256(phases).hexdigest() != PHASES_SHA:
            raise RuntimeError("Unexpected prepared phases source")
        (directory / "phases_impl.py").write_bytes(phases)
        for sector, enabled in CASES:
            with (directory / f"target-{sector}-{'on' if enabled else 'off'}.raw").open("xb") as file:
                file.truncate(TARGET_SIZE)
        logical = sum(item.stat().st_size for item in directory.iterdir() if item.suffix in {".raw", ".qcow2"})
        if logical > 1024 ** 3:
            raise RuntimeError("Added image storage exceeds 1 GiB")
        save(directory / "fixture-manifest.json", {
            "schema_version": 1, "purpose": "real block-device correctness; no performance claim",
            "source_sha256": sha(source), "source_bytes": source.stat().st_size,
            "virtual_size": SOURCE_SIZE, "expected_raw_sha256": sha(raw),
            "target_size": TARGET_SIZE, "prefill_byte": 167, "map": mapping, "info": info,
            "phases_sha256": PHASES_SHA, "upstream_source_sha256": sha(upstream),
            "host_qemu_img_sha256": sha(image), "host_qemu_io_sha256": sha(io),
            "added_disk_logical_bytes": logical,
            "recipe": {"nonzero_3d": [0, 8 * MIB], "repeating_0_to_255": [8 * MIB, 16 * MIB],
                       "allocated_zero": [16 * MIB, 24 * MIB], "sparse_hole": [24 * MIB, 40 * MIB],
                       "alternating_4096_6b_zero": [40 * MIB, 48 * MIB],
                       "unaligned_95": [63 * MIB + 512, 63 * MIB + 4096],
                       "last_512_e1": [SOURCE_SIZE - 512, SOURCE_SIZE]},
        })
    finally:
        save(directory / "prepare-commands.json", commands)
    print(json.dumps({"event": "fixture-ready", "directory": str(directory),
                      "source_bytes": source.stat().st_size, "added_disk_logical_bytes": logical}))


def run(args):
    directory = guarded_directory(args.directory)
    fixture = json.loads((directory / "fixture-manifest.json").read_text())
    if sha(directory / "source.qcow2") != fixture["source_sha256"]:
        raise RuntimeError("Source fixture changed")
    if sha(directory / "phases_impl.py") != PHASES_SHA:
        raise RuntimeError("Prepared source changed")
    run_dir = directory / "vm"
    if run_dir.exists():
        raise RuntimeError("VM run directory must be fresh")
    extra = ["-drive", f"file={args.supplemental},media=cdrom,if=none,format=raw,id=supplemental,readonly=on",
             "-device", "ide-cd,drive=supplemental,id=supplemental-cd,bus=ide.1"]
    for sector, enabled in CASES:
        suffix = f"{sector}-{'on' if enabled else 'off'}"
        path = directory / f"target-{suffix}.raw"
        if not path.is_file() or path.stat().st_size != TARGET_SIZE:
            raise RuntimeError("Target geometry changed")
        identifier = "dio" + suffix.replace("-", "")
        extra.extend(["-drive", f"file={path},if=none,id={identifier},format=raw,cache=writeback,"
                     f"discard={'unmap' if enabled else 'ignore'},detect-zeroes=off",
                     "-device", f"virtio-blk-pci,drive={identifier},serial=OMARCHY_DIO_{sector}_{'ON' if enabled else 'OFF'},"
                     f"logical_block_size={sector},physical_block_size={sector},"
                     f"write-zeroes={'on' if enabled else 'off'},discard={'on' if enabled else 'off'}" +
                     ("" if enabled else ",max-write-zeroes-sectors=0,max-discard-sectors=0")])
    extra.extend(["-drive", f"file={directory / 'source.qcow2'},if=none,id=source,format=raw,readonly=on",
                  "-device", "virtio-blk-pci,drive=source,serial=OMARCHY_DIO_SOURCE"])
    argv = [sys.executable, str(HERE.parent.parent / "iso-vm.py"), "run", "--mode", "builder",
            "--guest-user", "root", "--ssh-key", str(args.key), "--iso", str(args.iso),
            "--iso-source", str(args.iso_source), "--run-dir", str(run_dir),
            "--toolchain", str(args.toolchain), "--cpus", "4", "--memory", "8192",
            "--kernel", str(args.kernel), "--initrd", str(args.initrd), "--append", args.append,
            "--extra-qemu-args-json", json.dumps(extra), "--keep-running",
            "--timeout", str(args.timeout), "--ssh-port", str(args.ssh_port),
            "--qmp-port", str(args.qmp_port)]
    save(directory / "launch.json", {"argv": argv,
        "scope": "real block-device correctness only; no installation or performance claim",
        "added_test_disk_logical_bytes": fixture["added_disk_logical_bytes"],
        "unused_runner_target_virtual_bytes": 40 * 1024 ** 3,
        "unused_runner_target_note": "Proven install runner creates a sparse placeholder; test never writes it",
        "runner_sha256": sha(HERE.parent.parent / "iso-vm.py"),
        "host_harness_sha256": sha(Path(__file__).resolve())})
    os.execv(sys.executable, argv)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "run"):
        entry = sub.add_parser(action)
        entry.add_argument("--directory", type=Path, required=True)
        entry.add_argument("--toolchain", type=Path, required=True)
        if action == "prepare":
            entry.add_argument("--upstream", type=Path, required=True)
        else:
            for name in ("iso", "iso-source", "supplemental", "kernel", "initrd", "key"):
                entry.add_argument("--" + name, type=Path, required=True)
            entry.add_argument("--append", required=True)
            entry.add_argument("--ssh-port", type=int, default=24022)
            entry.add_argument("--qmp-port", type=int, default=24444)
            entry.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    (prepare if args.action == "prepare" else run)(args)


if __name__ == "__main__":
    main()
