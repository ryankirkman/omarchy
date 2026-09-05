#!/usr/bin/env python3
"""Host-only archive and preflight contracts; these are not guest boot tests."""

import gzip
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("overlay", Path(__file__).with_name("make-initramfs.py"))
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class OverlayContracts(unittest.TestCase):
  def fixture(self):
    return overlay.make_cpio({
      "config": (stat.S_IFREG | 0o644, b'EARLYHOOKS="udev"\nLATEHOOKS="archiso_pxe_common plymouth"\n'),
      "init": (stat.S_IFREG | 0o755, b'"$mount_handler" /new_root\nrun_hookfunctions \'run_latehook\' \'late hook\' $LATEHOOKS\n'),
    })

  def test_archive_modes_and_content(self):
    original = self.fixture()
    combined, metadata = overlay.build(original, "control", b"#!/bin/bash\ntrue\n")
    self.assertEqual(combined[:len(original)], original)
    entries = overlay.initramfs_files(combined)
    self.assertIn(b'LATEHOOKS="archiso_pxe_common plymouth"', entries["config"][1])
    self.assertTrue(entries["config"][1].endswith(b'LATEHOOKS="$LATEHOOKS omarchy_benchmark"\n'))
    self.assertEqual(entries["hooks/omarchy_benchmark"][0], stat.S_IFREG | 0o755)
    self.assertEqual(metadata["original_initramfs_sha256"], overlay.sha256(original))

  def test_early_and_compressed_archive(self):
    early = overlay.make_cpio({"early_cpio": (stat.S_IFREG | 0o644, b"1\n")})
    files = overlay.initramfs_files(early + gzip.compress(self.fixture(), mtime=0))
    self.assertIn("early_cpio", files)
    self.assertIn("config", files)

  def test_refuse_incompatible_or_double_overlay(self):
    with self.assertRaises(ValueError):
      overlay.build(overlay.make_cpio({}), "control", b"true")
    combined, _ = overlay.build(self.fixture(), "control", b"true")
    with self.assertRaises(ValueError):
      overlay.build(combined, "control", b"true")

  def test_reserved_paths_and_symlinks_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      payload = Path(directory)
      (payload / "escape").symlink_to("/etc/passwd")
      with self.assertRaises(ValueError):
        overlay.build(self.fixture(), "candidate", b"true", payload)
      (payload / "escape").unlink()
      (payload / "root").mkdir()
      (payload / "root/.automated_script.sh").write_text("unexpected")
      with self.assertRaises(ValueError):
        overlay.build(self.fixture(), "candidate", b"true", payload)

  def test_preflight_failure_precedes_autoinstall(self):
    text = overlay.wrapper("candidate").decode()
    self.assertLess(text.index("preflight.sh"), text.index("exec /root/.automated_script.benchmark-original.sh"))
    self.assertIn("exit 1", text[text.index("preflight.sh"):text.index("preflight-complete")])
    self.assertIn("if [[ 'builder' == 'builder' ]]", overlay.wrapper("builder").decode())

  def test_hook_keeps_existing_directory_permissions(self):
    self.assertIn(b'mkdir -p "/new_root/${relative%/*}"', overlay.HOOK)
    self.assertIn(b'cp -p "$source" "/new_root/$relative"', overlay.HOOK)
    self.assertNotIn(b"cp -a", overlay.HOOK)

  def test_truncated_archive_rejected(self):
    with self.assertRaises(ValueError):
      overlay.cpio_entries(self.fixture()[:120])


if __name__ == "__main__":
  unittest.main()
