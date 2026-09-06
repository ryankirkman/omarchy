#!/usr/bin/env python3
"""Exercise logger guard refusals and cleanup; actual mounts use guest-contract.py."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('logging_bind_guard', HERE / 'guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
REPO = HERE.parents[3]
ORIGINAL = HERE.parent / 'localdb-overlap/fixtures/runtime/usr/share/omarchy/install/helpers/logging.sh'
OPTIMIZED = REPO / 'install/helpers/logging.sh'


class GuardContract(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix='omarchy-logging-guard-')
    self.addCleanup(self.temp.cleanup)
    self.target = Path(self.temp.name) / 'target'
    self.logger = self.target / guard.RELATIVE_LOGGER
    self.logger.parent.mkdir(parents=True)
    self.logger.write_bytes(ORIGINAL.read_bytes())
    self.logger.chmod(0o644)
    self.helper = Path(self.temp.name) / 'logging.sh'
    self.helper.write_bytes(OPTIMIZED.read_bytes())
    self.helper.chmod(0o644)
    self.command = [*guard.PRIVATE_PREFIX, 'arch-chroot', str(self.target), 'bash', '-c', 'exit 0']
    self.original = guard.file_record(self.logger, guard.ORIGINAL_LOGGER_SHA256)

  def test_exact_hashes_modes_and_no_symlink(self):
    guard.file_record(self.helper, guard.LOGGER_SHA256)
    self.logger.write_bytes(self.logger.read_bytes() + b'\n')
    with self.assertRaisesRegex(ValueError, 'hash differs'):
      guard.file_record(self.logger, guard.ORIGINAL_LOGGER_SHA256)
    self.logger.write_bytes(ORIGINAL.read_bytes())
    self.logger.chmod(0o600)
    with self.assertRaisesRegex(ValueError, 'mode 0644'):
      guard.file_record(self.logger, guard.ORIGINAL_LOGGER_SHA256)
    self.logger.unlink()
    self.logger.symlink_to(ORIGINAL)
    with self.assertRaisesRegex(ValueError, 'nonsymlink'):
      guard.file_record(self.logger, guard.ORIGINAL_LOGGER_SHA256)

  def test_command_preserves_root_user_and_arguments(self):
    self.assertEqual(guard.validate_command(self.target, self.command), self.command[5:])
    user = [*guard.PRIVATE_PREFIX, 'arch-chroot', '-u', 'bench-user', str(self.target),
      'env', 'NAME=value with spaces', 'bash', '-c', 'printf "%s" "$1"', 'original-zero', 'literal;$value']
    self.assertEqual(guard.validate_command(self.target, user), user[5:])
    for bad in (self.command[5:], ['unshare', '--mount', '--', *self.command[5:]],
        [*guard.PRIVATE_PREFIX, 'arch-chroot', '/', 'bash'],
        [*guard.PRIVATE_PREFIX, 'arch-chroot', '-u', '', str(self.target), 'bash']):
      with self.assertRaises(ValueError):
        guard.validate_command(self.target, bad)

  def test_target_parent_symlink_and_root_rejected(self):
    with self.assertRaises(ValueError):
      guard.target_logger(Path('/'))
    with self.assertRaises(ValueError):
      guard.target_logger(Path('relative'))
    link = self.target.with_name('linked-target')
    link.symlink_to(self.target, target_is_directory=True)
    with self.assertRaises(ValueError):
      guard.target_logger(link)

  def test_same_namespace_rejected_before_mount_or_child(self):
    descriptor = os.open('/proc/self/ns/mnt', os.O_RDONLY)
    try:
      with patch.object(guard.subprocess, 'run') as launch:
        with self.assertRaisesRegex(RuntimeError, 'distinct mount namespace'):
          guard.run_inside(self.target, self.command[5:], descriptor, self.helper)
        launch.assert_not_called()
    finally:
      os.close(descriptor)

  def test_parent_requires_unshare_and_no_existing_mount(self):
    with patch.object(guard, 'mount_record', return_value=None), patch.object(guard.shutil, 'which', return_value=None):
      with self.assertRaisesRegex(RuntimeError, 'requires unshare'):
        guard.run(self.target, self.command, self.helper)
    with patch.object(guard, 'mount_record', return_value={'target': str(self.logger)}):
      with self.assertRaisesRegex(RuntimeError, 'parent namespace'):
        guard.run(self.target, self.command, self.helper)

  def test_owned_namespace_descriptor_and_child_failure_status(self):
    observed = []
    def launch(argv, **kwargs):
      descriptor, = kwargs['pass_fds']
      self.assertRegex(os.readlink(f'/proc/self/fd/{descriptor}'), r'^mnt:\[\d+\]$')
      self.assertEqual(argv[:5], ['/usr/bin/unshare', *guard.PRIVATE_PREFIX[1:]])
      self.assertEqual(argv[-len(self.command[5:]):], self.command[5:])
      observed.append(descriptor)
      return SimpleNamespace(returncode=37)
    with patch.object(guard, 'mount_record', return_value=None), patch.object(guard.shutil, 'which', return_value='/usr/bin/unshare'), patch.object(guard.subprocess, 'run', side_effect=launch):
      self.assertEqual(guard.run(self.target, self.command, self.helper), 37)
    with self.assertRaises(OSError):
      os.fstat(observed[0])
    self.assertEqual(guard.file_record(self.logger, guard.ORIGINAL_LOGGER_SHA256), self.original)

  def lifecycle(self, *, child_status=0, remount_fails=False, readonly=True, unmount_fails=False):
    state = {'mounted': False, 'readonly': False, 'child': 0, 'unmount': 0}
    record = guard.file_record
    replacement = record(self.helper, guard.LOGGER_SHA256)
    def file_record(path, expected):
      if path == self.logger and state['mounted']:
        self.assertEqual(expected, guard.LOGGER_SHA256)
        return replacement
      return record(path, expected)
    def mount_record(path):
      return {'target': str(path), 'options': 'ro,relatime' if state['readonly'] else 'rw,relatime'} if state['mounted'] else None
    def launch(argv, **kwargs):
      if argv[:2] == ['mount', '--bind']:
        state['mounted'] = True
      elif argv[:3] == ['mount', '-o', 'remount,bind,ro']:
        if remount_fails:
          raise subprocess.CalledProcessError(19, argv)
        state['readonly'] = readonly
      elif argv[0] == 'umount':
        state['unmount'] += 1
        if unmount_fails:
          raise subprocess.CalledProcessError(29, argv)
        state['mounted'] = False
      elif argv[0] == 'arch-chroot':
        self.assertTrue(state['mounted'] and state['readonly'])
        state['child'] += 1
        return SimpleNamespace(returncode=child_status)
      else:
        self.fail('Unexpected command: ' + repr(argv))
      return SimpleNamespace(returncode=0)
    real_stat = guard.os.stat
    def namespace_stat(path, *args, **kwargs):
      return SimpleNamespace(st_dev=1, st_ino=2) if str(path) == '/proc/self/ns/mnt' else real_stat(path, *args, **kwargs)
    with patch.object(guard.os, 'readlink', return_value='mnt:[1]'), patch.object(guard.os, 'fstat', return_value=SimpleNamespace(st_dev=1, st_ino=1)), patch.object(guard.os, 'stat', side_effect=namespace_stat), patch.object(guard, 'file_record', side_effect=file_record), patch.object(guard, 'mount_record', side_effect=mount_record), patch.object(guard.subprocess, 'run', side_effect=launch):
      try:
        result = guard.run_inside(self.target, self.command[5:], 99, self.helper)
      except (RuntimeError, subprocess.CalledProcessError) as error:
        result = error
    return state, result

  def test_private_lifecycle_joins_cleanup_and_propagates_failure(self):
    for code in (0, 37):
      state, result = self.lifecycle(child_status=code)
      self.assertEqual(result, code)
      self.assertEqual(state, {'mounted': False, 'readonly': True, 'child': 1, 'unmount': 1})

  def test_readonly_setup_failure_cleans_without_running_child(self):
    for settings in ({'remount_fails': True}, {'readonly': False}):
      state, result = self.lifecycle(**settings)
      self.assertIsInstance(result, (RuntimeError, subprocess.CalledProcessError))
      self.assertFalse(state['mounted'])
      self.assertEqual(state['child'], 0)
      self.assertEqual(state['unmount'], 1)

  def test_unmount_failure_cannot_report_success(self):
    state, result = self.lifecycle(unmount_fails=True)
    self.assertIsInstance(result, subprocess.CalledProcessError)
    self.assertEqual(result.returncode, 29)
    self.assertEqual(state['child'], 1)


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--iso-source', type=Path, required=True)
  args = parser.parse_args()
  result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GuardContract))
  if not result.wasSuccessful():
    raise SystemExit(1)
  subprocess.run([shutil.which('python3'), str(HERE / 'payload-contract-test.py'), '--iso-source', str(args.iso_source)], check=True)
