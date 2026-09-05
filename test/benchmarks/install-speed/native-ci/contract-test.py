#!/usr/bin/env python3
"""Check native benchmark process cleanup and evidence boundaries."""
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_experiment', DIRECTORY / 'run-native-experiment.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def load_sibling(name):
  source = importlib.util.spec_from_file_location(name.replace('-', '_'), DIRECTORY / (name + '.py'))
  loaded = importlib.util.module_from_spec(source)
  source.loader.exec_module(loaded)
  return loaded


rescue = load_sibling('rescue-failed-install')
collector = load_sibling('collect-failed-install')


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

  def test_standalone_public_evidence_is_preserved(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      run, output = root / 'run', root / 'output'
      run.mkdir()
      for name in ('standalone-reboot.json', 'standalone-root.json', 'standalone-identity.json',
          'standalone-machine-id.txt', 'standalone-ssh-host-fingerprints.txt',
          'standalone-pacman-master-keys.txt', 'standalone-btrfs-uuid.txt',
          'standalone-btrfs-subvolumes.txt', 'standalone-uki-files.txt', 'id_ed25519'):
        (run / name).write_text('fixture')
      module.collect_small(run, output)
      self.assertEqual(len(list(output.iterdir())), 9)
      self.assertFalse((output / 'id_ed25519').exists())

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

  def test_cache_and_variant_selection(self):
    base = ['--repo', '/repo', '--work', '/new-work', '--evidence', '/evidence']
    default = module.parse_arguments(base)
    self.assertEqual(default.boot_method, 'direct')
    self.assertEqual(default.source_cache, 'conditioned')
    self.assertEqual(default.variants, ['upstream-image', 'image-no-package-prefetch'])
    selected = module.parse_arguments(base + ['--source-cache', 'cold', '--variants', 'image-no-package-prefetch'])
    self.assertEqual(selected.source_cache, 'cold')
    self.assertEqual(selected.variants, ['image-no-package-prefetch'])
    self.assertEqual(module.parse_arguments(base + ['--variants', 'image-no-package-prefetch-fast-reboot']).variants,
      ['image-no-package-prefetch-fast-reboot'])
    self.assertEqual(module.parse_arguments(base + ['--boot-method', 'firmware']).boot_method, 'firmware')
    for invalid in (['--source-cache', 'warm'], ['--boot-method', 'automatic'], ['--variants', 'upstream-image', 'upstream-image']):
      with contextlib.redirect_stderr(io.StringIO()):
        with self.assertRaises(SystemExit) as result:
          module.parse_arguments(base + invalid)
      self.assertEqual(result.exception.code, 1)

  def test_firmware_keeps_builder_direct_and_requires_standalone_proof(self):
    source, derived, kernel, initrd = map(Path, ('/original.iso', '/derived.iso', '/kernel', '/initrd'))
    firmware = module.boot_arguments('firmware', source, kernel, initrd, firmware_iso=derived)
    self.assertEqual(firmware, ['--iso', derived, '--verify-standalone-reboot'])
    for method in ('firmware', 'direct'):
      builder = module.boot_arguments(method, source, kernel, initrd, builder=True)
      self.assertEqual(builder, ['--iso', source, '--kernel', kernel, '--initrd', initrd, '--append', module.CMDLINE])
    with self.assertRaisesRegex(ValueError, 'verified derived ISO'):
      module.boot_arguments('firmware', source, kernel, initrd)

  def test_firmware_disk_headroom_fails_before_preparation(self):
    with patch.object(module.shutil, 'disk_usage', return_value=SimpleNamespace(free=43 * 1024**3)):
      with self.assertRaisesRegex(RuntimeError, '44 GiB'):
        module.disk_budget(Path('/tmp'), 'firmware')
      self.assertEqual(module.disk_budget(Path('/tmp'), 'direct')['minimum_free_bytes'], 28 * 1024**3)
    with patch.object(module.shutil, 'disk_usage', return_value=SimpleNamespace(free=44 * 1024**3)):
      self.assertEqual(module.disk_budget(Path('/tmp'), 'firmware')['minimum_free_bytes'], 44 * 1024**3)

  def test_pinned_sources(self):
    self.assertEqual(module.HARNESS_PIN, '2673c613d9a71e23920e43fbb951238145e0f1e8')
    self.assertEqual(module.FAST_PIN, 'dbffaa6c65344d644627a023c28661e08382b8fa')
    self.assertEqual(module.ISO_SHA256, '2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8')

  def test_rescue_refuses_an_active_supervisor(self):
    with tempfile.TemporaryDirectory() as temporary:
      run = Path(temporary)
      (run / 'supervisor.pid').write_text(str(os.getpid()))
      with self.assertRaisesRegex(RuntimeError, 'still alive'):
        rescue.assert_stopped(run)

  def test_rescue_requires_a_readonly_dedicated_target(self):
    args = rescue.readonly_target_args(Path('/tmp/failed/target.qcow2'))
    self.assertIn('readonly=on', args[1])
    self.assertIn('serial=OMARCHY_RESCUE', args[3])
    with self.assertRaises(ValueError):
      rescue.readonly_target_args(Path('/tmp/target,readonly=off'))
    with patch.object(collector.os, 'geteuid', return_value=0), \
        patch.object(collector.Path, 'is_file', return_value=True), \
        patch.object(collector.Path, 'is_block_device', return_value=True), \
        patch.object(collector, 'required', side_effect=['kvm', '0']) as invoked:
      with self.assertRaisesRegex(RuntimeError, 'must be read-only'):
        collector.collect()
      self.assertEqual(invoked.call_count, 2, 'Writable media reached a mount or inspection command')

  def test_rescue_network_and_log_redaction(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      profile = root / 'profile.nmconnection'
      profile.write_text('[connection]\nid=fixture\ntype=wifi\n[wifi-security]\npsk=DO-NOT-EXPORT\n[ipv4]\nmethod=auto\n')
      filtered = collector.network_profile(profile, root)
      self.assertEqual(filtered, {'connection': {'id': 'fixture', 'type': 'wifi'}, 'ipv4': {'method': 'auto'}})
      text = collector.redact('PasswordAuthentication no\npassword=DO-NOT-EXPORT\n-----BEGIN OPENSSH PRIVATE KEY-----\nSENSITIVE\n-----END OPENSSH PRIVATE KEY-----\nboot completed')
      self.assertIn('PasswordAuthentication no', text)
      self.assertIn('boot completed', text)
      self.assertNotIn('DO-NOT-EXPORT', text)
      self.assertNotIn('SENSITIVE', text)
      private = root / 'ssh_host_ed25519_key'
      private.write_text('DO-NOT-EXPORT')
      self.assertNotIn('DO-NOT-EXPORT', str(collector.public_key_metadata(private, root)))


if __name__ == '__main__':
  unittest.main()
