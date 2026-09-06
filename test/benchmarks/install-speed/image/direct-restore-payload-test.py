#!/usr/bin/env python3
"""Check direct-restore payload activation without host writes or block devices."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import direct_restore
import root_image_mounts


HERE = Path(__file__).resolve().parent


def module(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  result = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(result)
  return result


payload = module("direct_restore_payload", HERE / "direct-restore-payload.py")
fast_reboot = module("fast_reboot_payload", HERE.parent / "fast-reboot/prepare-payload.py")


def digest(data):
  return hashlib.sha256(data).hexdigest()


def files(root):
  return {str(path.relative_to(root)): {"sha256": digest(path.read_bytes()),
    "mode": oct(stat.S_IMODE(path.stat().st_mode))}
    for path in root.rglob("*") if path.is_file()}


def replace_once(source, old, new):
  if source.count(old) != 1:
    raise AssertionError(f"Sandbox expects one literal assignment: {old}")
  return source.replace(old, new)


def refresh_checksums(directory):
  manifest = directory / "payload.sha256"
  names = [line.split("  ", 1)[1] for line in manifest.read_text().splitlines()]
  assert all(Path(name).name == name for name in names), names
  manifest.write_text("".join(f"{digest((directory / name).read_bytes())}  {name}\n" for name in names))


class DirectRestorePayloadContract(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.temporary = tempfile.TemporaryDirectory(prefix="omarchy-direct-payload-", dir="/tmp")
    cls.addClassCleanup(cls.temporary.cleanup)
    cls.work = Path(cls.temporary.name)
    ordinary = cls.work / "ordinary"
    (ordinary / "nested").mkdir(parents=True)
    (ordinary / "nested/ordinary-payload").write_text("unchanged ordinary payload\n")
    (ordinary / "nested/ordinary-payload").chmod(0o640)
    (ordinary / "executable").write_text("#!/bin/bash\nexit 0\n")
    (ordinary / "executable").chmod(0o755)
    cls.base = cls.work / "fast-reboot"
    fast_reboot.prepare(ISO_SOURCE, ordinary, cls.base)
    cls.before = files(cls.base)
    cls.output = cls.work / "direct"
    cls.manifest = payload.prepare(ISO_SOURCE, cls.base, cls.output)
    cls.staged = cls.output / payload.PAYLOAD_PATH
    cls.upstream = subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show",
      f"{payload.PIN}:configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py"])
    cls.prepared = root_image_mounts.patch_source(cls.upstream)
    cls.direct = direct_restore.patch_source(cls.prepared)

  def test_pinned_sources_base_preservation_and_complete_manifest(self):
    self.assertEqual(payload.PIN, direct_restore.UPSTREAM_COMMIT)
    self.assertEqual(digest(self.prepared), payload.PREPARED_SHA256)
    self.assertEqual(digest(self.direct), payload.DIRECT_SHA256)
    self.assertEqual((self.staged / "phases_impl.py").read_bytes(), self.direct)
    self.assertEqual(stat.S_IMODE((self.staged / "phases_impl.py").stat().st_mode), 0o644)
    self.assertEqual((self.staged / "fast-reboot-preflight.sh").read_bytes(),
      (HERE.parent / "fast-reboot/candidate-preflight.sh").read_bytes())
    license_data = subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show", f"{payload.PIN}:LICENSE"])
    self.assertEqual((self.staged / "LICENSE").read_bytes(), license_data)
    after = files(self.output)
    self.assertEqual({name: after[name] for name in self.before}, self.before)
    self.assertEqual(files(self.base), self.before)
    entries = self.manifest["files"]
    self.assertEqual(len(entries), len({entry["path"] for entry in entries}))
    self.assertEqual({entry["path"]: {key: entry[key] for key in ("sha256", "mode")}
      for entry in entries}, after)
    self.assertEqual(self.manifest["upstream_commit"], payload.PIN)
    self.assertEqual(self.manifest["upstream_source_sha256"], digest(self.upstream))
    self.assertEqual(self.manifest["ordinary_phases_sha256"], digest(self.prepared))
    self.assertEqual(self.manifest["direct_phases_sha256"], digest(self.direct))
    self.assertEqual(self.manifest["target_cache"], "none")
    self.assertFalse(self.manifest["supplemental_image_changed"])
    self.assertEqual(self.manifest["base_payload_manifest_sha256"],
      digest(self.base.with_name(self.base.name + ".manifest.json").read_bytes()))
    self.assertEqual(self.manifest["preflight_sha256"],
      digest((HERE / "direct-restore-preflight.sh").read_bytes()))
    self.assertEqual(json.loads(self.output.with_name(self.output.name + ".manifest.json").read_text()),
      self.manifest)
    subprocess.run(["sha256sum", "--check", "--strict", "payload.sha256"], cwd=self.staged,
      check=True, capture_output=True, text=True)
    for script in (self.staged / "fast-reboot-preflight.sh", HERE / "direct-restore-preflight.sh"):
      subprocess.run(["bash", "-n", str(script)], check=True)

  def test_existing_output_and_manifest_are_not_overwritten(self):
    with self.assertRaises(ValueError):
      payload.prepare(ISO_SOURCE, self.base, self.output)
    output = self.work / "manifest-collision"
    manifest = output.with_name(output.name + ".manifest.json")
    manifest.write_text("must remain unchanged\n")
    with self.assertRaises(ValueError):
      payload.prepare(ISO_SOURCE, self.base, output)
    self.assertEqual(manifest.read_text(), "must remain unchanged\n")
    self.assertFalse(output.exists())

  def test_changed_fast_reboot_base_is_rejected_before_output(self):
    base = self.work / "changed-base"
    shutil.copytree(self.base, base)
    shutil.copy2(self.base.with_name(self.base.name + ".manifest.json"),
      base.with_name(base.name + ".manifest.json"))
    with (base / fast_reboot.PAYLOAD_PATH / "omarchy-release-install-target").open("ab") as changed:
      changed.write(b"# changed after base provenance was recorded\n")
    output = self.work / "changed-base-output"
    with self.assertRaises(ValueError):
      payload.prepare(ISO_SOURCE, base, output)
    self.assertFalse(output.exists())
    self.assertFalse(output.with_name(output.name + ".manifest.json").exists())

  def activate(self, *, corruption=False, drift=False, ordinary_status=0, install_corruption=False):
    # Copy the real staged payload for each case. Only absolute destinations
    # and the ordinary image activation are sandboxed; both outer preflights
    # retain their actual checksum, shell failure and ordering behavior.
    box = Path(tempfile.mkdtemp(prefix="activation-", dir=self.work))
    output = box / "payload"
    shutil.copytree(self.output, output)
    staged = output / payload.PAYLOAD_PATH
    fast = output / fast_reboot.PAYLOAD_PATH
    binary = box / "bin"
    binary.mkdir()
    live = box / "live"
    live.mkdir()
    (box / "calls").write_text("")
    (box / "prepared.py").write_bytes(self.prepared)
    # Start with the direct file present: ordinary activation deliberately
    # replaces it, so activating too early cannot satisfy the final assertion.
    (live / "phases_impl.py").write_bytes(self.direct)
    (fast / "image-candidate-preflight.sh").write_text('''#!/bin/bash
set -euo pipefail
echo ordinary-image-activation >>"$BOX/calls"
/usr/bin/cp "$BOX/prepared.py" "$BOX/live/phases_impl.py"
echo ordinary >"$BOX/live/omarchy-release-install-target"
echo ordinary >"$BOX/live/omarchy-install-dashboard"
if [[ $DRIFT == 1 ]]; then echo '# unexpected source' >>"$BOX/live/phases_impl.py"; fi
exit "$ORDINARY_STATUS"
''')
    refresh_checksums(fast)
    fast_script = staged / "fast-reboot-preflight.sh"
    fast_script.write_text(replace_once(fast_script.read_text(),
      "payload=/usr/local/lib/omarchy-benchmark/fast-reboot", "payload=" + shlex.quote(str(fast))))
    refresh_checksums(staged)
    if corruption:
      with (staged / "phases_impl.py").open("ab") as changed:
        changed.write(b"# corrupt payload\n")

    # Strict allowlists ensure every possible install destination remains a
    # regular file beneath this case's temporary directory. No privileged
    # image activation, mounting, QEMU or device operation is executed.
    stubs = {
      "install": '''[[ $# == 4 && $1 == -m ]] || exit 91
case "$4" in
  /usr/local/bin/omarchy-release-install-target|/usr/local/bin/omarchy-install-dashboard)
    [[ $2 == 0755 ]] || exit 92
    destination="$BOX/live/${4##*/}" ;;
  "$BOX/live/phases_impl.py")
    [[ $2 == 0644 ]] || exit 93
    destination="$4" ;;
  *) echo "Rejected unsandboxed install destination" >&2; exit 94 ;;
esac
echo "install ${destination##*/}" >>"$BOX/calls"
/usr/bin/install -m "$2" "$3" "$destination"
if [[ $INSTALL_CORRUPTION == 1 && $destination == "$BOX/live/phases_impl.py" ]]; then
  echo '# corrupted installation' >>"$destination"
fi
''',
      "cmp": '''[[ $# == 2 ]] || exit 95
case "$2" in
  /usr/local/bin/omarchy-release-install-target|/usr/local/bin/omarchy-install-dashboard)
    destination="$BOX/live/${2##*/}" ;;
  "$BOX/live/phases_impl.py") destination="$2" ;;
  *) echo "Rejected unsandboxed comparison" >&2; exit 96 ;;
esac
exec /usr/bin/cmp "$1" "$destination"
''',
    }
    for command, body in stubs.items():
      script = binary / command
      script.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
      script.chmod(0o755)
    preflight = replace_once((HERE / "direct-restore-preflight.sh").read_text(),
      "payload=/usr/local/lib/omarchy-benchmark/direct-restore", "payload=" + shlex.quote(str(staged)))
    preflight = replace_once(preflight, "live=/usr/share/omarchy-iso/orchestrator/phases_impl.py",
      "live=" + shlex.quote(str(live / "phases_impl.py")))
    environment = dict(os.environ, BOX=str(box), PATH=str(binary) + ":/usr/bin:/bin",
      DRIFT=str(int(drift)), ORDINARY_STATUS=str(ordinary_status),
      INSTALL_CORRUPTION=str(int(install_corruption)))
    result = subprocess.run(["bash"], input=preflight, text=True, env=environment,
      capture_output=True, timeout=15)
    return result, (box / "calls").read_text().splitlines(), live

  def test_direct_activation_survives_ordinary_overlay_overwrite(self):
    result, calls, live = self.activate()
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(calls, ["ordinary-image-activation", "install omarchy-release-install-target",
      "install omarchy-install-dashboard", "install phases_impl.py"])
    self.assertEqual((live / "phases_impl.py").read_bytes(), self.direct)
    self.assertEqual(stat.S_IMODE((live / "phases_impl.py").stat().st_mode), 0o644)
    for name in ("omarchy-release-install-target", "omarchy-install-dashboard"):
      self.assertEqual((live / name).read_bytes(), (self.base / fast_reboot.PAYLOAD_PATH / name).read_bytes())

  def test_corrupt_payload_aborts_before_ordinary_activation(self):
    result, calls, live = self.activate(corruption=True)
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(calls, [])
    self.assertEqual((live / "phases_impl.py").read_bytes(), self.direct)

  def test_live_source_drift_aborts_before_direct_install(self):
    result, calls, live = self.activate(drift=True)
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(calls, ["ordinary-image-activation", "install omarchy-release-install-target",
      "install omarchy-install-dashboard"])
    self.assertEqual((live / "phases_impl.py").read_bytes(), self.prepared + b"# unexpected source\n")

  def test_failed_ordinary_activation_is_not_masked(self):
    result, calls, live = self.activate(ordinary_status=37)
    self.assertEqual(result.returncode, 37)
    self.assertEqual(calls, ["ordinary-image-activation"])
    self.assertEqual((live / "phases_impl.py").read_bytes(), self.prepared)

  def test_corruption_during_install_fails_final_verification(self):
    result, calls, live = self.activate(install_corruption=True)
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(calls[-1], "install phases_impl.py")
    self.assertNotEqual(digest((live / "phases_impl.py").read_bytes()), payload.DIRECT_SHA256)


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  arguments, remaining = parser.parse_known_args()
  ISO_SOURCE = arguments.iso_source.resolve()
  unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
