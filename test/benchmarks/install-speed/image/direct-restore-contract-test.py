#!/usr/bin/env python3
"""Exercise the pinned restore function and opt-in bundle compatibility."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import direct_restore
import root_image_mounts


LOCAL = Path(__file__).resolve().parent
PHASES = "usr/share/omarchy-iso/orchestrator/phases_impl.py"
# Captured before this option was added, using prepare-bundles.py Git blob
# 992a49ad83ebf685a75b69ecbda9af22923adcb5 and the pinned clean ISO checkout.
DEFAULT_DIGESTS = {
  "builder-bundle.manifest.json": "bdd909b40f3c3d3839ac8415606767ea42052989fed7728df7a6743b961b7a4f",
  "builder-bundle.tar": "2efbebcb5e5c9ac35eba79656c77f75376f4d2d04c226a817d0b9730840ee488",
  "builder-bundle.tar.sha256": "2a4e4c080003c2a3d10e1456af923fde4e6e19b64efaf51b8aca68ea5d56f0c8",
  "installer-overlay.manifest.json": "b2fa5c6418f6bb0074048db010cdf267b25c53090c2a4abe36f2eae3f75d53f9",
  "installer-overlay.tar": "b5cf4014cd99d06a949fb7340e35fa506ec3c39fd7880c4d9d81f6fdd90ff775",
  "installer-overlay.tar.sha256": "aafa456360e4cda744d0fc5e0e96e447abc9d83409c49c76a6e5b8d0d80508ff",
}


def digest(data):
  return hashlib.sha256(data).hexdigest()


class DirectRestoreContract(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.temporary = tempfile.TemporaryDirectory(prefix="omarchy-direct-restore-contract-", dir="/tmp")
    cls.addClassCleanup(cls.temporary.cleanup)
    cls.work = Path(cls.temporary.name)
    cls.upstream = subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show",
      f"{direct_restore.UPSTREAM_COMMIT}:configs/airootfs/{PHASES}"])
    cls.prepared = root_image_mounts.patch_source(cls.upstream)
    cls.direct = direct_restore.patch_source(cls.prepared)
    cls.outputs = {}
    for mode, options in (("default", []), ("direct", ["--direct-restore"])):
      output = cls.work / mode
      subprocess.run([sys.executable, str(LOCAL / "prepare-bundles.py"),
        str(ISO_SOURCE), str(output), *options], check=True, capture_output=True, text=True)
      cls.outputs[mode] = output

  def test_only_destination_cache_option_changes(self):
    self.assertEqual(digest(self.upstream), direct_restore.UPSTREAM_SOURCE_SHA256)
    self.assertEqual(digest(self.prepared), direct_restore.PREPARED_SOURCE_SHA256)
    self.assertEqual(self.direct.count(direct_restore.NEW_ARGUMENTS), 1)
    self.assertEqual(self.direct.replace(direct_restore.NEW_ARGUMENTS,
      direct_restore.OLD_ARGUMENTS), self.prepared)

  def test_source_drift_missing_mount_fix_and_repeat_are_rejected(self):
    for source in (self.upstream, self.prepared + b"\n", self.direct):
      with self.subTest(digest=digest(source)), self.assertRaisesRegex(ValueError, "exact mount-corrected"):
        direct_restore.patch_source(source)

  def test_anchor_count_remains_guarded_if_pin_is_updated(self):
    for source in (self.prepared.replace(direct_restore.OLD_ARGUMENTS, b"[]"),
           self.prepared + direct_restore.OLD_ARGUMENTS):
      with patch.object(direct_restore, "PREPARED_SOURCE_SHA256", digest(source)):
        with self.assertRaisesRegex(ValueError, "Unexpected pinned"):
          direct_restore.patch_source(source)

  def execute_restore(self, status):
    # Execute the actual patched function, with a real subprocess crossing
    # an executable boundary. The command shim records arguments and errors;
    # actual block-device data correctness is a separate VM fixture.
    function = next(node for node in ast.parse(self.direct).body
      if isinstance(node, ast.FunctionDef) and node.name == "_restore_root_image")
    namespace = {"Path": Path, "os": SimpleNamespace(cpu_count=lambda: 2),
      "subprocess": subprocess}
    exec(compile(ast.Module(body=[function], type_ignores=[]),
      "actual-direct-restore-function", "exec"), namespace)
    with tempfile.TemporaryDirectory(prefix="command-", dir=self.work) as temporary:
      directory = Path(temporary)
      record = directory / "command.json"
      binary = directory / "qemu-img"
      binary.write_text(f"#!{sys.executable}\n" +
        "import json, os, pathlib, sys\n" +
        "pathlib.Path(os.environ['DIRECT_RESTORE_TEST_RECORD']).write_text(json.dumps(sys.argv[1:]))\n" +
        "status = int(os.environ['DIRECT_RESTORE_TEST_STATUS'])\n" +
        "if status: print('fixture rejected restore', file=sys.stderr)\n" +
        "sys.exit(status)\n")
      binary.chmod(0o755)
      image = directory / "source image.qcow2"
      target = str(directory / "target device")
      with patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
          "DIRECT_RESTORE_TEST_RECORD": str(record), "DIRECT_RESTORE_TEST_STATUS": str(status)}):
        if status:
          with self.assertRaisesRegex(RuntimeError,
              "root filesystem restore failed: fixture rejected restore"):
            namespace["_restore_root_image"](image, target)
        else:
          namespace["_restore_root_image"](image, target)
      self.assertEqual(json.loads(record.read_text()), ["convert", "-q", "-f", "qcow2", "-O",
        "raw", "-W", "-n", "-t", "none", "-m", "4", str(image), target])

  def test_actual_restore_executes_direct_destination_command(self):
    self.execute_restore(0)

  def test_actual_restore_failure_aborts_without_silent_fallback(self):
    self.execute_restore(23)

  def test_all_six_default_artifacts_are_byte_identical(self):
    actual = {path.name: digest(path.read_bytes()) for path in self.outputs["default"].iterdir()}
    self.assertEqual(actual, DEFAULT_DIGESTS)

  def test_opt_in_preserves_builder_and_other_overlay_members(self):
    for filename in DEFAULT_DIGESTS:
      if filename.startswith("builder-bundle"):
        self.assertEqual((self.outputs["default"] / filename).read_bytes(),
          (self.outputs["direct"] / filename).read_bytes())
    archives = []
    for mode in ("default", "direct"):
      with tarfile.open(self.outputs[mode] / "installer-overlay.tar") as archive:
        archives.append({item.name: (item.get_info(), archive.extractfile(item).read())
          for item in archive.getmembers()})
    baseline, direct = archives
    self.assertEqual(baseline.keys(), direct.keys())
    self.assertEqual([name for name in baseline if baseline[name] != direct[name]], [PHASES])
    self.assertEqual(baseline[PHASES][1], self.prepared)
    self.assertEqual(direct[PHASES][1], self.direct)
    # Tar size changes with the source; all other metadata stays fixed.
    self.assertEqual({k: v for k, v in baseline[PHASES][0].items() if k != "size"},
      {k: v for k, v in direct[PHASES][0].items() if k != "size"})

  def test_opt_in_records_distinct_provenance_and_valid_archive_checksums(self):
    default = json.loads((self.outputs["default"] / "installer-overlay.manifest.json").read_text())
    manifest = json.loads((self.outputs["direct"] / "installer-overlay.manifest.json").read_text())
    self.assertNotIn("direct_restore", default)
    self.assertEqual(manifest["upstream_commit"], direct_restore.UPSTREAM_COMMIT)
    self.assertEqual(manifest["direct_restore"], {"target_cache": "none",
      "after_mount_fix_sha256": direct_restore.PREPARED_SOURCE_SHA256})
    phase_entry = next(entry for entry in manifest["files"] if entry["path"] == PHASES)
    self.assertEqual(phase_entry["upstream_sha256"], direct_restore.UPSTREAM_SOURCE_SHA256)
    self.assertEqual(phase_entry["after_mount_fix_sha256"], direct_restore.PREPARED_SOURCE_SHA256)
    self.assertEqual(phase_entry["sha256"], digest(self.direct))
    self.assertEqual(phase_entry["direct_restore"]["target_cache"], "none")
    self.assertNotEqual(default["sha256"], manifest["sha256"])
    archive = self.outputs["direct"] / "installer-overlay.tar"
    self.assertEqual(manifest["sha256"], digest(archive.read_bytes()))
    self.assertEqual(archive.with_suffix(".tar.sha256").read_text(),
      f"{manifest['sha256']}  installer-overlay.tar\n")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  arguments, remaining = parser.parse_known_args()
  ISO_SOURCE = arguments.iso_source.resolve()
  unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
