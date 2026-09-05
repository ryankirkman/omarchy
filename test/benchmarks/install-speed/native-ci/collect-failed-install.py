#!/usr/bin/env python3
"""Collect a small, public diagnostic record from one read-only rescue disk."""
import configparser
import json
import os
from pathlib import Path
import re
import stat
import subprocess

DEVICE = Path('/dev/disk/by-id/virtio-OMARCHY_RESCUE')
MOUNT = Path('/run/omarchy-failed-install')
LIMIT = 2 * 1024**2


def redact(text):
  text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
    '[PRIVATE KEY REDACTED]', text, flags=re.S)
  return '\n'.join(line if re.match(r'(?i)^\s*passwordauthentication\s+(yes|no)\s*$', line)
    else '[CREDENTIAL LINE REDACTED]' if re.search(
    r'(?i)(password|passwd|\bpsk\b|secret|token|authorization:|authorized_keys|PRIVATE KEY)', line)
    else line for line in text.splitlines())


def command(argv, timeout=20):
  try:
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return {'argv': argv, 'exit_status': result.returncode,
      'output': redact(result.stdout)[-LIMIT:], 'truncated': len(result.stdout) > LIMIT}
  except subprocess.TimeoutExpired as error:
    output = error.stdout or b''
    if isinstance(output, bytes):
      output = output.decode(errors='replace')
    return {'argv': argv, 'error': 'command timed out', 'output': redact(output)[-LIMIT:],
      'truncated': len(output) > LIMIT}
  except OSError as error:
    return {'argv': argv, 'error': redact(str(error))}


class CommandFailure(RuntimeError):
  def __init__(self, record):
    self.record = record
    super().__init__('Required diagnostic command failed: ' + json.dumps(record))


def failed_command(argv, error, stdout='', stderr='', exit_status=None):
  record = {'argv': argv, 'error': error, 'exit_status': exit_status}
  for name, value in (('stdout', stdout or ''), ('stderr', stderr or '')):
    if isinstance(value, bytes):
      value = value.decode(errors='replace')
    record[name] = redact(value)[-LIMIT:]
    record[name + '_truncated'] = len(value) > LIMIT
  return CommandFailure(record)


def required(argv):
  try:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=20, check=False)
  except subprocess.TimeoutExpired as error:
    raise failed_command(argv, 'command timed out', error.stdout, error.stderr) from None
  except OSError as error:
    raise failed_command(argv, redact(str(error))) from None
  if result.returncode:
    raise failed_command(argv, 'nonzero exit', result.stdout, result.stderr, result.returncode)
  return result.stdout.strip()


def mount_failure_diagnostics(result, partitions):
  # These commands inspect only. blkid --probe bypasses its persistent cache;
  # no alternate mount, log replay, repair or writable device is attempted.
  result['commands']['failure_mounts'] = command(['findmnt', '--json', '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS'])
  result['commands']['failure_block_devices'] = command(['lsblk', '--json', '--paths', '--output',
    'NAME,TYPE,FSTYPE,RO,MOUNTPOINTS', str(DEVICE)])
  for row in partitions[:8]:
    result['commands']['failure_blkid/' + Path(row['name']).name] = command([
      'blkid', '--probe', '--output', 'export', row['name']])
  kernel = command(['dmesg', '--color=never'])
  lines = [line for line in kernel.get('output', '').splitlines() if re.search(r'(?i)btrfs|mount', line)]
  kernel['output'] = '\n'.join(lines[-200:])
  kernel['truncated'] = kernel.get('truncated', False) or len(lines) > 200
  kernel['filter'] = 'Last 200 Btrfs or mount lines from the bounded rescue kernel log tail'
  result['commands']['failure_kernel_mount_tail'] = kernel


def safe_file(path, root):
  return path.is_file() and path.resolve().is_relative_to(root.resolve())


def read_file(path, root):
  if not safe_file(path, root):
    return {'missing_or_unsafe': True}
  with path.open('rb') as source:
    source.seek(max(0, path.stat().st_size - LIMIT))
    data = source.read(LIMIT)
  return {'text': redact(data.decode(errors='replace')), 'bytes': path.stat().st_size,
    'truncated': path.stat().st_size > LIMIT}


def network_profile(path, root):
  # Read only an explicit allowlist, never Wi-Fi/VPN secrets or entire files.
  if not safe_file(path, root) or path.stat().st_size > LIMIT:
    return {'missing_or_unsafe': True}
  parser = configparser.ConfigParser(interpolation=None, strict=False)
  parser.read(path)
  allowed = {'connection': {'id', 'type', 'interface-name', 'autoconnect'},
    'ipv4': {'method', 'address1', 'route1', 'never-default', 'dns'},
    'ipv6': {'method', 'address1', 'route1', 'never-default', 'dns'},
    'ethernet': {'mac-address', 'cloned-mac-address'}}
  return {section: {key: value for key, value in parser.items(section) if key in keys}
    for section, keys in allowed.items() if parser.has_section(section)}


def public_key_metadata(path, root):
  if not safe_file(path, root):
    return {'missing_or_unsafe': True}
  info = path.stat()
  value = {'mode': oct(stat.S_IMODE(info.st_mode)), 'uid': info.st_uid,
    'gid': info.st_gid, 'bytes': info.st_size}
  if path.name.endswith('.pub') or path.name == 'authorized_keys':
    result = subprocess.run(['ssh-keygen', '-E', 'sha256', '-lf', str(path)],
      text=True, capture_output=True, timeout=10, check=False)
    # ssh-keygen comments can contain user data; retain only SHA256 fingerprints.
    value['fingerprints'] = re.findall(r'SHA256:[A-Za-z0-9+/]+', result.stdout)
    value['fingerprint_exit_status'] = result.returncode
  return value


def collect():
  if os.geteuid() != 0 or required(['systemd-detect-virt', '--vm']) not in {'qemu', 'kvm'}:
    raise RuntimeError('Requires the disposable QEMU rescue guest')
  if not Path('/run/archiso/bootmnt/arch/x86_64/airootfs.sfs').is_file():
    raise RuntimeError('Requires the official live ISO')
  if not DEVICE.is_block_device() or required(['blockdev', '--getro', str(DEVICE)]) != '1':
    raise RuntimeError('Dedicated rescue block device must be read-only')
  layout = json.loads(required(['lsblk', '--json', '--paths', '--output',
    'NAME,TYPE,FSTYPE,RO', str(DEVICE)]))
  partitions = layout['blockdevices'][0].get('children', [])
  if any(not row['ro'] for row in partitions):
    raise RuntimeError('Every rescue partition must be read-only')
  result = {'schema_version': 1, 'measurement_valid': False,
    'purpose': 'Read-only failure forensics after the installation timer failed',
    'status': 'partial', 'block_read_only': True, 'block_devices': layout,
    'files': {}, 'commands': {}, 'command_failures': [], 'network_profiles': {}, 'ssh_keys': {}}
  MOUNT.mkdir()
  mounted = []
  try:
    roots = [row['name'] for row in partitions if row['fstype'] == 'btrfs']
    if len(roots) != 1:
      result['error'] = 'Expected one unencrypted Btrfs partition; no unlock or write attempted'
      return result
    top = MOUNT / 'btrfs'
    top.mkdir()
    # The standalone nologreplay alias was removed in Linux 6.16. The documented
    # rescue=nologreplay spelling retains the same no-replay, read-only scope:
    # https://btrfs.readthedocs.io/en/latest/Feature-by-version.html
    mount_argv = ['mount', '-t', 'btrfs', '-o', 'ro,rescue=nologreplay,subvolid=5,nosuid,nodev', roots[0], str(top)]
    result['mount_attempt'] = {'argv': mount_argv, 'read_only': True, 'log_replay': False}
    required(mount_argv)
    mounted.append(top)
    root = top / '@'
    if not root.is_dir():
      result['error'] = 'Installed @ subvolume is absent'
      return result
    result['commands']['mount'] = command(['findmnt', '--json', str(top)])
    result['commands']['subvolumes'] = command(['btrfs', 'subvolume', 'list', str(top)])
    for name in ('etc/fstab', 'etc/kernel/cmdline', 'etc/limine.conf', 'boot/limine.conf',
        'etc/ssh/sshd_config', 'etc/systemd/system/default.target', 'etc/ufw/ufw.conf',
        'etc/ufw/user.rules', 'etc/ufw/user6.rules', 'etc/ufw/before.rules', 'etc/default/ufw'):
      result['files'][name] = read_file(root / name, top)
    for path in sorted((root / 'etc/ssh/sshd_config.d').glob('*.conf'))[:50]:
      result['files'][str(path.relative_to(root))] = read_file(path, top)
    result['commands']['enabled_units'] = command(['systemctl', '--root', str(root),
      'list-unit-files', '--state=enabled,failed,masked', '--no-pager'])
    # sshd -T validates and prints effective settings; it does not start a daemon.
    # The target and every partition remain read-only, including inside chroot.
    result['commands']['sshd_effective'] = command(['chroot', str(root), '/usr/bin/sshd', '-T',
      '-C', 'user=omarchy,host=localhost,addr=127.0.0.1'])
    for path in sorted((root / 'etc/NetworkManager/system-connections').glob('*'))[:50]:
      result['network_profiles'][path.name] = network_profile(path, top)
    for path in sorted((root / 'etc/ssh').glob('ssh_host_*'))[:30]:
      result['ssh_keys'][str(path.relative_to(root))] = public_key_metadata(path, top)
    home = top / '@home' if (top / '@home').is_dir() else root / 'home'
    for path in sorted(home.glob('*/.ssh/authorized_keys'))[:10]:
      result['ssh_keys'][str(path.relative_to(top))] = public_key_metadata(path, top)
      for parent in (path.parent, path.parent.parent):
        info = parent.stat()
        result['ssh_keys'][str(parent.relative_to(top))] = {'mode': oct(stat.S_IMODE(info.st_mode)),
          'uid': info.st_uid, 'gid': info.st_gid}
    log_dirs = [root / 'var/log', top / '@log']
    for logs in log_dirs:
      for name in ('omarchy-install.log', 'omarchy-install-timing.json', 'archinstall/install.log'):
        path = logs / name
        if path.is_file():
          result['files'][str(path.relative_to(top))] = read_file(path, top)
      journal = logs / 'journal'
      if journal.is_dir():
        label = str(journal.relative_to(top))
        result['commands'][label + '/boots'] = command(['journalctl', '--directory', str(journal), '--list-boots', '--no-pager'])
        result['commands'][label + '/warnings'] = command(['journalctl', '--directory', str(journal), '-p', 'warning', '-n', '500', '--no-pager'])
        # Service-only filters can hide the boot job that prevents networking
        # and SSH from starting. Retain bounded latest-boot context as well;
        # the same credential redaction and byte cap apply to every command.
        result['commands'][label + '/latest_boot'] = command(['journalctl', '--directory', str(journal),
          '-b', '0', '-n', '2000', '--no-pager'])
        result['commands'][label + '/latest_boot_kernel'] = command(['journalctl', '--directory', str(journal),
          '-b', '0', '-k', '-n', '1000', '--no-pager'])
        result['commands'][label + '/boot_services'] = command(['journalctl', '--directory', str(journal), '-n', '500', '--no-pager',
          '-u', 'sshd', '-u', 'sshdgenkeys', '-u', 'NetworkManager', '-u', 'systemd-networkd', '-u', 'systemd-logind', '-u', 'ufw'])
    for row in partitions:
      if row['fstype'] != 'vfat':
        continue
      efi = MOUNT / 'efi'
      efi.mkdir()
      required(['mount', '-t', 'vfat', '-o', 'ro,nosuid,nodev,noexec', row['name'], str(efi)])
      mounted.append(efi)
      result['efi_files'] = [str(path.relative_to(efi)) for path in efi.rglob('*') if path.is_file()][:1000]
      for path in efi.rglob('limine.conf'):
        result['files']['efi/' + str(path.relative_to(efi))] = read_file(path, efi)
      break
    result['status'] = 'collected'
    return result
  except Exception as error:
    result['status'] = 'partial'
    if isinstance(error, CommandFailure):
      result['command_failures'].append(error.record)
      result['error'] = 'Required diagnostic command failed; see command_failures'
    else:
      result['error'] = redact(f'{type(error).__name__}: {error}')[-LIMIT:]
    mount_failure_diagnostics(result, partitions)
    return result
  finally:
    for target in reversed(mounted):
      try:
        required(['umount', str(target)])
      except CommandFailure as error:
        result['status'] = 'partial'
        result['command_failures'].append(error.record)


if __name__ == '__main__':
  print(json.dumps(collect(), indent=2))
