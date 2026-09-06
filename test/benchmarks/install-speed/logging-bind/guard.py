#!/usr/bin/env python3
"""Expose the optimized logger only inside one owned setup mount namespace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

ORIGINAL_LOGGER_SHA256 = '61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27'
LOGGER_SHA256 = '1d8151adb150bc1dfe930b30e7039978591add500a132b9951152d7a8a23d715'
RELATIVE_LOGGER = Path('usr/share/omarchy/install/helpers/logging.sh')
PRIVATE_PREFIX = ['unshare', '--mount', '--propagation', 'private', '--']


def file_record(path, expected):
  if path.is_symlink() or not path.is_file():
    raise ValueError(f'Logging bind requires a regular nonsymlink file: {path}')
  status = path.stat()
  if stat.S_IMODE(status.st_mode) != 0o644:
    raise ValueError(f'Logging bind requires mode 0644: {path}')
  data = path.read_bytes()
  if hashlib.sha256(data).hexdigest() != expected:
    raise ValueError(f'Logging bind source hash differs: {path}')
  return {key: getattr(status, key) for key in
    ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}


def target_logger(target):
  if not target.is_absolute() or target.resolve() != target or target == Path('/'):
    raise ValueError('Logging bind requires an absolute nonsymlink installed target')
  current = target
  for part in RELATIVE_LOGGER.parts[:-1]:
    current /= part
    if current.is_symlink() or not current.is_dir():
      raise ValueError(f'Logging bind target parents must be real directories: {current}')
  return target / RELATIVE_LOGGER


def validate_command(target, command, *, inside=False):
  prefix = [] if inside else PRIVATE_PREFIX
  if command[:len(prefix)] != prefix:
    raise ValueError('Logging bind requires the existing private unshare command; no fallback')
  inner = command[len(prefix):]
  if not inner or inner[0] != 'arch-chroot':
    raise ValueError('Logging bind requires the original arch-chroot command')
  position = 1
  if len(inner) > 1 and inner[1] == '-u':
    if len(inner) < 4 or not inner[2] or inner[2].startswith('-'):
      raise ValueError('Logging bind requires an explicit valid target user')
    position = 3
  if len(inner) <= position + 1 or inner[position] != str(target):
    raise ValueError('Logging bind chroot target or command differs')
  return inner


def mount_record(path):
  result = subprocess.run(['findmnt', '--json', '--mountpoint', str(path), '--output', 'TARGET,OPTIONS'],
    capture_output=True, text=True, check=False)
  if result.returncode == 1 and not result.stdout.strip():
    return None
  if result.returncode != 0:
    raise RuntimeError('Cannot inspect the logging helper mount: ' + result.stderr)
  rows = json.loads(result.stdout).get('filesystems', [])
  if len(rows) != 1 or rows[0].get('target') != str(path):
    raise RuntimeError('Logging helper mount observation is absent or ambiguous')
  return rows[0]


def run_inside(target, command, namespace_fd, helper):
  inner = validate_command(target, command, inside=True)
  parent_name = os.readlink(f'/proc/self/fd/{namespace_fd}')
  before_namespace = os.fstat(namespace_fd)
  own_namespace = os.stat('/proc/self/ns/mnt')
  if (not re.fullmatch(r'mnt:\[\d+\]', parent_name)
      or before_namespace.st_dev != own_namespace.st_dev
      or before_namespace.st_ino == own_namespace.st_ino):
    raise RuntimeError('Logging bind did not enter a distinct mount namespace')
  logger = target_logger(target)
  original = file_record(logger, ORIGINAL_LOGGER_SHA256)
  replacement = file_record(helper, LOGGER_SHA256)
  if mount_record(logger) is not None:
    raise RuntimeError('Logging helper already has a mount; refusing to stack it')
  mounted = False
  try:
    subprocess.run(['mount', '--bind', str(helper), str(logger)], check=True)
    mounted = True
    subprocess.run(['mount', '-o', 'remount,bind,ro', str(logger)], check=True)
    observation = mount_record(logger)
    options = observation.get('options', '').split(',') if observation else []
    if 'ro' not in options or 'rw' in options:
      raise RuntimeError('Logging helper bind is not observed read-only')
    if file_record(logger, LOGGER_SHA256) != replacement:
      raise RuntimeError('Logging helper bind does not expose the exact staged inode')
    return subprocess.run(inner, check=False).returncode
  finally:
    if mounted:
      subprocess.run(['umount', str(logger)], check=True)
      if mount_record(logger) is not None:
        raise RuntimeError('Logging helper mount remains after setup')
      if file_record(logger, ORIGINAL_LOGGER_SHA256) != original:
        raise RuntimeError('Underlying package logging helper changed during setup')


def run(target, command, helper=None):
  helper = helper or Path(__file__).resolve().with_name('logging.sh')
  inner = validate_command(target, command)
  logger = target_logger(target)
  original = file_record(logger, ORIGINAL_LOGGER_SHA256)
  file_record(helper, LOGGER_SHA256)
  if mount_record(logger) is not None:
    raise RuntimeError('Package logging helper is mounted in the parent namespace')
  unshare = shutil.which('unshare')
  if not unshare:
    raise RuntimeError('Logging bind requires unshare; no non-private fallback')
  namespace_fd = os.open('/proc/self/ns/mnt', os.O_RDONLY)
  try:
    argv = [unshare, *PRIVATE_PREFIX[1:], sys.executable, str(Path(__file__).resolve()),
      '--inside-fd', str(namespace_fd), '--target', str(target), '--', *inner]
    return subprocess.run(argv, pass_fds=(namespace_fd,), check=False).returncode
  finally:
    os.close(namespace_fd)
    if mount_record(logger) is not None:
      raise RuntimeError('Logging helper mount leaked into the parent namespace')
    if file_record(logger, ORIGINAL_LOGGER_SHA256) != original:
      raise RuntimeError('Package logging helper changed outside the private namespace')


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--target', type=Path, required=True)
  parser.add_argument('--inside-fd', type=int)
  parser.add_argument('command', nargs=argparse.REMAINDER)
  args = parser.parse_args()
  command = args.command[1:] if args.command[:1] == ['--'] else args.command
  try:
    if args.inside_fd is None:
      status = run(args.target, command)
    else:
      status = run_inside(args.target, command, args.inside_fd, Path(__file__).resolve().with_name('logging.sh'))
  except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
    print('Logging bind refused: ' + str(error), file=sys.stderr)
    return 1
  return status if status >= 0 else 128 - status


if __name__ == '__main__':
  sys.exit(main())
