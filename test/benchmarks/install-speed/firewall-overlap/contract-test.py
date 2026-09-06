#!/usr/bin/env python3
"""Check exact firewall scheduling with temporary, controlled shell dependencies."""
import __future__
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch as mock

HERE = Path(__file__).resolve().parent


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def digest(data):
  return hashlib.sha256(data).hexdigest()


firewall = load('firewall_contract_patch', HERE / 'patch.py')
localdb = load('firewall_localdb_patch', HERE.parent / 'localdb-overlap/patch.py')


class FirewallContract(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.temporary = tempfile.TemporaryDirectory(prefix='omarchy-firewall-contract-', dir='/tmp')
    cls.addClassCleanup(cls.temporary.cleanup)
    cls.work = Path(cls.temporary.name)
    cls.producer = load('firewall_contract_producer', HERE / 'prepare-payload.py')
    sys.path.insert(0, str(HERE.parent / 'animation-overlap'))
    sys.path.insert(0, str(HERE.parent / 'image'))
    producers = (
      ('fast-reboot/prepare-payload.py', None), ('image/direct-restore-payload.py', None),
      ('localdb-overlap/prepare-payload.py', None),
      ('animation-overlap/prepare-payload.py', HERE.parent / 'localdb-overlap/preflight.sh'),
    )
    base = cls.work / 'ordinary'
    base.mkdir()
    (base / 'unchanged').write_bytes(b'ordinary payload remains unchanged\n')
    (base / 'unchanged').chmod(0o640)
    for index, (name, preflight) in enumerate(producers):
      producer = load('firewall_base_' + str(index), HERE.parent / name)
      output = cls.work / ('base-' + str(index))
      args = (ISO_SOURCE, base, output) if preflight is None else (ISO_SOURCE, base, preflight, output)
      producer.prepare(*args)
      base = output
    cls.base_payload = base
    cls.base_preflight = HERE.parent / 'animation-overlap/preflight.sh'
    cls.base = (base / cls.producer.BASE_PHASES).read_bytes()
    cls.patched = firewall.patch_source(cls.base)
    cls.output = cls.work / 'firewall-payload'
    cls.manifest = cls.producer.prepare(ISO_SOURCE, base, cls.base_preflight, cls.output)
    cls.runtime = {}
    for name, expected in (localdb.TARGET_SHA256 | firewall.TARGET_SHA256).items():
      path = HERE / 'fixtures/runtime' / name
      if not path.exists():
        path = HERE.parent / 'localdb-overlap/fixtures/runtime' / name
      data = path.read_bytes()
      if digest(data) != expected:
        raise AssertionError('Exact ISO runtime fixture differs: ' + name)
      cls.runtime[name] = data

  def namespace(self):
    names = {'_localdb_overlap_sources', '_localdb_overlap_system_command', 'finalize_localdb',
      '_firewall_overlap_sources', '_firewall_overlap_system_command', 'finalize_firewall',
      'run_system_finalizer', '_run_finalization_branch', 'finalize_boot_and_user_setup'}
    nodes = [n for n in ast.parse(self.patched).body if isinstance(n, ast.FunctionDef) and n.name in names]
    self.assertEqual({n.name for n in nodes}, names)
    namespace = {'hashlib': hashlib, 'os': os, 'time': time, 'ThreadPoolExecutor': ThreadPoolExecutor,
      'shutil': SimpleNamespace(which=lambda command: '/usr/bin/unshare'), 'info': lambda message: None,
      'LOCALDB_OVERLAP_TARGET_SHA256': dict(localdb.TARGET_SHA256),
      'FIREWALL_OVERLAP_TARGET_SHA256': dict(firewall.TARGET_SHA256),
      'TARGET_DEFERRED_BOOT_HOOKS': (), '_mask_mkinitcpio_pacman_hooks': lambda *args: None,
      '_unmask_mkinitcpio_pacman_hooks': lambda *args: None,
      '_run_target_setup_command': lambda *args: self.fail('Unexpected setup execution')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual-firewall-branches', 'exec',
      flags=__future__.annotations.compiler_flag), namespace)
    return namespace

  def fixture(self, deferred=False):
    box = Path(tempfile.mkdtemp(dir=self.work))
    target = box / 'target'
    for name, data in self.runtime.items():
      path = target / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_bytes(data)
      path.chmod(0o755 if name == 'usr/bin/omarchy-apply-system' else 0o644)
    return box, SimpleNamespace(target=target, username='test-user', defer_provisioning=deferred, state={}), self.namespace()

  def shell_fixture(self, deferred=False, status=0):
    box, ctx, ns = self.fixture(deferred)
    runtime = ctx.target / 'usr/share/omarchy'
    install = runtime / 'install'
    binary = runtime / 'bin'
    binary.mkdir()
    (box / 'tmp').mkdir()
    (box / 'trace').write_text('')
    (box / 'ufw.conf').write_text('ENABLED=no\n')
    for line in self.runtime['usr/share/omarchy/install/config/all.sh'].decode().splitlines():
      relative = line.split('$OMARCHY_INSTALL/', 1)[1].split('"', 1)[0]
      if relative != 'config/firewall.sh':
        (install / relative).write_text('printf "config\\n" >>"$BOX/trace"\n')
    for name in ('login/all.sh', 'post-install/pacman.sh', 'post-install/udev.sh'):
      path = install / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text('printf "other-setup\\n" >>"$BOX/trace"\n')
    commands = {
      'getent': '[[ $1 == passwd && $2 == test-user ]]\nprintf "test-user:x:1000:1000::/home/test-user:/bin/bash\\n"\n',
      'omarchy-apply-hardware': 'printf "hardware %s\\n" "$*" >>"$BOX/trace"\n',
      'ufw': 'printf "ufw %s\\n" "$*" >>"$BOX/trace"\nexit "$FIREWALL_STATUS"\n',
      'ufw-docker': 'PATH=/usr/bin:/bin\n[[ $1 == install ]]\n[[ $(ufw status) == "Status: active" ]]\nprintf "docker-rules\\n" >>"$BOX/trace"\n',
      'systemctl': '[[ $* == "enable ufw" ]]\nprintf "firewall-finished\\n" >>"$BOX/trace"\n',
      'updatedb': 'printf "updatedb\\n" >>"$BOX/trace"\n',
      # The only absolute target write in the exact shell is redirected into
      # our owned fixture; no host firewall command or service is executed.
      'sed': 'if [[ ${1:-} == -i ]]; then\n [[ $3 == /etc/ufw/ufw.conf ]]\n exec /usr/bin/sed "$1" "$2" "$BOX/ufw.conf"\nfi\nexec /usr/bin/sed "$@"\n',
    }
    for name, body in commands.items():
      path = binary / name
      path.write_text('#!/bin/bash\nset -euo pipefail\n' + body)
      path.chmod(0o755)
    env = dict(os.environ, BOX=str(box), TMPDIR=str(box / 'tmp'), FIREWALL_STATUS=str(status),
      OMARCHY_PATH=str(runtime), OMARCHY_INSTALL=str(install), OMARCHY_INSTALL_USER=ctx.username,
      OMARCHY_INSTALL_LOG_FILE=str(box / 'install.log'), OMARCHY_LOG_TO_STDOUT='1',
      PATH=str(binary) + ':/usr/bin:/bin')
    records = []
    def execute(context, command):
      argv = command
      if command[0] == '/usr/bin/omarchy-apply-system':
        argv = ['bash', '-c', self.runtime[command[0].lstrip('/')].decode(), *command]
      result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=10)
      records.append((command, result))
      result.check_returncode()
    ns['_run_target_setup_command'] = execute
    return box, ctx, ns, records

  def test_exact_source_and_only_scheduling_changes(self):
    self.assertEqual(digest(self.base), firewall.SOURCE_SHA256)
    before = {n.name: ast.dump(n) for n in ast.parse(self.base).body if isinstance(n, ast.FunctionDef)}
    after = {n.name: ast.dump(n) for n in ast.parse(self.patched).body if isinstance(n, ast.FunctionDef)}
    self.assertEqual(set(after) - set(before), {'_firewall_overlap_sources', '_firewall_overlap_system_command', 'finalize_firewall'})
    self.assertEqual({n for n in before if before[n] != after[n]}, {'run_system_finalizer', 'finalize_boot_and_user_setup'})
    for source in (self.base + b'\n', self.patched):
      with self.assertRaises(ValueError):
        firewall.patch_source(source)
    for anchor in (b'def run_system_finalizer(ctx: InstallContext) -> None:\n',
        b'        _run_target_setup_command(ctx, _localdb_overlap_system_command(ctx, cmd))\n',
        b'            ("Finalizing user", run_chroot_finalizer),\n'):
      drift = self.base.replace(anchor, b'')
      with mock.object(firewall, 'SOURCE_SHA256', digest(drift)), self.assertRaises(ValueError):
        firewall.patch_source(drift)

  def test_target_guard_rejects_each_drift_before_execution(self):
    for name in firewall.TARGET_SHA256:
      box, ctx, ns = self.fixture()
      (ctx.target / name).write_bytes(self.runtime[name] + b'\n')
      calls = []
      ns['_run_target_setup_command'] = lambda *args: calls.append(args)
      with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'target source differs'):
        ns['run_system_finalizer'](ctx)
      self.assertEqual(calls, [])
      self.assertNotIn('firewall_overlap_pending', ctx.state)
    for kind in ('missing', 'symlink', 'anchor'):
      box, ctx, ns = self.fixture()
      path = ctx.target / 'usr/share/omarchy/install/config/all.sh'
      if kind == 'missing':
        path.unlink()
      elif kind == 'symlink':
        original = box / 'original'; original.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(original)
      else:
        data = path.read_bytes().replace(b'run_logged "$OMARCHY_INSTALL/config/firewall.sh"\n', b'')
        path.write_bytes(data)
        ns['FIREWALL_OVERLAP_TARGET_SHA256']['usr/share/omarchy/install/config/all.sh'] = digest(data)
      with self.subTest(kind=kind), self.assertRaises(RuntimeError):
        ns['run_system_finalizer'](ctx)

  def test_real_shell_preserves_commands_logging_target_bytes_and_once_only(self):
    box, ctx, ns, records = self.shell_fixture()
    original = {name: (ctx.target / name).read_bytes() for name in self.runtime}
    ns['run_system_finalizer'](ctx)
    self.assertFalse(any(x.startswith('ufw ') for x in (box / 'trace').read_text().splitlines()))
    self.assertEqual(records[0][0][3:], ['/usr/bin/omarchy-apply-system', '--install-user', 'test-user', '--first-install'])
    ns['finalize_firewall'](ctx)
    trace = (box / 'trace').read_text().splitlines()
    self.assertEqual([x for x in trace if x.startswith('ufw ')], [
      'ufw default deny incoming', 'ufw default allow outgoing', 'ufw allow 53317/udp', 'ufw allow 53317/tcp',
      'ufw allow in proto udp from 172.16.0.0/12 to 172.17.0.1 port 53 comment allow-docker-dns',
      'ufw allow in proto udp from 192.168.0.0/16 to 172.17.0.1 port 53 comment allow-docker-dns'])
    self.assertEqual(trace[-2:], ['docker-rules', 'firewall-finished'])
    self.assertEqual((box / 'ufw.conf').read_text(), 'ENABLED=yes\n')
    self.assertEqual(list((box / 'tmp').iterdir()), [])
    output = records[-1][1].stdout
    leaf = str(ctx.target / 'usr/share/omarchy/install/config/firewall.sh')
    self.assertIn('Starting: ' + leaf, output)
    self.assertIn('Completed: ' + leaf, output)
    self.assertEqual({name: (ctx.target / name).read_bytes() for name in self.runtime}, original)
    with self.assertRaisesRegex(RuntimeError, 'no pending'):
      ns['finalize_firewall'](ctx)
    ns['finalize_localdb'](ctx)
    self.assertEqual((box / 'trace').read_text().splitlines()[-1], 'updatedb')

  def test_deferred_mode_keeps_original_serial_firewall_and_index(self):
    box, ctx, ns, records = self.shell_fixture(deferred=True)
    command = ['/usr/bin/omarchy-apply-system', '--defer-provisioning', '--first-install']
    self.assertIs(ns['_firewall_overlap_system_command'](ctx, command), command)
    ns['run_system_finalizer'](ctx)
    self.assertEqual(records[0][0], command)
    trace = (box / 'trace').read_text().splitlines()
    self.assertLess(trace.index('firewall-finished'), trace.index('hardware --defer-provisioning'))
    self.assertEqual(trace[-1], 'updatedb')
    ns['finalize_firewall'](ctx); ns['finalize_localdb'](ctx)
    self.assertEqual(len(records), 1)
    self.assertNotIn('firewall_overlap_pending', ctx.state)

  def test_branches_join_firewall_failure_before_returning_and_skip_user_index(self):
    box, ctx, ns, records = self.shell_fixture(status=23)
    ns['run_system_finalizer'](ctx)
    boot_started, release_boot = threading.Event(), threading.Event()
    def boot(context):
      boot_started.set()
      if not release_boot.wait(5):
        raise RuntimeError('test did not release boot')
    ns['finalize_limine_boot'] = boot
    unexpected = []
    for name in ('run_chroot_finalizer', 'configure_login', 'configure_ssh_access', 'configure_tailscale', 'configure_dns_resolver'):
      ns[name] = lambda ctx, name=name: unexpected.append(name)
    with ThreadPoolExecutor(max_workers=1) as executor:
      future = executor.submit(ns['finalize_boot_and_user_setup'], ctx)
      try:
        self.assertTrue(boot_started.wait(3))
        self.assertFalse(future.done())
      finally:
        release_boot.set()
      with self.assertRaisesRegex(RuntimeError, 'parallel finalization failed .*Configuring firewall'):
        future.result(timeout=5)
    self.assertEqual(unexpected, [])
    self.assertIn('firewall.sh (exit code: 23)', records[-1][1].stdout)
    self.assertNotIn('Completed: ' + str(ctx.target / 'usr/share/omarchy/install/config/firewall.sh'), records[-1][1].stdout)
    self.assertNotIn('updatedb', (box / 'trace').read_text().splitlines())
    self.assertTrue(ctx.state['firewall_overlap_pending'])
    self.assertEqual(ctx.state['phase_substeps'][-1]['status'], 'failed')

  def test_successful_branch_order_retains_index_after_all_firewall_writes(self):
    box, ctx, ns, records = self.shell_fixture()
    ns['run_system_finalizer'](ctx)
    ns['finalize_limine_boot'] = lambda context: None
    def step(context, name):
      trace = (box / 'trace').read_text().splitlines()
      self.assertIn('firewall-finished', trace)
      with (box / 'trace').open('a') as out:
        out.write(name + '\n')
    for name in ('run_chroot_finalizer', 'configure_login', 'configure_ssh_access', 'configure_tailscale', 'configure_dns_resolver'):
      ns[name] = lambda context, name=name: step(context, name)
    ns['finalize_boot_and_user_setup'](ctx)
    trace = (box / 'trace').read_text().splitlines()
    self.assertEqual(trace[-6:], ['run_chroot_finalizer', 'configure_login', 'configure_ssh_access',
      'configure_tailscale', 'configure_dns_resolver', 'updatedb'])
    user = [x for x in ctx.state['phase_substeps'] if x['branch'] == 'user']
    self.assertEqual(user[0]['name'], 'Configuring firewall')
    self.assertEqual(user[-1]['name'], 'Indexing installed files')
    self.assertTrue(all(x['status'] == 'ok' and x['elapsed'] >= 0 for x in user))

  def test_payload_inventory_and_preflight_reject_drift_and_retain_inherited_files(self):
    manifest = self.manifest
    self.assertEqual(manifest['firewall_phases_sha256'], digest(self.patched))
    self.assertEqual(manifest['target_source_sha256'], firewall.TARGET_SHA256)
    self.assertEqual(manifest['files'], self.producer.inventory(self.output))
    for item in self.producer.inventory(self.base_payload):
      self.assertEqual((self.output / item['path']).read_bytes(), (self.base_payload / item['path']).read_bytes())
      self.assertEqual((self.output / item['path']).stat().st_mode, (self.base_payload / item['path']).stat().st_mode)
    for kind in ('base', 'preflight', 'source', 'component'):
      box = Path(tempfile.mkdtemp(dir=self.work)); base = box / 'base'
      shutil.copytree(self.base_payload, base)
      meta = json.loads(self.base_payload.with_name(self.base_payload.name + '.manifest.json').read_text())
      preflight = box / 'preflight.sh'; preflight.write_bytes(self.base_preflight.read_bytes())
      if kind == 'base': (base / 'unchanged').write_text('drift')
      elif kind == 'preflight': preflight.write_text('drift')
      elif kind == 'source':
        (base / self.producer.BASE_PHASES).write_text('drift'); meta['files'] = self.producer.inventory(base)
      else: meta['component'] = 'other'
      base.with_name(base.name + '.manifest.json').write_text(json.dumps(meta))
      with self.subTest(kind=kind), self.assertRaises(ValueError):
        self.producer.prepare(ISO_SOURCE, base, preflight, box / 'output')
      self.assertFalse((box / 'output').exists())
    for case in ('success', 'corrupt', 'drifted-base', 'failed-base'):
      box = Path(tempfile.mkdtemp(dir=self.work)); staged = box / 'staged'
      shutil.copytree(self.output / self.producer.PAYLOAD_PATH, staged)
      (box / 'base.py').write_bytes(self.base)
      (box / 'calls').write_text('')
      (staged / 'base-preflight.sh').write_text('''#!/bin/bash
set -euo pipefail
echo inherited >>"$BOX/calls"
cp "$BOX/base.py" "$BOX/live.py"
if [[ $CASE == drifted-base ]]; then echo drift >>"$BOX/live.py"; fi
if [[ $CASE == failed-base ]]; then exit 37; fi
''')
      names = [line.split('  ', 1)[1] for line in (staged / 'payload.sha256').read_text().splitlines()]
      (staged / 'payload.sha256').write_text(''.join(f'{digest((staged / name).read_bytes())}  {name}\n' for name in names))
      if case == 'corrupt': (staged / 'phases_impl.py').write_text('corrupt')
      script = (HERE / 'preflight.sh').read_text().replace(
        'payload=/usr/local/lib/omarchy-benchmark/firewall-overlap', 'payload=' + shlex.quote(str(staged))).replace(
        'live=/usr/share/omarchy-iso/orchestrator/phases_impl.py', 'live=' + shlex.quote(str(box / 'live.py')))
      result = subprocess.run(['bash'], input=script, text=True, capture_output=True, timeout=10,
        env=dict(os.environ, BOX=str(box), CASE=case, PATH='/usr/bin:/bin'))
      self.assertEqual((box / 'calls').read_text().splitlines(), [] if case == 'corrupt' else ['inherited'])
      if case == 'success':
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((box / 'live.py').read_bytes(), self.patched)
      else:
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--iso-source', type=Path, required=True)
  args, remaining = parser.parse_known_args()
  ISO_SOURCE = args.iso_source.resolve()
  unittest.main(argv=[sys.argv[0], *remaining])
