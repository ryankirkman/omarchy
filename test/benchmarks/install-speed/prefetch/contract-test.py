#!/usr/bin/env python3
"""Apply the opt-in patch to its exact source and exercise real Bash policy."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PIN = "dbffaa6c65344d644627a023c28661e08382b8fa"
ENTRYPOINT = "configs/airootfs/root/.automated_script.sh"
PATCH = Path(__file__).with_name("experimental-verify-only-prefetch.patch").resolve()
UPSTREAM = None


class PrefetchPolicy(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.tree = tempfile.TemporaryDirectory(prefix="omarchy-prefetch-source-")
    cls.addClassCleanup(cls.tree.cleanup)
    root = Path(cls.tree.name)
    source = subprocess.check_output(
      ["git", "show", f"{PIN}:{ENTRYPOINT}"], cwd=UPSTREAM, text=True)
    script = root / ENTRYPOINT
    script.parent.mkdir(parents=True)
    script.write_text(source)
    subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=root, check=True)
    subprocess.run(["git", "apply", str(PATCH)], cwd=root, check=True)
    subprocess.run(["bash", "-n", str(script)], check=True)
    cls.original = cls.function(source)
    cls.patched = cls.function(script.read_text())

  @staticmethod
  def function(source):
    start = source.index("warm_offline_mirror() {\n")
    end = source.index("\n}\n", start) + 3
    return source[start:end]

  def run_warmer(self, *, patched=True, image=True, legacy=False,
                 checksum=True, option=None, disabled=None, state="active",
                 available_kb=1048576):
    with tempfile.TemporaryDirectory(prefix="omarchy-prefetch-fixture-") as directory:
      root = Path(directory)
      medium = root / "medium"
      mirror = root / "mirror"
      medium.mkdir()
      mirror.mkdir()
      if image:
        (medium / "omarchy-root.btrfs.qcow2").write_text("image fixture")
      if checksum:
        (medium / "omarchy-root.btrfs.qcow2.sha256").write_text("checksum fixture")
      if legacy:
        (medium / "omarchy-root.btrfs.zst").write_text("legacy fixture")
      (mirror / "a.pkg.tar.zst").write_text("package A")
      (mirror / "b.pkg.tar.zst").write_text("package B")
      (root / "meminfo").write_text(f"MemAvailable: {available_kb} kB\n")
      log = root / "reads"
      log.touch()
      body = self.patched if patched else self.original
      body = body.replace("/run/archiso/bootmnt/arch/x86_64", str(medium))
      body = body.replace("/var/cache/omarchy/mirror/offline", str(mirror))
      body = body.replace("/proc/meminfo", str(root / "meminfo"))
      script = root / "exercise.sh"
      script.write_text("""#!/bin/bash
set -euo pipefail
cat() { printf 'cat %s\\n' "$*" >>"$READ_LOG"; command cat "$@"; }
head() { printf 'head %s\\n' "$*" >>"$READ_LOG"; command head "$@"; }
systemctl() { printf 'systemctl %s\\n' "$*" >>"$READ_LOG"; printf '%s\\n' "$UNIT_STATE"; }
""" + body + "\nwarm_offline_mirror\n")
      environment = os.environ.copy()
      for name in ("OMARCHY_NO_PREFETCH", "OMARCHY_EXPERIMENTAL_VERIFY_ONLY_PREFETCH"):
        environment.pop(name, None)
      environment.update(READ_LOG=str(log), UNIT_STATE=state)
      if option is not None:
        environment["OMARCHY_EXPERIMENTAL_VERIFY_ONLY_PREFETCH"] = option
      if disabled is not None:
        environment["OMARCHY_NO_PREFETCH"] = disabled
      subprocess.run(["bash", str(script)], env=environment, check=True, timeout=5)
      return log.read_text().replace(str(root), "$FIXTURE").splitlines()

  def test_image_opt_in_leaves_warming_to_verifier(self):
    # The cache policy must not wait on, start, or replace the security gate.
    # Even an unfinished or failed verification gets no speculative reader.
    for state in ("active", "activating", "failed", "inactive"):
      with self.subTest(state=state):
        self.assertEqual(self.run_warmer(option="1", state=state, legacy=True), [])

  def test_missing_checksum_does_not_trigger_speculative_reads(self):
    # This is not a successful verification: the unchanged foreground gate
    # will fail the install. The warmer simply has no useful work to do.
    self.assertEqual(self.run_warmer(option="1", checksum=False), [])

  def test_package_only_media_preserve_existing_prefetch(self):
    original = self.run_warmer(patched=False, image=False)
    self.assertEqual(self.run_warmer(option="1", image=False), original)
    packages = [line for line in original if line.startswith("cat ")]
    self.assertEqual(len(packages), 2)
    self.assertTrue(all(".pkg.tar.zst" in line for line in packages))

  def test_default_and_other_values_preserve_pinned_behavior(self):
    original = self.run_warmer(patched=False, legacy=True)
    self.assertTrue(any(line.startswith("head ") for line in original))
    for option in (None, "0", "true", "2"):
      with self.subTest(option=option):
        self.assertEqual(self.run_warmer(option=option, legacy=True), original)

  def test_global_disable_remains_effective_for_both_media(self):
    for image in (False, True):
      with self.subTest(image=image):
        self.assertEqual(self.run_warmer(image=image, option="1", disabled="1"), [])

  def test_small_memory_package_only_path_still_obeys_budget_gate(self):
    self.assertEqual(self.run_warmer(image=False, option="1", available_kb=262144), [])


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("upstream", type=Path, help="local omarchy-iso checkout containing the pinned commit")
  args, rest = parser.parse_known_args()
  UPSTREAM = args.upstream.resolve()
  unittest.main(argv=[__file__, *rest])
