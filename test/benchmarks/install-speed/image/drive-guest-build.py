#!/usr/bin/python3
"""Run and collect the real root-image build through iso-vm's SSH mailbox."""
import argparse
import base64
import io
import json
import shlex
import tarfile
import time
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("builder_run", type=Path)
parser.add_argument("baseline_run", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--timeout", type=float, default=14400)
args = parser.parse_args()
if args.output.exists():
    parser.error("output exists; use a fresh build evidence directory")
args.output.mkdir(parents=True)
deadline = time.monotonic() + args.timeout


def ssh(command, timeout=60):
    identifier = "image-build-" + uuid.uuid4().hex
    request_dir = args.builder_run / "requests"
    temporary = request_dir / (identifier + ".tmp")
    temporary.write_text(json.dumps({"action": "ssh", "command": command, "timeout": timeout}))
    temporary.rename(request_dir / (identifier + ".json"))
    response = args.builder_run / "responses" / (identifier + ".json")
    until = min(deadline, time.monotonic() + timeout + 60)
    while not response.exists():
        if time.monotonic() > until:
            raise TimeoutError(f"Supervisor did not answer {identifier}")
        time.sleep(1)
    result = json.loads(response.read_text())
    if not result.get("ok"):
        raise RuntimeError(result)
    command_result = result["result"]
    if command_result["returncode"]:
        raise RuntimeError(command_result)
    return command_result["stdout"]


commands = ["set -euo pipefail", "test ! -e /run/baseline-manifests", "mkdir /run/baseline-manifests"]
for name in ("package-manifest.txt", "package-explicit.txt"):
    data = (args.baseline_run / name).read_bytes()
    if not data.strip():
        parser.error(f"empty baseline input: {name}")
    (args.output / ("baseline-" + name)).write_bytes(data)
    encoded = base64.b64encode(data).decode()
    commands.append(f"printf %s {shlex.quote(encoded)} | base64 -d > /run/baseline-manifests/{name}")
commands.append("systemd-run --unit=omarchy-image-build --property=StandardOutput=append:/var/log/omarchy-image-build.log --property=StandardError=append:/var/log/omarchy-image-build.log /bin/bash /usr/local/lib/omarchy-benchmark/image-builder/build-root-from-iso.sh /usr/local/lib/omarchy-benchmark/image-builder /run/baseline-manifests /run/image-build-output")
ssh("\n".join(commands))
started = time.monotonic()
print(json.dumps({"event": "build-started", "builder_run": str(args.builder_run)}), flush=True)
success = False
while time.monotonic() < deadline:
    status_text = ssh("systemctl show omarchy-image-build.service -p ActiveState -p Result -p ExecMainStatus")
    status = dict(line.split("=", 1) for line in status_text.splitlines() if "=" in line)
    status["host_elapsed_s"] = time.monotonic() - started
    (args.output / "build-progress.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status), flush=True)
    if status.get("ActiveState") in {"inactive", "failed"}:
        success = status.get("Result") == "success" and status.get("ExecMainStatus") == "0"
        break
    time.sleep(15)
log = ssh("cat /var/log/omarchy-image-build.log")
(args.output / "guest-build.log").write_text(log)
if not success:
    raise SystemExit("Guest build did not succeed; inspect preserved status/log. Do not compress the raw disk.")
archive = base64.b64decode(ssh("tar -C /run/image-build-output -czf - . | base64 -w0"))
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
    for member in source:
        if member.isdir():
            continue
        name = member.name.removeprefix("./")
        if not member.isfile() or "/" in name or name in {"", ".", ".."}:
            raise RuntimeError(f"Unexpected guest build output: {member.name}")
        stream = source.extractfile(member)
        if stream is None:
            raise RuntimeError(f"Missing guest build output: {name}")
        (args.output / name).write_bytes(stream.read())
if (args.output / "build-status.txt").read_text().strip() != "BUILD_COMPLETE":
    raise SystemExit("Guest unit exited without validated build completion")
(args.output / "build-run.json").write_text(json.dumps({"schema_version": 1, "builder_run": str(args.builder_run), "baseline_run": str(args.baseline_run), "status": "complete", "host_build_seconds": time.monotonic() - started}, indent=2) + "\n")
print(json.dumps({"event": "build-complete", "output": str(args.output), "next": "shut down builder VM, then run native compression"}), flush=True)
