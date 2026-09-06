#!/usr/bin/env python3
"""Exercise pinned dashboard release decisions without disks or real reboots."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fast_reboot_payload", HERE / "prepare-payload.py")
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


def function(source, name):
  found = re.search(r"(?ms)^" + re.escape(name) + r"\(\) \{\n.*?^\}", source)
  if not found:
    raise AssertionError(f"Missing pinned dashboard function: {name}")
  return found.group()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  args = parser.parse_args()
  original = payload.read_sources(args.iso_source)
  with tempfile.TemporaryDirectory(prefix="omarchy-fast-reboot-contract-") as temporary:
    root = Path(temporary)
    base = root / "base"
    base.mkdir()
    (base / "ordinary-payload").write_text("unchanged\n")
    output = root / "payload"
    manifest = payload.prepare(args.iso_source, base, output)
    staged = output / payload.PAYLOAD_PATH
    assert (output / "ordinary-payload").read_text() == "unchanged\n"
    assert (staged / "omarchy-install-dashboard").read_bytes() == original["omarchy-install-dashboard"]
    assert (staged / "LICENSE").read_bytes() == original["LICENSE"]
    guarded = (staged / "omarchy-release-install-target").read_bytes()
    assert guarded.replace(b"\nsync || exit 1\n", b"\nsync\n") == original["omarchy-release-install-target"]
    assert guarded.count(b"\nsync || exit 1\n") == 2
    assert manifest["upstream_commit"] == payload.PIN and not manifest["host_reset_used"]
    try:
      payload.prepare(args.iso_source, base, output)
    except ValueError:
      pass
    else:
      raise AssertionError("Existing payload was overwritten")
    try:
      payload.guard_sync(b"#!/bin/bash\nsync\n")
    except ValueError:
      pass
    else:
      raise AssertionError("Unexpected upstream source shape was silently patched")
    for name in ("omarchy-install-dashboard", "omarchy-release-install-target", "image-candidate-preflight.sh"):
      subprocess.run(["bash", "-n", staged / name], check=True)
    subprocess.run(["bash", "-n", HERE / "candidate-preflight.sh"], check=True)

    # These stubs are the only device/shutdown commands that can be reached.
    # The actual guarded upstream release helper and actual dashboard decision
    # functions execute against them, preserving shell status/ordering behavior.
    stubs = {
      "sync": '''echo sync >>"$BOX/calls"
count=$(cat "$BOX/sync-count"); count=$((count + 1)); echo "$count" >"$BOX/sync-count"
[[ $count != ${SYNC_FAIL_AT:-0} ]]''',
      "jq": '''printf '%s\\n' "$TARGET"''',
      "tail": '''if [[ ${*: -1} == /proc/swaps ]]; then
  printf '%s file 1024 0 -2\\n/dev/zram0 partition 1024 0 100\\n' "$TARGET/swap/swapfile"
else exec /usr/bin/tail "$@"; fi''',
      "swapoff": '''echo "swapoff $*" >>"$BOX/calls"''',
      "findmnt": '''echo "findmnt $*" >>"$BOX/calls"
if [[ $* == *"-o SOURCE"* && ${ENCRYPTED:-0} == 1 ]]; then echo '/dev/mapper/omarchy_root[/@]'; fi
exit 0''',
      "mountpoint": '''exit 0''',
      "umount": '''echo "umount $*" >>"$BOX/calls"; exit "${UMOUNT_RC:-0}"''',
      "dmsetup": '''echo "dmsetup $*" >>"$BOX/calls"
if [[ ${DM_BROKEN:-0} == 1 ]]; then echo 'Cannot query device mapper' >&2; exit 1; fi
if [[ -f $BOX/closed ]]; then echo 'Device does not exist.' >&2; exit 1; fi
echo CRYPT-LUKS2-test-omarchy_root''',
      "cryptsetup": '''echo "cryptsetup $*" >>"$BOX/calls"
if [[ ${CLOSE_RC:-0} == 0 ]]; then touch "$BOX/closed"; fi
exit "${CLOSE_RC:-0}"''',
      "fuser": '''echo 'test holder diagnostic' ''',
      "systemctl": '''echo "systemctl $*" >>"$BOX/calls"; exit "${SYSTEMCTL_RC:-0}"''',
      "reboot": '''echo "reboot $*" >>"$BOX/calls"; exit 0''',
    }
    dashboard = original["omarchy-install-dashboard"].decode()
    decision = "\n".join(function(dashboard, name) for name in ("release_target", "reboot_now"))
    script = '''set -euo pipefail
STATE_FILE="$BOX/state.json"
dashboard_log() { printf 'log %s\\n' "$*" >>"$BOX/calls"; }
TARGET_RELEASED=yes
''' + decision + '''
release_target || true
printf '%s\\n' "$TARGET_RELEASED" >"$BOX/released"
reboot_now
'''

    def exercise(name, *, immediate, **settings):
      box = root / name
      binary = box / "bin"
      binary.mkdir(parents=True)
      (box / "calls").write_text("")
      (box / "sync-count").write_text("0\n")
      (box / "state.json").write_text(json.dumps({"target": str(box / "target")}))
      for command, body in stubs.items():
        target = binary / command
        target.write_text("#!/bin/bash\n" + body + "\n")
        target.chmod(0o755)
      (binary / "omarchy-release-install-target").symlink_to(staged / "omarchy-release-install-target")
      env = dict(os.environ, BOX=str(box), TARGET=str(box / "target"), PATH=str(binary) + ":/usr/bin:/bin",
                 **{key: str(value) for key, value in settings.items()})
      subprocess.run(["bash"], input=script, env=env, text=True, check=True, capture_output=True, timeout=15)
      calls = (box / "calls").read_text().splitlines()
      assert (box / "released").read_text().strip() == ("yes" if immediate else "no"), (name, calls)
      reboot_calls = [line for line in calls if line.startswith(("systemctl ", "reboot "))]
      expected = ["systemctl reboot -ff"] if immediate else ["systemctl reboot"]
      if settings.get("SYSTEMCTL_RC"):
        expected += ["reboot -f" if immediate else "reboot "]
      assert reboot_calls == expected, (name, reboot_calls)
      if immediate:
        assert calls.count("sync") == 2, calls
        first, last = [i for i, line in enumerate(calls) if line == "sync"]
        swap = next(i for i, line in enumerate(calls) if line.startswith("swapoff "))
        unmount = next(i for i, line in enumerate(calls) if line.startswith("umount "))
        reboot = next(i for i, line in enumerate(calls) if line.startswith("systemctl "))
        assert first < swap < unmount < last < reboot, calls
        assert not any(line.startswith("swapoff /dev/zram0") for line in calls)
        if settings.get("ENCRYPTED"):
          close = next(i for i, line in enumerate(calls) if line.startswith("cryptsetup close"))
          assert unmount < close < last
      else:
        assert not any("-f" in line for line in reboot_calls), calls
      print("ok -", name)

    exercise("successful-release", immediate=True)
    exercise("successful-encrypted-release", immediate=True, ENCRYPTED=1)
    exercise("first-sync-failure-falls-back", immediate=False, SYNC_FAIL_AT=1)
    exercise("last-sync-failure-falls-back", immediate=False, SYNC_FAIL_AT=2)
    exercise("unmount-failure-falls-back", immediate=False, UMOUNT_RC=1)
    exercise("mapper-close-failure-falls-back", immediate=False, ENCRYPTED=1, CLOSE_RC=1)
    exercise("unanswerable-mapper-falls-back", immediate=False, ENCRYPTED=1, DM_BROKEN=1)
    exercise("immediate-systemctl-fallback", immediate=True, SYSTEMCTL_RC=1)
    exercise("graceful-systemctl-fallback", immediate=False, SYNC_FAIL_AT=1, SYSTEMCTL_RC=1)

    # Exercise the actual preflight's ordering with normal image activation
    # replaced by a sandbox stub that deliberately restores stock files first.
    # No install destination can resolve outside this temporary directory.
    activation = root / "activation"
    (activation / "bin").mkdir(parents=True)
    (activation / "live").mkdir()
    (activation / "calls").write_text("")
    (staged / "image-candidate-preflight.sh").write_text('''#!/bin/bash
echo ordinary-image-activation >>"$BOX/calls"
echo stock >"$BOX/live/omarchy-release-install-target"
echo stock >"$BOX/live/omarchy-install-dashboard"
''')
    (staged / "payload.sha256").write_text("".join(
      f"{payload.digest((staged / name).read_bytes())}  {name}\n"
      for name in ("LICENSE", "image-candidate-preflight.sh", *payload.SOURCE_SHA256)))
    for command, body in {
      "install": '''[[ $# == 4 && $1 == -m && $2 == 0755 ]] || exit 9
case "$4" in /usr/local/bin/omarchy-release-install-target|/usr/local/bin/omarchy-install-dashboard) ;; *) exit 9 ;; esac
echo "install ${4##*/}" >>"$BOX/calls"
exec /usr/bin/install -m "$2" "$3" "$BOX/live/${4##*/}"''',
      "cmp": '''case "$2" in /usr/local/bin/omarchy-release-install-target|/usr/local/bin/omarchy-install-dashboard) ;; *) exit 9 ;; esac
exec /usr/bin/cmp "$1" "$BOX/live/${2##*/}"''',
    }.items():
      path = activation / "bin" / command
      path.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
      path.chmod(0o755)
    preflight = (HERE / "candidate-preflight.sh").read_text().replace(
      "payload=/usr/local/lib/omarchy-benchmark/fast-reboot", "payload=" + shlex.quote(str(staged)))
    env = dict(os.environ, BOX=str(activation), PATH=str(activation / "bin") + ":/usr/bin:/bin")
    subprocess.run(["bash"], input=preflight, text=True, env=env, check=True, capture_output=True, timeout=15)
    assert (activation / "calls").read_text().splitlines() == [
      "ordinary-image-activation", "install omarchy-release-install-target", "install omarchy-install-dashboard"]
    assert (activation / "live/omarchy-release-install-target").read_bytes() == guarded
    (activation / "calls").write_text("")
    with (staged / "omarchy-release-install-target").open("ab") as changed:
      changed.write(b"# corrupted payload\n")
    result = subprocess.run(["bash"], input=preflight, text=True, env=env, capture_output=True, timeout=15)
    assert result.returncode != 0 and (activation / "calls").read_text() == ""
    print("ok - variant activates after normal overlay, and corrupt payload aborts before activation")
  print("ok - pinned payload provenance, release ordering and all reboot fallbacks; no real reboot executed")


if __name__ == "__main__":
  main()
