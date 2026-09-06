#!/usr/bin/env python3
"""Exercise the prepared block matrix through the existing supervisor mailbox."""

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time


HERE = Path(__file__).resolve().parent
TAR_SHA = "52534a252d99fc157bf1febd1380c09633e1a39b30c94cbef60e5b9fcd6a949b"
BINARY_SHA = "634320b91165669917123e8e79cce1c4d00cee0a4aa4d662d7c0a8186479b3fb"
GUEST = "/tmp/omarchy-direct-restore"


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def request(directory, name, command, timeout=120):
    source = directory / "vm/requests" / (name + ".json")
    destination = directory / "vm/responses" / source.name
    if source.exists() or destination.exists():
        raise RuntimeError("Refusing duplicate mailbox request")
    save(source, {"action": "ssh", "command": command, "timeout": timeout})
    deadline = time.monotonic() + timeout + 30
    while not destination.exists():
        if time.monotonic() > deadline:
            raise RuntimeError(f"Mailbox timeout: {name}; preserve VM for diagnosis")
        time.sleep(1)
    response = json.loads(destination.read_text())
    if not response.get("ok") or response["result"]["returncode"]:
        raise RuntimeError(f"Guest command failed: {name}: {response}")
    print(json.dumps({"event": name, "returncode": 0}), flush=True)
    return response["result"]["stdout"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(Path("/tmp")) or directory == Path("/tmp"):
        raise ValueError("Fixture must be below /tmp")
    evidence = directory / "guest-evidence"
    evidence.mkdir(exist_ok=False)
    deadline = time.monotonic() + 600
    while True:
        manifest = directory / "vm/manifest.json"
        if manifest.exists():
            status = json.loads(manifest.read_text()).get("status")
            if status == "builder-ssh-ready":
                break
            if status == "failed":
                raise RuntimeError("Builder failed before SSH")
        if time.monotonic() > deadline:
            raise RuntimeError("Builder SSH deadline exceeded; retain VM evidence")
        time.sleep(2)
    payload = io.BytesIO()
    paths = [HERE / "guest-test.py", directory / "phases_impl.py", directory / "fixture-manifest.json"]
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.name)
    encoded = base64.b64encode(payload.getvalue()).decode()
    request(directory, "01-stage", "bash -euo pipefail -c " + shlex.quote(
        f"test ! -e {GUEST}; mkdir -m 700 {GUEST}; "
        f"printf %s {shlex.quote(encoded)} | base64 -d | tar -xzf - -C {GUEST}"))
    save(evidence / "staged-files.json", [
        {"name": path.name, "bytes": path.stat().st_size,
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths])
    # The fixed supplemental ISO is selected by its unique filesystem label. Verify
    # the only consumed member before extracting its relocatable /opt subtree.
    bootstrap = f"""set -euo pipefail
test "$(lsblk -ndo TYPE /dev/disk/by-label/OMARCHY_FAST_IMAGE)" = rom
test "$(blockdev --getro /dev/disk/by-label/OMARCHY_FAST_IMAGE)" = 1
mkdir {GUEST}/media
mount -o ro /dev/disk/by-label/OMARCHY_FAST_IMAGE {GUEST}/media
printf '%s  %s\\n' {TAR_SHA} {GUEST}/media/qemu-img-live.tar | sha256sum -c -
tar -xf {GUEST}/media/qemu-img-live.tar -C {GUEST} --strip-components=2 opt/omarchy-benchmark/qemu
printf '%s  %s\\n' {BINARY_SHA} {GUEST}/qemu/bin/qemu-img | sha256sum -c -
umount {GUEST}/media
rmdir {GUEST}/media
python3 - <<'PY'
import hashlib, json, pathlib, subprocess
p=pathlib.Path('{GUEST}')
files=[p/'qemu/qemu-img',p/'qemu/bin/qemu-img']
v={{'bundle_sha256':'{TAR_SHA}','files':[{{'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}} for f in files],
   'version':subprocess.check_output([str(files[0]),'--version'],text=True)}}
(p/'qemu-provenance.json').write_text(json.dumps(v,indent=2)+'\\n')
PY
"""
    request(directory, "02-portable-qemu", "bash -c " + shlex.quote(bootstrap))
    run_matrix(directory, evidence)


def run_matrix(directory, evidence):
    command = (f"cd {GUEST}; PATH={GUEST}/qemu:$PATH python3 guest-test.py "
               f"--work-dir {GUEST} --phases {GUEST}/phases_impl.py "
               f"--fixture-manifest {GUEST}/fixture-manifest.json > {GUEST}/test.log 2>&1")
    request(directory, "03-start-test", "systemd-run --unit=omarchy-direct-restore-test --property=Type=exec "
            "/bin/bash -c " + shlex.quote(command))
    deadline = time.monotonic() + 1200
    counter = 0
    while True:
        counter += 1
        output = request(directory, f"04-poll-{counter:03}",
            "systemctl show omarchy-direct-restore-test -p ActiveState -p SubState -p ExecMainStatus")
        states = dict(line.split("=", 1) for line in output.splitlines())
        if states.get("ActiveState") in {"inactive", "failed"}:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Guest matrix deadline exceeded; retain VM evidence")
        time.sleep(10)
    save(evidence / "unit-exit.json", states)
    collector = f"""import base64,json,pathlib
p=pathlib.Path('{GUEST}')
names=['results.json','qemu-provenance.json','test.log','readonly-error.json']+[f'case-{{s}}-{{z}}.json' for s in (512,4096) for z in ('on','off')]
print(json.dumps({{n:base64.b64encode((p/n).read_bytes()).decode() for n in names if (p/n).is_file()}}))
"""
    output = request(directory, "05-collect", "python3 -c " + shlex.quote(collector))
    for name, data in json.loads(output).items():
        if Path(name).name != name:
            raise RuntimeError("Unsafe result filename")
        (evidence / name).write_bytes(base64.b64decode(data, validate=True))
    results = json.loads((evidence / "results.json").read_text())
    print(json.dumps({"event": "matrix-collected", "status": results["status"],
                      "unit_exit": states, "evidence": str(evidence)}), flush=True)
    # Preserve failed devices without any retry or repair. Poweroff is manual
    # after review so a failure's original diagnostics remain available.
    if results["status"] != "passed" or states.get("ExecMainStatus") != "0":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
