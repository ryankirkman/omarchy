#!/usr/bin/env python3
"""Exercise exact firewall shell writes inside a disposable guest's tiny chroot.

UFW/systemctl are recording dependencies: this verifies filesystem placement,
namespace isolation and original error logging, not live netfilter behavior.
"""
import __future__
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace


def sha(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--component', type=Path, required=True)
  parser.add_argument('--runtime', type=Path, required=True)
  parser.add_argument('--phases', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  if os.geteuid() != 0 or args.output.exists():
    parser.error('Requires a disposable root guest and fresh output')
  spec = importlib.util.spec_from_file_location('firewall_guest_patch', args.component / 'patch.py')
  patch = importlib.util.module_from_spec(spec); spec.loader.exec_module(patch)
  if sha(args.phases) != 'f5235ae1ed7e6a783978d2f51e49fc3e0d44c687f218967a599a104101e0070c':
    raise RuntimeError('Guest contract requires exact firewall phases')
  names = {'_firewall_overlap_sources', 'finalize_firewall'}
  nodes = [n for n in ast.parse(args.phases.read_bytes()).body if isinstance(n, ast.FunctionDef) and n.name in names]
  if {n.name for n in nodes} != names:
    raise RuntimeError('Required actual firewall helpers are missing')
  binaries = [Path(shutil.which(n)) for n in ('bash', 'env', 'date', 'mktemp', 'cat', 'sed', 'chmod', 'rm')]
  linked = ''.join(subprocess.run(['ldd', str(p)], check=True, text=True, capture_output=True).stdout for p in binaries)
  libraries = {Path(p) for p in re.findall(r'(/[^\s()]+)', linked)}
  if not libraries or len(libraries) > 20 or sum(p.stat().st_size for p in libraries | set(binaries)) > 32 * 1024**2:
    raise RuntimeError('Unexpected minimal shell dependency inventory')
  guest_before = {p: sha(Path(p)) if Path(p).is_file() else None for p in ('/etc/ufw/ufw.conf', '/etc/default/ufw')}
  report = {'schema_version': 1, 'scope': __doc__, 'phases_sha256': sha(args.phases),
    'target_disk_used': False, 'packages_installed': False, 'live_firewall_commands_executed': False,
    'guest_binary_sha256': {str(p): sha(p) for p in [*binaries, *sorted(libraries)]}, 'cases': []}
  with tempfile.TemporaryDirectory(prefix='omarchy-firewall-guest-', dir='/tmp') as temporary:
    work = Path(temporary)
    for status in (0, 23):
      target = work / ('target-' + str(status)); target.mkdir()
      for source in [*binaries, *sorted(libraries)]:
        destination = target / source.relative_to('/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination); destination.chmod(stat.S_IMODE(source.stat().st_mode))
      (target / 'bin').mkdir(exist_ok=True)
      if not (target / 'bin/bash').exists():
        shutil.copyfile(target / 'usr/bin/bash', target / 'bin/bash'); (target / 'bin/bash').chmod(0o755)
      for name in ('proc', 'dev', 'sys', 'run', 'tmp', 'etc/ufw'):
        (target / name).mkdir(parents=True, exist_ok=True)
      (target / 'tmp').chmod(0o1777)
      (target / 'etc/resolv.conf').touch()
      (target / 'etc/ufw/ufw.conf').write_text('ENABLED=no\n')
      for name, expected in patch.TARGET_SHA256.items():
        source = args.runtime / name
        if sha(source) != expected:
          raise RuntimeError('Guest runtime source drift: ' + name)
        destination = target / name; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination); destination.chmod(0o755 if name.startswith('usr/bin/') else 0o644)
      binary = target / 'usr/share/omarchy/bin'; binary.mkdir()
      for name, body in {
          'ufw': 'printf "ufw %s\\n" "$*" >>/etc/ufw/commands\nexit "$FIREWALL_STATUS"\n',
          'ufw-docker': 'PATH=/usr/bin:/bin\n[[ $1 == install ]]\n[[ $(ufw status) == "Status: active" ]]\nprintf "docker-rules\\n" >>/etc/ufw/commands\n',
          'systemctl': '[[ $* == "enable ufw" ]]\nprintf "enable-ufw\\n" >>/etc/ufw/commands\n',
        }.items():
        path = binary / name; path.write_text('#!/bin/bash\nset -euo pipefail\n' + body); path.chmod(0o755)
      ctx = SimpleNamespace(target=target, defer_provisioning=False, state={'firewall_overlap_pending': True})
      ns = {'hashlib': hashlib, 'os': os, 'FIREWALL_OVERLAP_TARGET_SHA256': patch.TARGET_SHA256}
      results = []
      def execute(context, command):
        argv = ['unshare', '--mount', '--propagation', 'private', '--fork', 'arch-chroot', str(target),
          'env', '-i', 'PATH=/usr/share/omarchy/bin:/usr/bin:/bin', 'OMARCHY_PATH=/usr/share/omarchy',
          'OMARCHY_INSTALL=/usr/share/omarchy/install', 'OMARCHY_INSTALL_USER=test-user',
          'OMARCHY_LOG_TO_STDOUT=1', 'FIREWALL_STATUS=' + str(status), *command]
        result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
        results.append(result); result.check_returncode()
      ns['_run_target_setup_command'] = execute
      exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual-firewall-guest-helper', 'exec',
        flags=__future__.annotations.compiler_flag), ns)
      try:
        ns['finalize_firewall'](ctx)
      except subprocess.CalledProcessError as error:
        if error.returncode != status or status == 0:
          raise
      if len(results) != 1 or results[0].returncode != status:
        raise RuntimeError('Unexpected exact leaf execution count or status')
      result = results[0]
      success = status == 0
      marker = ('Completed: ' if success else 'Failed: ') + '/usr/share/omarchy/install/config/firewall.sh'
      if marker not in result.stdout or (target / 'etc/ufw/ufw.conf').read_text() != ('ENABLED=yes\n' if success else 'ENABLED=no\n'):
        raise RuntimeError('Exact shell logging or configuration write differed: ' + result.stdout + result.stderr)
      if any(sha(target / n) != h for n, h in patch.TARGET_SHA256.items()):
        raise RuntimeError('A target package file changed')
      if subprocess.run(['findmnt', '-rn', '--mountpoint', str(target)], capture_output=True).returncode == 0:
        raise RuntimeError('Private target mount escaped to parent')
      report['cases'].append({'status': status, 'passed': True, 'target_package_bytes_unchanged': True,
        'pending_required_setup': ctx.state.get('firewall_overlap_pending', False),
        'ufw_configuration': (target / 'etc/ufw/ufw.conf').read_text(),
        'command_intents': (target / 'etc/ufw/commands').read_text().splitlines(),
        'stdout': result.stdout, 'stderr': result.stderr, 'parent_target_mount_absent': True})
    report['fixture'] = str(work)
  report['fixture_removed'] = not Path(report['fixture']).exists()
  report['guest_ufw_configuration_unchanged'] = guest_before == {p: sha(Path(p)) if Path(p).is_file() else None for p in guest_before}
  if not report['guest_ufw_configuration_unchanged']:
    raise RuntimeError('Guest base UFW configuration changed')
  report['status'] = 'passed'
  args.output.write_text(json.dumps(report, indent=2) + '\n')
  print(json.dumps(report, indent=2))


if __name__ == '__main__':
  main()
