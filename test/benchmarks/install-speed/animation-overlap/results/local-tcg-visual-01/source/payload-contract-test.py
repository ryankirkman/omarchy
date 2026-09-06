#!/usr/bin/env python3
"""Exercise composable activation and corruption gates inside temporary paths."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from dashboard_patch import PIN, SOURCE_PATH, SOURCE_SHA256, patch_source


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("animation_payload", HERE / "prepare-payload.py")
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


def digest(data):
  return hashlib.sha256(data).hexdigest()


class PayloadContract(unittest.TestCase):
  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory(prefix="omarchy-animation-payload-", dir="/tmp")
    self.addCleanup(self.temporary.cleanup)
    self.work = Path(self.temporary.name)
    self.original = subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show", f"{PIN}:{SOURCE_PATH}"])
    self.base = self.work / "base"
    (self.base / "arbitrary-phases").mkdir(parents=True)
    (self.base / "arbitrary-phases/phases_impl.py").write_text("# independent phase patch must remain untouched\n")
    (self.base / "arbitrary-phases/phases_impl.py").chmod(0o640)
    (self.base / "inherited-executable").write_text("#!/bin/bash\nexit 0\n")
    (self.base / "inherited-executable").chmod(0o755)
    self.base_preflight = self.work / "base-preflight.sh"
    self.base_preflight.write_text('''#!/bin/bash
set -euo pipefail
printf 'base\\n' >>"$BOX/calls"
[[ ${BASE_FAIL:-0} == 0 ]] || exit 19
cp "$BOX/original-dashboard" "$BOX/live-dashboard"
if [[ ${WRONG_LIVE:-0} == 1 ]]; then printf '# unexpected\\n' >>"$BOX/live-dashboard"; fi
''')
    self.base_preflight.chmod(0o755)
    self.before = payload.inventory(self.base)
    self.base_manifest = self.base.with_name(self.base.name + ".manifest.json")
    self.base_manifest.write_text(json.dumps({"upstream_commit": PIN, "variant": "independent-prior-phase-patch",
      "preflight_sha256": digest(self.base_preflight.read_bytes()), "files": self.before}))
    self.output = self.work / "output"

  def prepare(self):
    self.manifest = payload.prepare(ISO_SOURCE, self.base, self.base_preflight, self.output)
    return self.output / payload.PAYLOAD_PATH

  def test_pins_and_complete_composition(self):
    staged = self.prepare()
    self.assertEqual(digest(self.original), SOURCE_SHA256)
    self.assertEqual((staged / "omarchy-install-dashboard").read_bytes(), patch_source(self.original))
    self.assertEqual(payload.inventory(self.base), self.before)
    inherited = {row["path"]: row for row in payload.inventory(self.output)}
    self.assertEqual([inherited[row["path"]] for row in self.before], self.before)
    self.assertEqual(self.manifest["files"], payload.inventory(self.output))
    self.assertFalse(self.manifest["installer_phases_changed"])
    self.assertEqual(self.manifest["base_preflight_sha256"], digest(self.base_preflight.read_bytes()))
    self.assertEqual(self.manifest["base_payload_manifest_sha256"], digest(self.base_manifest.read_bytes()))
    with self.assertRaises(ValueError):
      payload.prepare(ISO_SOURCE, self.base, self.base_preflight, self.output)
    with self.assertRaises(ValueError):
      patch_source(self.original + b"\n")

  def test_changed_inherited_file_and_preflight_are_rejected(self):
    (self.base / "arbitrary-phases/phases_impl.py").chmod(0o644)
    with self.assertRaises(ValueError):
      self.prepare()
    (self.base / "arbitrary-phases/phases_impl.py").chmod(0o640)
    self.base_preflight.write_text(self.base_preflight.read_text() + "# changed\n")
    with self.assertRaises(ValueError):
      self.prepare()
    self.assertFalse(self.output.exists())

  def test_inherited_symlink_is_rejected(self):
    (self.base / "symlink").symlink_to(self.base / "inherited-executable")
    with self.assertRaises(ValueError):
      self.prepare()

  def exercise(self, name, *, corrupt=False, **settings):
    staged = self.output / payload.PAYLOAD_PATH
    box = self.work / name
    box.mkdir()
    sandbox_payload = box / "payload"
    shutil.copytree(staged, sandbox_payload)
    (box / "original-dashboard").write_bytes(self.original)
    (box / "live-dashboard").write_bytes(b"untouched before base activation\n")
    if corrupt:
      (sandbox_payload / "omarchy-install-dashboard").write_bytes(b"corrupt payload\n")
    source = (HERE / "preflight.sh").read_text()
    replacements = {
      "payload=/usr/local/lib/omarchy-benchmark/animation-overlap": f"payload={sandbox_payload}",
      "live=/usr/local/bin/omarchy-install-dashboard": f"live={box / 'live-dashboard'}",
    }
    for old, new in replacements.items():
      self.assertEqual(source.count(old), 1)
      source = source.replace(old, new)
    result = subprocess.run(["bash"], input=source, text=True, capture_output=True,
      env={**os.environ, "BOX": str(box), **{key: str(value) for key, value in settings.items()}}, timeout=10)
    calls = (box / "calls").read_text().splitlines() if (box / "calls").exists() else []
    return result, calls, (box / "live-dashboard").read_bytes()

  def test_real_preflight_order_and_fail_closed_boundaries(self):
    staged = self.prepare()
    result, calls, installed = self.exercise("success")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(calls, ["base"])
    self.assertEqual(installed, (staged / "omarchy-install-dashboard").read_bytes())
    result, calls, installed = self.exercise("corrupt", corrupt=True)
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(calls, [])
    self.assertEqual(installed, b"untouched before base activation\n")
    result, calls, installed = self.exercise("base-failure", BASE_FAIL=1)
    self.assertEqual(result.returncode, 19)
    self.assertEqual(calls, ["base"])
    self.assertEqual(installed, b"untouched before base activation\n")
    result, calls, installed = self.exercise("wrong-live-source", WRONG_LIVE=1)
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(calls, ["base"])
    self.assertNotEqual(installed, (staged / "omarchy-install-dashboard").read_bytes())


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  args = parser.parse_args()
  ISO_SOURCE = args.iso_source.resolve()
  unittest.main(argv=[__file__])
