#!/usr/bin/env python3
"""Check native benchmark process cleanup and evidence boundaries."""
import importlib.util
import contextlib
import io
import json
import os
import selectors
import stat
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
repeat_spec = importlib.util.spec_from_file_location('native_repeat_contract', DIRECTORY.parent / 'repeat-installs.py')
repeat = importlib.util.module_from_spec(repeat_spec)
repeat_spec.loader.exec_module(repeat)


def failed_repeat_fixture(root, revision='candidate'):
  repo = DIRECTORY.parents[3]
  work, evidence = root / 'work', root / 'evidence'
  run_root, series_evidence = work / 'fresh-installs-fixture', evidence / 'repetitions'
  order = repeat.schedule(3, 'control')
  selected = order[0 if revision == 'control' else 1]
  run = run_root / selected['name']
  run.mkdir(parents=True)
  (run / 'target.qcow2').write_bytes(b'Disposable test fixture; never passed to QEMU')
  for name in ('supervisor.pid', 'qemu.pid'):
    (run / name).write_text('2147483647\n')
  module.save_json(run / 'manifest.json', {'status': 'timeout', 'mode': 'install', 'fresh_target': True,
    'qemu_argv': ['qemu-system-x86_64', '-drive',
      f'file={run / "target.qcow2"},format=qcow2,if=none,id=target,cache=writeback']})
  bench = repo / 'test/benchmarks'
  provenance = {'runner_sha256': module.digest(bench / 'iso-vm.py'),
    'comparator_sha256': module.digest(bench / 'compare-installs.py'),
    'repeat_driver_sha256': module.digest(bench / 'install-speed/repeat-installs.py')}
  failed_evidence = series_evidence / 'failed-runs' / run.name
  repeat.retain_failed_run(run, failed_evidence, 'fixture installation timeout', 1, provenance)
  series = {'status': 'failed', 'failure': 'fixture installation timeout',
    'failed_run_evidence': str(failed_evidence),
    'runs': [] if revision == 'control' else [order[0]],
    'plan': {'run_root': str(run_root), 'evidence_root': str(series_evidence),
      'order': order, 'source_provenance': provenance}}
  module.save_json(series_evidence / 'series.json', series)
  context = {'repo': repo, 'work': work, 'evidence': evidence, 'iso': work / 'official.iso',
    'harness': work / 'iso-harness', 'kernel': work / 'kernel', 'initrd': work / 'initrd'}
  return run, run_root, series_evidence, context


class NativeContract(unittest.TestCase):
  def test_timeout_terminates_child_group(self):
    # /proc may expose a different PID namespace. Only our inherited writers
    # determine EOF, and the ready marker proves the child actually started.
    child_script = "import os,sys,time; os.write(int(sys.argv[1]),b'ready\\n'); time.sleep(30)"
    parent_script = "import subprocess,sys,time; fd=int(sys.argv[1]); subprocess.Popen([sys.executable,'-c',sys.argv[2],str(fd)],pass_fds=(fd,)); time.sleep(30)"
    for negative_control in (True, False):
      with self.subTest(negative_control=negative_control):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb', buffering=0) as reader, \
            os.fdopen(write_fd, 'wb', buffering=0) as writer, selectors.DefaultSelector() as selector:
          selector.register(reader, selectors.EVENT_READ)
          if negative_control:
            child = subprocess.Popen([sys.executable, '-c', child_script, str(write_fd)], pass_fds=(write_fd,))
            try:
              writer.close()
              self.assertTrue(selector.select(3), 'owned child did not report ready')
              self.assertEqual(reader.read(1024), b'ready\n')
              self.assertIsNone(child.poll())
              self.assertEqual(selector.select(0.1), [], 'live owned child must keep the pipe open')
            finally:
              child.kill()
              child.wait(timeout=3)
            expected = b''
          else:
            with self.assertRaises(subprocess.TimeoutExpired):
              module.command([sys.executable, '-c', parent_script, str(write_fd), child_script],
                timeout=1, pass_fds=(write_fd,))
            writer.close()
            expected = b'ready\n'
          received = bytearray()
          deadline = time.monotonic() + 3
          while True:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, 'owned writer survived cleanup')
            self.assertTrue(selector.select(remaining), 'owned writer survived cleanup')
            chunk = reader.read(1024)
            if not chunk:
              break
            received.extend(chunk)
          self.assertEqual(received, expected)

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
          'standalone-btrfs-subvolumes.txt', 'standalone-uki-files.txt',
          'standalone-last-failed-ssh-probe.json', 'standalone-timeout-diagnostics.json',
          'standalone-timeout-before-keys.png', 'standalone-timeout-after-escape.png',
          'standalone-timeout-after-tty2.png', 'timeout-console.log', 'timeout-console.json',
          'standalone-timeout-console.log', 'standalone-timeout-console.json', 'id_ed25519'):
        (run / name).write_text('fixture')
      module.collect_small(run, output)
      self.assertEqual(len(list(output.iterdir())), 18)
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
    self.assertEqual(firmware, ['--iso', derived, '--verify-standalone-reboot',
      '--standalone-reboot-timeout', '600'])
    for method in ('firmware', 'direct'):
      builder = module.boot_arguments(method, source, kernel, initrd, builder=True)
      self.assertEqual(builder, ['--iso', source, '--kernel', kernel, '--initrd', initrd, '--append', module.CMDLINE])
    with self.assertRaisesRegex(ValueError, 'verified derived ISO'):
      module.boot_arguments('firmware', source, kernel, initrd)

  def test_install_timeout_is_shared_and_leaves_builder_unchanged(self):
    base = ['--repo', '/repo', '--work', '/new-work', '--evidence', '/evidence']
    self.assertEqual(module.parse_arguments(base).install_timeout, 1800)
    selected = module.parse_arguments(base + ['--install-timeout', '600'])
    self.assertEqual(selected.install_timeout, 600)
    self.assertEqual(module.timeout_arguments(selected.install_timeout), ['--timeout', '600'])
    self.assertEqual(module.timeout_arguments(selected.install_timeout, builder=True), ['--timeout', '5400'])
    for invalid in ('0', '-1', '600.5', 'invalid'):
      with contextlib.redirect_stderr(io.StringIO()):
        with self.assertRaises(SystemExit) as result:
          module.parse_arguments(base + ['--install-timeout', invalid])
      self.assertEqual(result.exception.code, 1)

  def test_standalone_timeout_is_shared_and_leaves_builder_unchanged(self):
    base = ['--repo', '/repo', '--work', '/new-work', '--evidence', '/evidence']
    self.assertEqual(module.parse_arguments(base).standalone_reboot_timeout, 600)
    selected = module.parse_arguments(base + ['--standalone-reboot-timeout', '180'])
    self.assertEqual(selected.standalone_reboot_timeout, 180)
    source, kernel = Path('/original.iso'), Path('/kernel')
    for label in ('calibration', 'control', 'candidate'):
      initrd, derived = Path(f'/{label}.img'), Path(f'/{label}.iso')
      argv = module.boot_arguments('firmware', source, kernel, initrd, firmware_iso=derived,
        standalone_reboot_timeout=selected.standalone_reboot_timeout)
      self.assertEqual(argv, ['--iso', derived, '--verify-standalone-reboot',
        '--standalone-reboot-timeout', '180'])
    for method in ('direct', 'firmware'):
      builder = module.boot_arguments(method, source, kernel, initrd, builder=True,
        standalone_reboot_timeout=selected.standalone_reboot_timeout)
      self.assertEqual(builder, ['--iso', source, '--kernel', kernel, '--initrd', initrd,
        '--append', module.CMDLINE])
    for invalid in ('0', '-1', '180.5', 'invalid'):
      with contextlib.redirect_stderr(io.StringIO()):
        with self.assertRaises(SystemExit) as result:
          module.parse_arguments(base + ['--standalone-reboot-timeout', invalid])
      self.assertEqual(result.exception.code, 1)

  def test_diagnostic_calibration_cli_requires_bounded_count_cold_firmware(self):
    base = ['--repo', '/repo', '--work', '/new-work', '--evidence', '/evidence']
    self.assertIsNone(module.parse_arguments(base).diagnostic_calibrations)
    for count in ('1', '6'):
      parsed = module.parse_arguments(base + ['--diagnostic-calibrations', count,
        '--boot-method', 'firmware', '--source-cache', 'cold'])
      self.assertEqual(parsed.diagnostic_calibrations, int(count))
    invalid = [['--diagnostic-calibrations', value, '--boot-method', 'firmware', '--source-cache', 'cold']
      for value in ('0', '-1', '7', '1.5', 'invalid')]
    invalid += [['--diagnostic-calibrations', '2'] + flags
      for flags in ([], ['--boot-method', 'firmware'], ['--source-cache', 'cold'])]
    for flags in invalid:
      with contextlib.redirect_stderr(io.StringIO()):
        with self.assertRaises(SystemExit) as result:
          module.parse_arguments(base + flags)
      self.assertEqual(result.exception.code, 1)

  def test_diagnostic_calibrations_use_distinct_stock_runs_and_stop_before_build(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      control = root / 'control.img'
      provenance = {'comparisons': {}, **module.diagnostic_provenance(3)}
      calls = []
      def install(name, selected):
        self.assertEqual(selected, control)
        run = root / name
        run.mkdir()  # Reusing a name fails this contract.
        calls.append(name)
        return run
      self.assertIsNone(module.calibration_stage(install, control, 3, provenance, root))
      expected = ['diagnostic-calibration-01', 'diagnostic-calibration-02', 'diagnostic-calibration-03']
      self.assertEqual(calls, expected)
      saved = json.loads((root / 'experiment.json').read_text())
      self.assertFalse(saved['measurement_valid'])
      self.assertEqual(saved['status'], 'diagnostic-complete')
      self.assertEqual(saved['diagnostic_calibrations']['requested_count'], 3)
      self.assertEqual(saved['diagnostic_calibrations']['completed_names'], expected)
      self.assertEqual(saved['comparisons'], {})
      self.assertEqual(saved['variants'], [])
      self.assertEqual(saved['pairs_per_variant'], 0)
      self.assertEqual(module.calibration_stage(install, control, None, {}, root), root / 'calibration')
      self.assertEqual(calls[-1], 'calibration')

  def test_diagnostic_first_failure_propagates_without_retry_or_later_run(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      provenance = {'comparisons': {}, **module.diagnostic_provenance(4)}
      failure = RuntimeError('retained failed installation')
      calls = []
      def install(name, selected):
        calls.append(name)
        if len(calls) == 2:
          raise failure
        return root / name
      with self.assertRaises(RuntimeError) as caught:
        module.calibration_stage(install, root / 'control.img', 4, provenance, root)
      self.assertIs(caught.exception, failure)
      self.assertEqual(calls, ['diagnostic-calibration-01', 'diagnostic-calibration-02'])
      saved = json.loads((root / 'experiment.json').read_text())
      self.assertEqual(saved['status'], 'diagnostic-failed')
      self.assertEqual(saved['diagnostic_calibrations']['completed_names'], calls[:1])
      self.assertEqual(saved['diagnostic_calibrations']['failed_name'], calls[1])
      self.assertFalse(saved['measurement_valid'])
      self.assertEqual(saved['comparisons'], {})

  def test_diagnostic_execute_returns_before_image_preparation(self):
    # Exercise the real execute boundary with only host/KVM preparation
    # replaced. Diagnostic sequencing and failure handling are checked above.
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      args = module.parse_arguments(['--repo', str(DIRECTORY.parents[3]), '--work', str(root / 'work'),
        '--evidence', str(root / 'evidence'), '--boot-method', 'firmware', '--source-cache', 'cold',
        '--diagnostic-calibrations', '2', '--variants', module.FINALIZATION_OVERLAP_VARIANT])
      def prepare(argv, **kwargs):
        if Path(str(argv[1])).name == 'make-initramfs.py':
          output = Path(argv[argv.index('--output') + 1])
          output.write_bytes(b'diagnostic control fixture')
          module.save_json(output.with_suffix(output.suffix + '.manifest.json'), {})
        return 0
      original_open, original_close = os.open, os.close
      def open_kvm(path, *args, **kwargs):
        return 1000000 if path == '/dev/kvm' else original_open(path, *args, **kwargs)
      def close_kvm(fd):
        if fd not in (1000000, 1000001):
          original_close(fd)
      with patch.object(module.os, 'open', side_effect=open_kvm), patch.object(module.os, 'close', side_effect=close_kvm), \
          patch.object(module.fcntl, 'ioctl', side_effect=[12, 1000001]), \
          patch.object(module, 'disk_budget', return_value={}), patch.object(module, 'git_checkout'), \
          patch.object(module.subprocess, 'check_output', return_value='fixture-commit\n'), \
          patch.object(module, 'digest', side_effect=lambda path: module.ISO_SHA256 if path.suffix == '.iso' else module.INITRD_SHA256), \
          patch.object(module, 'command', side_effect=prepare) as commands, \
          patch.object(module, 'validate_finalization_overlap') as validate, \
          patch.object(module, 'calibration_stage', return_value=None) as stage:
        self.assertEqual(module.execute(args), 0)
      validate.assert_not_called()
      stage.assert_called_once()
      self.assertEqual(stage.call_args.args[2], 2)
      self.assertFalse(stage.call_args.args[3]['measurement_valid'])
      invoked = [str(call.args[0]) for call in commands.call_args_list]
      self.assertFalse(any('prepare-bundles.py' in argv or 'repeat-installs.py' in argv or 'qemu-img' in argv for argv in invoked))

  def test_firmware_disk_headroom_fails_before_preparation(self):
    with patch.object(module.shutil, 'disk_usage', return_value=SimpleNamespace(free=43 * 1024**3)):
      with self.assertRaisesRegex(RuntimeError, '44 GiB'):
        module.disk_budget(Path('/tmp'), 'firmware')
      self.assertEqual(module.disk_budget(Path('/tmp'), 'direct')['minimum_free_bytes'], 28 * 1024**3)
    with patch.object(module.shutil, 'disk_usage', return_value=SimpleNamespace(free=44 * 1024**3)):
      self.assertEqual(module.disk_budget(Path('/tmp'), 'firmware')['minimum_free_bytes'], 44 * 1024**3)

  def test_early_verify_requires_cold_firmware_and_preserves_defaults(self):
    base = ['--repo', '/repo', '--work', '/new-work', '--evidence', '/evidence']
    for variant in module.EARLY_VARIANTS:
      selected = base + ['--variants', variant]
      for options in ([], ['--source-cache', 'cold'], ['--boot-method', 'firmware']):
        with contextlib.redirect_stderr(io.StringIO()):
          with self.assertRaises(SystemExit) as result:
            module.parse_arguments(selected + options)
        self.assertEqual(result.exception.code, 1)
      accepted = module.parse_arguments(selected + ['--source-cache', 'cold', '--boot-method', 'firmware'])
      self.assertEqual(accepted.variants, [variant])
    self.assertEqual(module.parse_arguments(base).variants, ['upstream-image', 'image-no-package-prefetch'])
    with patch.object(module.shutil, 'disk_usage', return_value=SimpleNamespace(free=61 * 1024**3)):
      self.assertEqual(module.disk_budget(Path('/tmp'), 'firmware', accepted.variants)['minimum_free_bytes'], 44 * 1024**3)
      with self.assertRaisesRegex(RuntimeError, '62 GiB'):
        module.disk_budget(Path('/tmp'), 'firmware', ['upstream-image', 'image-no-package-prefetch',
          'image-no-package-prefetch-fast-reboot', module.EARLY_VERIFY_VARIANT])
      self.assertEqual(module.disk_budget(Path('/tmp'), 'firmware',
        [module.EARLY_VERIFY_VARIANT, module.DIRECT_RESTORE_VARIANT])['minimum_free_bytes'], 50 * 1024**3)
      self.assertEqual(module.disk_budget(Path('/tmp'), 'firmware',
        [module.DIRECT_RESTORE_VARIANT, module.FINALIZATION_OVERLAP_VARIANT])['minimum_free_bytes'], 50 * 1024**3)

  def test_overlap_layers_before_activation_and_retains_separate_provenance(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      bench, work, evidence, fast = (root / name for name in ('bench', 'work', 'evidence', 'fast'))
      work.mkdir()
      evidence.mkdir()
      base = work / 'direct-payload'
      base.mkdir()
      (base / 'original').write_text('immutable base')
      calls = []
      def command(argv, **kwargs):
        calls.append(list(map(str, argv)))
        if Path(argv[1]).name == 'contract-test.py':
          kwargs['stdout'].write(Path(argv[1]).parent.name + ' passed\n')
        if Path(argv[1]).name == 'prepare-payload.py':
          output = Path(argv[argv.index('--output') + 1])
          source = Path(argv[argv.index('--base-payload') + 1])
          module.shutil.copytree(source, output)
          output.with_name(output.name + '.manifest.json').write_text(json.dumps({'component': Path(argv[1]).parent.name}))
      with patch.object(module, 'command', side_effect=command), \
          patch.object(module, 'overlap_source_fingerprint', return_value=('pinned-source', ())), \
          patch.object(module, 'validate_finalization_overlap', wraps=module.validate_finalization_overlap) as validate:
        validation = module.validate_finalization_overlap(bench, evidence, fast)
        payload, preflight, manifest = module.prepare_finalization_overlap(bench, work, evidence, fast, base,
          validation=validation)
      validate.assert_called_once_with(bench, evidence, fast)
      self.assertEqual([(Path(argv[1]).parent.name, Path(argv[1]).name) for argv in calls], [
        ('localdb-overlap', 'contract-test.py'), ('animation-overlap', 'contract-test.py'),
        ('localdb-overlap', 'prepare-payload.py'), ('animation-overlap', 'prepare-payload.py')])
      self.assertEqual(validation.logs, tuple((component + '-contract.log',
        module.digest(evidence / (component + '-contract.log'))) for component in module.OVERLAP_COMPONENTS))
      producers = [argv for argv in calls if Path(argv[1]).name == 'prepare-payload.py']
      self.assertEqual([Path(argv[1]).parent.name for argv in producers], ['localdb-overlap', 'animation-overlap'])
      self.assertEqual(producers[1][producers[1].index('--base-payload') + 1],
        producers[0][producers[0].index('--output') + 1])
      self.assertEqual(producers[1][producers[1].index('--base-preflight') + 1],
        str(bench / 'install-speed/localdb-overlap/preflight.sh'))
      self.assertEqual((base / 'original').read_text(), 'immutable base')
      self.assertEqual((payload / 'original').read_text(), 'immutable base')
      self.assertEqual(preflight, bench / 'install-speed/animation-overlap/preflight.sh')
      self.assertEqual(manifest, payload.with_name(payload.name + '.manifest.json'))
      self.assertTrue((evidence / 'localdb-overlap.manifest.json').is_file())
      self.assertTrue((evidence / 'animation-overlap.manifest.json').is_file())
      configuration = module.early_variant_configuration(module.FINALIZATION_OVERLAP_VARIANT)
      self.assertEqual(configuration['provenance_key'], 'finalization_overlap_variant')
      self.assertNotEqual(configuration['output_name'], module.early_variant_configuration(module.DIRECT_RESTORE_VARIANT)['output_name'])
      control, candidate = root / 'control.img', root / 'candidate.img'
      control.write_bytes(b'control'); candidate.write_bytes(b'overlap')
      preflight.parent.mkdir(parents=True)
      preflight.write_text('verified component preflight')
      record = module.early_preflight_provenance(module.FINALIZATION_OVERLAP_VARIANT,
        control, candidate, preflight, 'cold', 'firmware')
      self.assertEqual(record['pairs'], 3)
      self.assertEqual(record['base_variant'], module.DIRECT_RESTORE_VARIANT)
      self.assertEqual(record['payload_manifest'], 'animation-overlap.manifest.json')
      self.assertEqual(record['component_manifests'], ['direct-restore.manifest.json',
        'localdb-overlap.manifest.json', 'animation-overlap.manifest.json'])
      self.assertFalse(record['supplemental_image_changed'])
      self.assertEqual(record['target_cache'], 'none')

  def test_setup_layers_preserve_the_base_and_separate_results(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      bench, work, evidence, fast = (root / name for name in ('bench', 'work', 'evidence', 'fast'))
      work.mkdir(); evidence.mkdir()
      base = work / 'overlap-payload'
      base.mkdir(); (base / 'original').write_bytes(b'immutable full-overlap payload')
      base_preflight = root / 'overlap-preflight.sh'
      base_preflight.write_text('verified overlap activation\n')
      calls = []
      def command(argv, **kwargs):
        calls.append(list(map(str, argv)))
        if Path(argv[1]).name == 'contract-test.py':
          kwargs['stdout'].write(Path(argv[1]).parent.name + ' passed\n')
        else:
          output = Path(argv[argv.index('--output') + 1])
          source = Path(argv[argv.index('--base-payload') + 1])
          module.shutil.copytree(source, output)
          output.with_name(output.name + '.manifest.json').write_text(json.dumps({'component': Path(argv[1]).parent.name}))
      with patch.object(module, 'command', side_effect=command), \
          patch.object(module, 'setup_source_fingerprint', return_value=('pinned-setup', ())):
        validation = module.validate_setup_overlap(bench, evidence, fast)
        payload, preflight, manifest = module.prepare_setup_overlap(bench, work, evidence, fast,
          base, base_preflight, validation=validation)
      self.assertEqual([(Path(argv[1]).parent.name, Path(argv[1]).name) for argv in calls], [
        ('firewall-overlap', 'contract-test.py'), ('logging-bind', 'contract-test.py'),
        ('firewall-overlap', 'prepare-payload.py'), ('logging-bind', 'prepare-payload.py')])
      producers = calls[2:]
      self.assertEqual(producers[0][producers[0].index('--base-preflight') + 1], str(base_preflight))
      self.assertEqual(producers[1][producers[1].index('--base-payload') + 1],
        producers[0][producers[0].index('--output') + 1])
      self.assertEqual(producers[1][producers[1].index('--base-preflight') + 1],
        str(bench / 'install-speed/firewall-overlap/preflight.sh'))
      self.assertEqual((base / 'original').read_bytes(), (payload / 'original').read_bytes())
      self.assertEqual(preflight, bench / 'install-speed/logging-bind/preflight.sh')
      self.assertEqual(manifest, payload.with_name(payload.name + '.manifest.json'))
      control, candidate = root / 'control.img', root / 'candidate.img'
      control.write_bytes(b'control'); candidate.write_bytes(b'setup optimized')
      preflight.parent.mkdir(parents=True); preflight.write_text('verified setup preflight\n')
      record = module.early_preflight_provenance(module.SETUP_OVERLAP_VARIANT,
        control, candidate, preflight, 'cold', 'firmware')
      self.assertEqual(record['base_variant'], module.FINALIZATION_OVERLAP_VARIANT)
      self.assertEqual(record['payload_manifest'], 'logging-bind.manifest.json')
      self.assertEqual(record['logging_scope'], 'serial-system-finalizer-only')
      self.assertEqual(record['component_manifests'], ['direct-restore.manifest.json',
        'localdb-overlap.manifest.json', 'animation-overlap.manifest.json',
        'firewall-overlap.manifest.json', 'logging-bind.manifest.json'])
      self.assertEqual(record['pairs'], 3)
      self.assertFalse(record['supplemental_image_changed'])
      names = [module.early_variant_configuration(variant)['output_name'] for variant in module.EARLY_VARIANTS]
      self.assertEqual(len(names), len(set(names)))

  def test_setup_validation_rejects_changed_sources_and_logs_before_preparation(self):
    for change in ('source', 'log', 'context', 'token'):
      with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bench, evidence, fast = (root / name for name in ('bench', 'evidence', 'fast'))
        evidence.mkdir()
        def contract(argv, **kwargs):
          kwargs['stdout'].write('passed\n')
        with patch.object(module, 'command', side_effect=contract) as commands, \
            patch.object(module, 'setup_source_fingerprint', return_value=('pinned-setup', ())) as fingerprint:
          validation = module.validate_setup_overlap(bench, evidence, fast)
          commands.reset_mock()
          if change == 'source':
            fingerprint.return_value = ('changed-setup', ())
          elif change == 'log':
            (evidence / 'logging-bind-contract.log').write_text('modified\n')
          elif change == 'context':
            bench = root / 'other-bench'
          else:
            validation = module._OverlapValidation(*validation)
          with self.assertRaises(RuntimeError):
            module.prepare_setup_overlap(bench, root / 'work', evidence, fast,
              root / 'base', root / 'preflight', validation=validation)
          commands.assert_not_called()

  def test_overlap_component_failure_stops_before_following_preparation(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      evidence = root / 'evidence'
      evidence.mkdir()
      calls = []
      def command(argv, **kwargs):
        calls.append(list(map(str, argv)))
        raise subprocess.CalledProcessError(7, argv)
      with patch.object(module, 'command', side_effect=command), \
          patch.object(module, 'overlap_source_fingerprint', return_value=('pinned-source', ())):
        with self.assertRaises(subprocess.CalledProcessError):
          module.prepare_finalization_overlap(root / 'bench', root / 'work', evidence,
            root / 'source', root / 'base')
      self.assertEqual(len(calls), 1)
      self.assertEqual(Path(calls[0][1]).parent.name, 'localdb-overlap')
      self.assertFalse((root / 'work').exists())
      self.assertFalse((evidence / 'animation-overlap.manifest.json').exists())

  def test_overlap_execute_component_failures_precede_download_and_install(self):
    for failed_component in (*module.OVERLAP_COMPONENTS, *module.SETUP_COMPONENTS):
      with self.subTest(component=failed_component), tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        selected = (module.SETUP_OVERLAP_VARIANT if failed_component in module.SETUP_COMPONENTS
          else module.FINALIZATION_OVERLAP_VARIANT)
        components = (module.OVERLAP_COMPONENTS + module.SETUP_COMPONENTS if selected == module.SETUP_OVERLAP_VARIANT
          else module.OVERLAP_COMPONENTS)
        args = module.parse_arguments(['--repo', str(DIRECTORY.parents[3]), '--work', str(root / 'work'),
          '--evidence', str(root / 'evidence'), '--boot-method', 'firmware', '--source-cache', 'cold',
          '--variants', selected])
        calls = []
        failure = subprocess.CalledProcessError(7, ['component-contract'])
        def command(argv, **kwargs):
          calls.append(list(map(str, argv)))
          script = Path(str(argv[1]))
          if script.name == 'contract-test.py':
            self.assertTrue((args.evidence / 'experiment.json').is_file())
            self.assertEqual(checkout.call_count, 2)
            kwargs['stdout'].write(script.parent.name + ' fixture result\n')
            if script.parent.name == failed_component:
              raise failure
          else:
            self.assertEqual(script.name, 'iso-vm-source-cache-test.sh')
        original_open, original_close = os.open, os.close
        def open_kvm(path, *args, **kwargs):
          return 1000000 if path == '/dev/kvm' else original_open(path, *args, **kwargs)
        def close_kvm(fd):
          if fd not in (1000000, 1000001):
            original_close(fd)
        with patch.object(module.os, 'open', side_effect=open_kvm), patch.object(module.os, 'close', side_effect=close_kvm), \
            patch.object(module.fcntl, 'ioctl', side_effect=[12, 1000001]), \
            patch.object(module, 'disk_budget', return_value={}), patch.object(module, 'git_checkout') as checkout, \
            patch.object(module.subprocess, 'check_output', return_value='fixture-commit\n'), \
            patch.object(module, 'overlap_source_fingerprint', return_value=('pinned-source', ())), \
            patch.object(module, 'setup_source_fingerprint', return_value=('pinned-setup-source', ())), \
            patch.object(module, 'command', side_effect=command), \
            patch.object(module, 'calibration_stage') as calibration:
          with self.assertRaises(subprocess.CalledProcessError) as caught:
            module.execute(args)
        self.assertIs(caught.exception, failure)
        calibration.assert_not_called()
        contracts = [Path(argv[1]).parent.name for argv in calls if Path(argv[1]).name == 'contract-test.py']
        self.assertEqual(contracts, list(components[:components.index(failed_component) + 1]))
        self.assertEqual(list(args.work.iterdir()), [])
        self.assertEqual([call.args[1] for call in checkout.call_args_list], [module.HARNESS_PIN, module.FAST_PIN])

  def test_overlap_validation_rejects_stale_sources_logs_and_context_before_preparation(self):
    for change in ('source', 'log', 'missing-log', 'bench', 'fast', 'evidence', 'wrong-type', 'log-record'):
      with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bench, evidence, fast = (root / name for name in ('bench', 'evidence', 'fast'))
        evidence.mkdir()
        def command(argv, **kwargs):
          kwargs['stdout'].write(Path(argv[1]).parent.name + ' passed\n')
        with patch.object(module, 'command', side_effect=command) as commands, \
            patch.object(module, 'overlap_source_fingerprint', return_value=('pinned-source', ())) as fingerprint:
          validation = module.validate_finalization_overlap(bench, evidence, fast)
          with self.assertRaises(AttributeError):
            validation.sources = ('changed', ())
          commands.reset_mock()
          if change == 'source':
            fingerprint.return_value = ('changed-source', ())
          elif change == 'log':
            (evidence / 'localdb-overlap-contract.log').write_text('changed result\n')
          elif change == 'missing-log':
            (evidence / 'animation-overlap-contract.log').unlink()
          elif change == 'bench':
            bench = root / 'other-bench'
          elif change == 'fast':
            fast = root / 'other-fast'
          elif change == 'evidence':
            evidence = root / 'other-evidence'
          elif change == 'wrong-type':
            validation = True
          elif change == 'log-record':
            validation = validation._replace(logs=())
          with self.assertRaises((RuntimeError, FileNotFoundError)):
            module.prepare_finalization_overlap(bench, root / 'work', evidence, fast, root / 'base',
              validation=validation)
          commands.assert_not_called()
        self.assertFalse((root / 'work').exists())

  def test_overlap_fingerprint_detects_actual_local_source_byte_and_mode_drift(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      bench, fast = root / 'bench', root / 'fast'
      evidence = root / 'evidence'
      evidence.mkdir()
      for name in module.OVERLAP_SOURCE_FILES:
        source = bench / 'install-speed' / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(name + '\n')
        source.chmod(0o644)
      def command(argv, **kwargs):
        kwargs['stdout'].write(Path(argv[1]).parent.name + ' passed\n')
      with patch.object(module.subprocess, 'check_output', return_value=module.FAST_PIN + '\n') as upstream, \
          patch.object(module, 'command', side_effect=command) as commands:
        validation = module.validate_finalization_overlap(bench, evidence, fast)
        original = validation.sources
        source = bench / 'install-speed/animation-overlap/payload-contract-test.py'
        source.write_text('changed contract\n')
        changed_bytes = module.overlap_source_fingerprint(bench, fast)
        self.assertNotEqual(original, changed_bytes)
        commands.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'sources changed after'):
          module.prepare_finalization_overlap(bench, root / 'work', evidence, fast, root / 'base',
            validation=validation)
        commands.assert_not_called()
        source.chmod(0o755)
        self.assertNotEqual(changed_bytes, module.overlap_source_fingerprint(bench, fast))
        source.unlink()
        with self.assertRaisesRegex(RuntimeError, 'regular file'):
          module.overlap_source_fingerprint(bench, fast)
        upstream.return_value = 'unselected-upstream-commit\n'
        with self.assertRaisesRegex(RuntimeError, 'selected upstream commit'):
          module.overlap_source_fingerprint(bench, fast)

  def test_early_verify_builds_matched_control_and_fast_reboot_candidate(self):
    # Exercise native fixture selection against the real overlay builder on a
    # tiny valid newc input. This establishes wiring, not real systemd timing.
    location = DIRECTORY.parent / 'boot-overlay/make-initramfs.py'
    spec = importlib.util.spec_from_file_location('native_early_overlay', location)
    overlay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overlay)
    original = overlay.make_cpio({
      'config': (stat.S_IFREG | 0o644, b'LATEHOOKS=""\n'),
      'init': (stat.S_IFREG | 0o755, b'"$mount_handler" /new_root\nrun_hookfunctions \'run_latehook\'\n'),
    })
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      payload = root / 'payload'
      scripts = payload / 'usr/local/lib/omarchy-benchmark'
      scripts.mkdir(parents=True)
      (scripts / 'dashboard').write_text('immutable pinned dashboard fixture')
      preflight = DIRECTORY.parent / 'fast-reboot/candidate-preflight.sh'
      outputs = {}
      def make_initrd(mode, selected_payload=None, selected_preflight=None, *, label=None,
          disable_prefetch=False, early_preflight=False):
        script = selected_preflight.read_bytes() if selected_preflight else b'#!/bin/bash\ntrue\n'
        data, manifest = overlay.build(original, mode, script, selected_payload,
          disable_package_prefetch=disable_prefetch, early_preflight=early_preflight)
        path = root / (label + '.img')
        self.assertNotIn(path, outputs, 'immutable fixtures must not be rebuilt over earlier variants')
        path.write_bytes(data)
        outputs[path] = (overlay.initramfs_files(data), manifest)
        return path
      control, candidate = module.early_preflight_pair(make_initrd, payload, preflight)
      self.assertNotEqual(control, candidate)
      control_files, control_manifest = outputs[control]
      candidate_files, candidate_manifest = outputs[candidate]
      self.assertTrue(control_manifest['early_preflight'])
      self.assertTrue(candidate_manifest['early_preflight'])
      self.assertFalse(control_manifest['disable_package_prefetch'])
      self.assertTrue(candidate_manifest['disable_package_prefetch'])
      prefix = 'omarchy-benchmark-payload/'
      for name in ('etc/systemd/system/omarchy-benchmark-preflight.service',
          'etc/systemd/system/getty@tty1.service.d/50-omarchy-benchmark-preflight.conf'):
        self.assertEqual(control_files[prefix + name], candidate_files[prefix + name])
      self.assertEqual(candidate_files[prefix + 'usr/local/lib/omarchy-benchmark/preflight.sh'][1], preflight.read_bytes())
      self.assertNotIn(prefix + 'usr/local/lib/omarchy-benchmark/dashboard', control_files)
      self.assertIn(prefix + 'usr/local/lib/omarchy-benchmark/dashboard', candidate_files)
      self.assertEqual(control.name, 'control-early-preflight.img')
      self.assertEqual(candidate.name, 'candidate-no-prefetch-fast-reboot-early-verify.img')

      # The additional candidate shares the immutable early control fixture,
      # but stages its final patch in a distinct initramfs after normal image
      # activation. The two variants must never overwrite evidence paths.
      direct_payload = root / 'direct-payload'
      module.shutil.copytree(payload, direct_payload)
      phase_path = 'usr/local/lib/omarchy-benchmark/direct-restore/phases_impl.py'
      (direct_payload / phase_path).parent.mkdir()
      (direct_payload / phase_path).write_text('pinned direct phases fixture')
      direct_preflight = DIRECTORY.parent / 'image/direct-restore-preflight.sh'
      shared_control, direct_candidate = module.early_preflight_pair(make_initrd,
        direct_payload, direct_preflight, variant=module.DIRECT_RESTORE_VARIANT, control=control)
      self.assertEqual(shared_control, control)
      self.assertEqual(len(outputs), 3)
      self.assertEqual(direct_candidate.name,
        'candidate-no-prefetch-fast-reboot-early-verify-direct-restore.img')
      direct_files, direct_manifest = outputs[direct_candidate]
      self.assertTrue(direct_manifest['early_preflight'])
      self.assertTrue(direct_manifest['disable_package_prefetch'])
      self.assertIn(prefix + phase_path, direct_files)
      self.assertNotIn(prefix + phase_path, candidate_files)
      self.assertEqual(direct_files[prefix + 'usr/local/lib/omarchy-benchmark/preflight.sh'][1],
        direct_preflight.read_bytes())
      overlap_payload = root / 'overlap-payload'
      module.shutil.copytree(direct_payload, overlap_payload)
      additions = {
        'usr/local/lib/omarchy-benchmark/localdb-overlap/phases_impl.py': 'pinned joined indexing phases',
        'usr/local/lib/omarchy-benchmark/animation-overlap/omarchy-install-dashboard': 'pinned foreground animation',
        'usr/local/lib/omarchy-benchmark/animation-overlap/base-preflight.sh': 'pinned indexing preflight',
      }
      for name, body in additions.items():
        path = overlap_payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
      overlap_preflight = DIRECTORY.parent / 'animation-overlap/preflight.sh'
      shared_control, overlap_candidate = module.early_preflight_pair(make_initrd,
        overlap_payload, overlap_preflight, variant=module.FINALIZATION_OVERLAP_VARIANT, control=control)
      self.assertEqual(shared_control, control)
      self.assertEqual(len(outputs), 4)
      self.assertEqual(overlap_candidate.name,
        'candidate-no-prefetch-fast-reboot-early-verify-direct-restore-overlap.img')
      overlap_files, overlap_manifest = outputs[overlap_candidate]
      self.assertTrue(overlap_manifest['early_preflight'])
      self.assertTrue(overlap_manifest['disable_package_prefetch'])
      self.assertEqual(overlap_files[prefix + phase_path], direct_files[prefix + phase_path])
      for name, body in additions.items():
        self.assertEqual(overlap_files[prefix + name][1], body.encode())
        self.assertNotIn(prefix + name, direct_files)
        self.assertNotIn(prefix + name, candidate_files)
      self.assertEqual(overlap_files[prefix + 'usr/local/lib/omarchy-benchmark/preflight.sh'][1],
        overlap_preflight.read_bytes())
      setup_payload = root / 'setup-payload'
      module.shutil.copytree(overlap_payload, setup_payload)
      setup_path = 'usr/local/lib/omarchy-benchmark/logging-bind/guard.py'
      (setup_payload / setup_path).parent.mkdir(parents=True)
      (setup_payload / setup_path).write_text('pinned private logging guard')
      setup_preflight = DIRECTORY.parent / 'logging-bind/preflight.sh'
      shared_control, setup_candidate = module.early_preflight_pair(make_initrd,
        setup_payload, setup_preflight, variant=module.SETUP_OVERLAP_VARIANT, control=control)
      self.assertEqual(shared_control, control)
      self.assertEqual(len(outputs), 5)
      setup_files, setup_manifest = outputs[setup_candidate]
      self.assertTrue(setup_manifest['early_preflight'])
      self.assertTrue(setup_manifest['disable_package_prefetch'])
      self.assertNotIn(prefix + setup_path, overlap_files)
      self.assertIn(prefix + setup_path, setup_files)
      self.assertEqual(setup_files[prefix + phase_path], overlap_files[prefix + phase_path])
      self.assertEqual(setup_files[prefix + 'usr/local/lib/omarchy-benchmark/preflight.sh'][1],
        setup_preflight.read_bytes())
      records = []
      for variant, selected, script in ((module.EARLY_VERIFY_VARIANT, candidate, preflight),
          (module.DIRECT_RESTORE_VARIANT, direct_candidate, direct_preflight),
          (module.FINALIZATION_OVERLAP_VARIANT, overlap_candidate, overlap_preflight),
          (module.SETUP_OVERLAP_VARIANT, setup_candidate, setup_preflight)):
        record = module.early_preflight_provenance(variant, control, selected, script, 'cold', 'firmware')
        self.assertEqual(record['candidate_initramfs_sha256'], module.digest(selected))
        self.assertEqual(record['control_initramfs_sha256'], module.digest(control))
        self.assertEqual(record['pairs'], 3)
        self.assertEqual(record['source_cache'], 'cold')
        self.assertEqual(record['boot_method'], 'firmware')
        selected_iso = selected.with_suffix('.iso')
        launch = module.boot_arguments('firmware', root / 'original.iso', root / 'kernel', selected,
          firmware_iso=selected_iso)
        self.assertEqual(launch, ['--iso', selected_iso, '--verify-standalone-reboot',
          '--standalone-reboot-timeout', '600'])
        records.append(record)
      self.assertNotEqual(records[0]['comparison'], records[1]['comparison'])
      self.assertNotEqual(records[0]['candidate_initramfs_sha256'], records[1]['candidate_initramfs_sha256'])
      self.assertNotIn('payload_manifest', records[0])
      self.assertEqual(records[1]['payload_manifest'], 'direct-restore.manifest.json')
      self.assertFalse(records[1]['supplemental_image_changed'])
      self.assertEqual(records[1]['target_cache'], 'none')
      self.assertEqual(module.early_variant_configuration(module.EARLY_VERIFY_VARIANT)['provenance_key'],
        'early_preflight_variant')
      self.assertEqual(module.early_variant_configuration(module.DIRECT_RESTORE_VARIANT)['provenance_key'],
        'direct_restore_variant')

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

  def test_failed_repeat_rescues_only_its_retained_control_or_candidate(self):
    for revision in ('control', 'candidate'):
      with tempfile.TemporaryDirectory() as temporary:
        run, run_root, evidence, context = failed_repeat_fixture(Path(temporary), revision)
        original = (evidence / 'series.json').read_bytes()
        target = (run / 'target.qcow2').read_bytes()
        process = SimpleNamespace(returncode=1, poll=lambda: 1)
        with patch.object(module, 'command', return_value=0) as invoked:
          self.assertTrue(module.rescue_failed_repeat(process, run_root, evidence, 'fixture-rescue', **context))
        invoked.assert_called_once()
        argv = invoked.call_args.args[0]
        self.assertEqual(argv[1], context['repo'] / 'test/benchmarks/install-speed/native-ci/rescue-failed-install.py')
        self.assertEqual(argv[argv.index('--failed-run') + 1], run)
        self.assertEqual(invoked.call_args.kwargs['timeout'], 360)
        self.assertIn('readonly=on', rescue.readonly_target_args(run / 'target.qcow2')[1])
        self.assertEqual((evidence / 'series.json').read_bytes(), original)
        self.assertEqual((run / 'target.qcow2').read_bytes(), target)
        request = json.loads((context['evidence'] / 'fixture-rescue/rescue-request.json').read_text())
        self.assertFalse(request['measurement_valid'])
        self.assertEqual(request['source_run_directory'], str(run))
        self.assertIn('failure_record_sha256', request['provenance'])

  def test_failed_repeat_missing_or_unsafe_provenance_never_launches_rescue(self):
    for corruption in ('missing-record', 'outside-run', 'changed-manifest', 'accepted-sample', 'symlink-target'):
      with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run, run_root, evidence, context = failed_repeat_fixture(root)
        series_path = evidence / 'series.json'
        series = json.loads(series_path.read_text())
        record_path = Path(series['failed_run_evidence']) / 'failure-record.json'
        record = json.loads(record_path.read_text())
        if corruption == 'missing-record':
          record_path.unlink()
        elif corruption == 'outside-run':
          record['source_run_directory'] = str(root / 'unrelated-run')
          module.save_json(record_path, record)
        elif corruption == 'changed-manifest':
          (run / 'manifest.json').write_text('{}')
        elif corruption == 'accepted-sample':
          series['runs'].append({'name': run.name})
          module.save_json(series_path, series)
        else:
          outside = root / 'unrelated-disk'
          (run / 'target.qcow2').rename(outside)
          (run / 'target.qcow2').symlink_to(outside)
        process = SimpleNamespace(returncode=1, poll=lambda: 1)
        with patch.object(module, 'command') as invoked:
          self.assertFalse(module.rescue_failed_repeat(process, run_root, evidence, 'fixture-rescue', **context))
          invoked.assert_not_called()
        self.assertEqual(json.loads(series_path.read_text())['status'], 'failed')

  def test_failed_repeat_waits_for_process_exit_and_never_retries_rescue(self):
    with tempfile.TemporaryDirectory() as temporary:
      run, run_root, evidence, context = failed_repeat_fixture(Path(temporary))
      original = (evidence / 'series.json').read_bytes()
      active = SimpleNamespace(returncode=None, poll=lambda: None)
      with patch.object(module, 'command') as invoked:
        self.assertFalse(module.rescue_failed_repeat(active, run_root, evidence, 'active-repeat-rescue', **context))
        invoked.assert_not_called()
      stopped = SimpleNamespace(returncode=1, poll=lambda: 1)
      (run / 'qemu.pid').write_text(str(os.getpid()))
      with patch.object(module, 'command') as invoked:
        self.assertFalse(module.rescue_failed_repeat(stopped, run_root, evidence, 'active-qemu-rescue', **context))
        invoked.assert_not_called()
      (run / 'qemu.pid').write_text('2147483647\n')
      with patch.object(module, 'command', side_effect=subprocess.TimeoutExpired(['rescue'], 360)) as invoked:
        self.assertFalse(module.rescue_failed_repeat(stopped, run_root, evidence, 'fixture-rescue', **context))
        self.assertFalse(module.rescue_failed_repeat(stopped, run_root, evidence, 'fixture-rescue', **context))
        invoked.assert_called_once()
      self.assertEqual((evidence / 'series.json').read_bytes(), original)
      self.assertTrue((run / 'target.qcow2').is_file())

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

  def test_rescue_mount_failure_preserves_partial_readonly_diagnostics(self):
    layout = {'blockdevices': [{'name': '/dev/vdb', 'type': 'disk', 'ro': True, 'children': [
      {'name': '/dev/vdb1', 'type': 'part', 'fstype': 'vfat', 'ro': True},
      {'name': '/dev/vdb2', 'type': 'part', 'fstype': 'btrfs', 'ro': True},
    ]}]}
    invoked = []
    real_run = subprocess.run
    def run(argv, **kwargs):
      invoked.append(argv)
      if argv[0] == 'mount':
        # Capture actual subprocess stderr/exit 32 without mounting anything.
        script = "import sys; sys.stderr.write('x'*300+'\\n-----BEGIN OPENSSH PRIVATE KEY-----\\n'+'KEY-DATA-'*40+'\\n-----END OPENSSH PRIVATE KEY-----\\nsynthetic mount failure\\npassword=DO-NOT-EXPORT\\n'); sys.exit(32)"
        return real_run([sys.executable, '-c', script], **kwargs)
      output = {'systemd-detect-virt': 'kvm\n', 'blockdev': '1\n', 'lsblk': json.dumps(layout),
        'findmnt': '{"filesystems": []}', 'blkid': 'TYPE=btrfs\n',
        'dmesg': 'BTRFS error: synthetic mount failure\nBTRFS token=DO-NOT-EXPORT\n'}[argv[0]]
      return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')
    with tempfile.TemporaryDirectory() as temporary, \
        patch.object(collector, 'MOUNT', Path(temporary) / 'rescue'), \
        patch.object(collector, 'LIMIT', 128), \
        patch.object(collector.os, 'geteuid', return_value=0), \
        patch.object(collector.Path, 'is_file', return_value=True), \
        patch.object(collector.Path, 'is_block_device', return_value=True), \
        patch.object(collector.subprocess, 'run', side_effect=run):
      result = collector.collect()
    self.assertEqual(result['status'], 'partial')
    self.assertFalse(result['measurement_valid'])
    self.assertTrue(result['block_read_only'])
    self.assertEqual(result['block_devices'], layout)
    failure = result['command_failures'][0]
    self.assertEqual(failure['exit_status'], 32)
    self.assertIn('synthetic mount failure', failure['stderr'])
    self.assertTrue(failure['stderr_truncated'])
    self.assertLessEqual(len(failure['stderr']), 128)
    self.assertNotIn('DO-NOT-EXPORT', json.dumps(result))
    self.assertNotIn('KEY-DATA', json.dumps(result))
    self.assertIn('failure_mounts', result['commands'])
    self.assertIn('failure_block_devices', result['commands'])
    self.assertIn('failure_blkid/vdb2', result['commands'])
    self.assertIn('BTRFS error', result['commands']['failure_kernel_mount_tail']['output'])
    mounts = [argv for argv in invoked if argv[0] == 'mount']
    self.assertEqual(len(mounts), 1, 'A failed mount must never trigger recovery/remount attempts')
    self.assertEqual(mounts[0][4], 'ro,rescue=nologreplay,subvolid=5,nosuid,nodev')


if __name__ == '__main__':
  unittest.main()
