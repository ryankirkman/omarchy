#!/usr/bin/python3
"""Bundle a trusted native x86-64 qemu-img with its exact loader and libraries.

This live-only benchmark dependency is not installed into the target system.
Run only on the already-verified host QEMU package, because ldd inspects code.
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("qemu_img", type=Path)
parser.add_argument("destination", type=Path)
parser.add_argument("--library-path", default="")
args = parser.parse_args()
if args.destination.exists():
    parser.error("destination exists")
env = dict(os.environ)
if args.library_path:
    env["LD_LIBRARY_PATH"] = args.library_path
dependencies = subprocess.check_output(["ldd", str(args.qemu_img)], env=env, text=True)
if "not found" in dependencies:
    parser.error(dependencies)
files = {"bin/qemu-img": args.qemu_img}
loader = None
for line in dependencies.splitlines():
    if match := re.match(r"\s*(\S+) => (/\S+) \(", line):
        files["lib/" + match[1]] = Path(match[2])
    elif match := re.match(r"\s*(/\S+) \(", line):
        source = Path(match[1])
        if "ld-linux" in source.name:
            loader = source.name
            files["lib/" + source.name] = source
if not loader:
    parser.error("could not identify the ELF interpreter")
prefix = "opt/omarchy-benchmark/qemu"
wrapper = f'''#!/bin/bash
set -euo pipefail
qemu_root=$(dirname "$(readlink -f "$0")")
export QEMU_MODULE_DIR="$qemu_root/modules"
exec "$qemu_root/lib/{loader}" --library-path "$qemu_root/lib" "$qemu_root/bin/qemu-img" "$@"
'''.encode()
entries = []
with tempfile.TemporaryDirectory(prefix="omarchy-qemu-bundle-") as temporary:
    root = Path(temporary)
    for name, source in files.items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o755)
    (root / "qemu-img").write_bytes(wrapper)
    (root / "qemu-img").chmod(0o755)
    # Resolve through only the bundled loader/libraries and exercise zstd qcow2.
    isolated_env = {"PATH": "/usr/bin:/bin"}
    version = subprocess.check_output([str(root / "qemu-img"), "--version"], env=isolated_env, text=True).splitlines()[0]
    raw = root / "check.raw"
    # Repeated entropy forces an actually compressed cluster; uniformly random
    # data can silently fall back to an uncompressed qcow2 cluster.
    raw.write_bytes(os.urandom(64 * 1024) * 16)
    qcow = root / "check.qcow2"
    validation = []
    for command in (("convert", "-c", "-f", "raw", "-O", "qcow2", "-o", "compression_type=zstd,cluster_size=1048576", str(raw), str(qcow)), ("check", str(qcow)), ("compare", "-f", "raw", "-F", "qcow2", str(raw), str(qcow))):
        result = subprocess.run([str(root / "qemu-img"), *command], env=isolated_env, check=True, capture_output=True, text=True)
        validation.append({"operation": command[0], "stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode})
        print(result.stdout, end="")
        if command[0] == "check" and "100.00% compressed clusters" not in result.stdout:
            parser.error("smoke fixture did not exercise an actually compressed qcow2 cluster")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.destination, "w") as archive:
        for name, source in sorted(files.items()):
            data = source.read_bytes()
            info = tarfile.TarInfo(prefix + "/" + name)
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
            entries.append({"path": info.name, "source": str(source), "sha256": hashlib.sha256(data).hexdigest()})
        info = tarfile.TarInfo(prefix + "/qemu-img")
        info.size, info.mode = len(wrapper), 0o755
        archive.addfile(info, io.BytesIO(wrapper))
        info = tarfile.TarInfo("usr/local/bin/qemu-img")
        info.type, info.linkname, info.mode = tarfile.SYMTYPE, "/" + prefix + "/qemu-img", 0o777
        archive.addfile(info)
digest = hashlib.sha256(args.destination.read_bytes()).hexdigest()
args.destination.with_suffix(".tar.sha256").write_text(f"{digest}  {args.destination.name}\n")
args.destination.with_suffix(".manifest.json").write_text(json.dumps({"qemu_version": version, "files": entries, "sha256": digest, "validation_environment": "isolated loader and bundled libraries; repeated entropy fixture", "validation": validation}, indent=2) + "\n")
print(f"Prepared live-only {version}: {args.destination}")
