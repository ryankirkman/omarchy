#!/usr/bin/env python3
"""Check native benchmark process cleanup and evidence boundaries."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

DIRECTORY = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_experiment', DIRECTORY / 'run-native-experiment.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativeContract(unittest.TestCase):
  def test_timeout_terminates_child_group(self):
    with tempfile.TemporaryDirectory() as temporary:
      pid_file = Path(temporary) / 'child.pid'
      script = "import pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
      with self.assertRaises(subprocess.TimeoutExpired):
        module.command([sys.executable, '-c', script, pid_file], timeout=0.3)
      pid = int(pid_file.read_text())
      deadline = time.monotonic() + 3
      while True:
        stat = Path(f'/proc/{pid}/stat')
        if not stat.exists() or stat.read_text().split()[2] == 'Z':
          break
        self.assertLess(time.monotonic(), deadline, 'child survived timeout cleanup')
        time.sleep(0.05)

  def test_evidence_excludes_private_and_large_state(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      run, output = root / 'run', root / 'output'
      run.mkdir()
      for name in ('manifest.json', 'package-manifest.txt', 'id_ed25519', 'cidata.img', 'target.qcow2', 'OVMF_VARS_4M.fd'):
        (run / name).write_text('fixture')
      module.collect_small(run, output)
      self.assertEqual({p.name for p in output.iterdir()}, {'manifest.json', 'package-manifest.txt'})

  def test_rejects_oversized_evidence(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      run = root / 'run'
      run.mkdir()
      with (run / 'serial.log').open('wb') as output:
        output.truncate(21 * 1024**2)
      with self.assertRaises(ValueError):
        module.collect_small(run, root / 'output')

  def test_existing_work_fails_before_touching_vm_state(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      with self.assertRaisesRegex(ValueError, 'new empty work path'):
        module.execute(SimpleNamespace(repo=DIRECTORY, work=root, evidence=root / 'evidence'))
      self.assertEqual(list(root.iterdir()), [])

  def test_pinned_sources(self):
    self.assertEqual(module.HARNESS_PIN, '2673c613d9a71e23920e43fbb951238145e0f1e8')
    self.assertEqual(module.FAST_PIN, 'dbffaa6c65344d644627a023c28661e08382b8fa')
    self.assertEqual(module.ISO_SHA256, '2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8')


if __name__ == '__main__':
  unittest.main()
