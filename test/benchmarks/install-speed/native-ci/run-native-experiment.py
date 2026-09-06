#!/usr/bin/env python3
"""Pinned official-package experiment on a disposable Linux KVM runner.

This driver deliberately never falls back to software emulation.
Build time is outside the per-install timer, as with building a release ISO.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from typing import NamedTuple
import uuid

ISO_URL = 'https://iso.omarchy.org/omarchy-4.0.2.iso'
ISO_SHA256 = '2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8'
INITRD_SHA256 = '6e3e15b983da69df4e18df2f1489fa854980b395b28546355d0f6dc13914694e'
FAST_PIN = 'dbffaa6c65344d644627a023c28661e08382b8fa'
HARNESS_PIN = '2673c613d9a71e23920e43fbb951238145e0f1e8'
CMDLINE = 'archisobasedir=arch archisosearchuuid=2026-08-31-03-24-58-00 quiet splash xe.enable_panel_replay=0 initramfs_async=0 copytoram=n'
EARLY_VERIFY_VARIANT = 'image-no-package-prefetch-fast-reboot-early-verify'
DIRECT_RESTORE_VARIANT = EARLY_VERIFY_VARIANT + '-direct-restore'
FINALIZATION_OVERLAP_VARIANT = DIRECT_RESTORE_VARIANT + '-overlap'
SETUP_OVERLAP_VARIANT = FINALIZATION_OVERLAP_VARIANT + '-firewall-logging'
OVERLAP_VARIANTS = (FINALIZATION_OVERLAP_VARIANT, SETUP_OVERLAP_VARIANT)
EARLY_VARIANTS = (EARLY_VERIFY_VARIANT, DIRECT_RESTORE_VARIANT, *OVERLAP_VARIANTS)
EVIDENCE_FILES = {
  'manifest.json', 'validation.json', 'install-timing.json', 'install-log-events.json', 'package-manifest.txt',
  'package-explicit.txt', 'package-files.txt', 'package-files.stderr', 'identity.json',
  'installed-root.json', 'installed-boot.txt', 'machine-id.txt', 'btrfs-uuid.txt',
  'btrfs-subvolumes.txt', 'ssh-host-fingerprints.txt', 'pacman-master-keys.txt',
  'uki-files.txt', 'systemd-analyze-blame.txt', 'systemd-analyze-critical-chain.txt',
  'serial.log', 'live-serial.log', 'qemu.log', 'progress.json', 'latest-screen.png',
  'last-failed-ssh-probe.json', 'timeout-diagnostics.json',
  'timeout-before-keys.png', 'timeout-after-escape.png', 'timeout-after-tty2.png',
  'standalone-last-failed-ssh-probe.json', 'standalone-timeout-diagnostics.json',
  'standalone-timeout-before-keys.png', 'standalone-timeout-after-escape.png',
  'standalone-timeout-after-tty2.png',
  'timeout-console.log', 'timeout-console.json',
  'standalone-timeout-console.log', 'standalone-timeout-console.json',
  'standalone-reboot.json',
  'standalone-root.json', 'standalone-identity.json', 'standalone-machine-id.txt',
  'standalone-ssh-host-fingerprints.txt', 'standalone-pacman-master-keys.txt',
  'standalone-btrfs-uuid.txt', 'standalone-btrfs-subvolumes.txt', 'standalone-uki-files.txt',
}


def stop_process_group(process, grace=10):
  # This is our own benchmark process group, never a user's VM or process.
  try:
    os.killpg(process.pid, signal.SIGTERM)
  except ProcessLookupError:
    return
  try:
    process.wait(timeout=grace)
  except subprocess.TimeoutExpired:
    pass
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  process.wait(timeout=10)


def command(argv, **kwargs):
  print('+ ' + ' '.join(map(str, argv)), flush=True)
  timeout = kwargs.pop('timeout', None)
  process = subprocess.Popen(list(map(str, argv)), start_new_session=True, **kwargs)
  try:
    status = process.wait(timeout=timeout)
    if status:
      raise subprocess.CalledProcessError(status, argv)
    return status
  except BaseException:
    stop_process_group(process)
    raise


def digest(path):
  with path.open('rb') as source:
    return hashlib.file_digest(source, 'sha256').hexdigest()


def save_json(path, value):
  path.write_text(json.dumps(value, indent=2) + '\n')


def disk_budget(work, boot_method, variants=()):
  required = (44 if boot_method == 'firmware' else 28) * 1024**3
  if any(variant in variants for variant in EARLY_VARIANTS):
    # The early experiment needs its own matched control ISO. When several
    # variants are requested, their immutable fixtures remain available too.
    required += 6 * max(0, len(variants) - 1) * 1024**3
  free = shutil.disk_usage(work).free
  if free < required:
    raise RuntimeError(f'{boot_method} mode requires {required // 1024**3} GiB free before preparation; found {free / 1024**3:.2f} GiB')
  return {'boot_method': boot_method, 'minimum_free_bytes': required, 'observed_free_bytes': free}


def early_variant_configuration(variant):
  if variant not in EARLY_VARIANTS:
    raise ValueError('Not an early-preflight variant')
  suffix = {EARLY_VERIFY_VARIANT: '', DIRECT_RESTORE_VARIANT: '-direct-restore',
    FINALIZATION_OVERLAP_VARIANT: '-direct-restore-overlap',
    SETUP_OVERLAP_VARIANT: '-direct-restore-overlap-firewall-logging'}[variant]
  provenance_key = {EARLY_VERIFY_VARIANT: 'early_preflight_variant',
    DIRECT_RESTORE_VARIANT: 'direct_restore_variant',
    FINALIZATION_OVERLAP_VARIANT: 'finalization_overlap_variant',
    SETUP_OVERLAP_VARIANT: 'setup_overlap_variant'}[variant]
  return {'candidate_label': 'candidate-no-prefetch-fast-reboot-early-verify' + suffix,
    'output_name': 'no-prefetch-fast-reboot-early-verify' + suffix + '-repetitions',
    'provenance_key': provenance_key}


def early_preflight_pair(make_initrd, payload, preflight, *, variant=EARLY_VERIFY_VARIANT, control=None):
  # The control includes the same early service and completion check, with
  # the existing no-op preflight. Only the candidate activates/verifies media.
  if control is None:
    control = make_initrd('control', label='control-early-preflight', early_preflight=True)
  candidate = make_initrd('candidate', payload, preflight,
    label=early_variant_configuration(variant)['candidate_label'], disable_prefetch=True, early_preflight=True)
  return control, candidate


def early_preflight_provenance(variant, control, candidate, preflight, source_cache, boot_method):
  configuration = early_variant_configuration(variant)
  record = {
    'variant': variant, 'base_variant': 'image-no-package-prefetch-fast-reboot',
    'control_initramfs_sha256': digest(control),
    'candidate_initramfs_sha256': digest(candidate),
    'fast_reboot_manifest': 'fast-reboot.manifest.json',
    'preflight_script_sha256': digest(preflight),
    'matched_control': 'early service with no-op preflight',
    'source_cache': source_cache, 'boot_method': boot_method,
    'pairs': 3, 'comparison': configuration['output_name'] + '/comparison.json',
  }
  if variant in (DIRECT_RESTORE_VARIANT, *OVERLAP_VARIANTS):
    record.update({'base_variant': EARLY_VERIFY_VARIANT,
      'payload_manifest': 'direct-restore.manifest.json',
      'supplemental_image_changed': False, 'target_cache': 'none'})
  if variant in OVERLAP_VARIANTS:
    record.update({'base_variant': DIRECT_RESTORE_VARIANT,
      'payload_manifest': 'animation-overlap.manifest.json',
      'component_manifests': ['direct-restore.manifest.json', 'localdb-overlap.manifest.json',
        'animation-overlap.manifest.json'],
      'required_work': 'File index joins existing finalization; complete animation precedes release-gated completion'})
  if variant == SETUP_OVERLAP_VARIANT:
    record.update({'base_variant': FINALIZATION_OVERLAP_VARIANT,
      'payload_manifest': 'logging-bind.manifest.json',
      'component_manifests': record['component_manifests'] +
        ['firewall-overlap.manifest.json', 'logging-bind.manifest.json'],
      'logging_scope': 'serial-system-finalizer-only',
      'required_work': 'Unchanged firewall commands precede user setup and indexing in the joined user branch; only serial system setup sees the temporary private read-only logging helper'})
  return record


OVERLAP_COMPONENTS = ('localdb-overlap', 'animation-overlap')
OVERLAP_SOURCE_FILES = (
  'localdb-overlap/contract-test.py', 'localdb-overlap/patch.py',
  'localdb-overlap/prepare-payload.py', 'localdb-overlap/preflight.sh',
  'animation-overlap/contract-test.py', 'animation-overlap/payload-contract-test.py', 'animation-overlap/dashboard_patch.py',
  'animation-overlap/prepare-payload.py', 'animation-overlap/preflight.sh',
  'fast-reboot/prepare-payload.py', 'fast-reboot/candidate-preflight.sh',
  'image/direct-restore-payload.py', 'image/direct_restore.py', 'image/root_image_mounts.py',
  'image/direct-restore-preflight.sh', 'image/candidate-preflight.sh', 'image/activate-installer-overlay.sh',
  'localdb-overlap/fixtures/runtime/usr/bin/omarchy-apply-system',
  'localdb-overlap/fixtures/runtime/usr/share/omarchy/install/helpers/logging.sh',
  'localdb-overlap/fixtures/runtime/usr/share/omarchy/install/post-install/all.sh',
  'localdb-overlap/fixtures/runtime/usr/share/omarchy/install/post-install/localdb.sh',
)


class _OverlapValidation(NamedTuple):
  bench: Path
  fast: Path
  evidence: Path
  sources: tuple
  logs: tuple


def overlap_source_fingerprint(bench, fast):
  """Hash only the selected contracts, producers and their local inputs."""
  upstream = subprocess.check_output(['git', '-C', str(fast), 'rev-parse', 'HEAD'], text=True).strip()
  if upstream != FAST_PIN:
    raise RuntimeError('Overlap contracts require the selected upstream commit')
  files = []
  for name in OVERLAP_SOURCE_FILES:
    path = bench / 'install-speed' / name
    if path.is_symlink() or not path.is_file():
      raise RuntimeError(f'Overlap source must be a regular file: {name}')
    files.append((name, digest(path), path.stat().st_mode & 0o7777))
  return upstream, tuple(files)


def validate_finalization_overlap(bench, evidence, fast):
  sources = overlap_source_fingerprint(bench, fast)
  logs = []
  for component in OVERLAP_COMPONENTS:
    scripts = bench / 'install-speed' / component
    path = evidence / (component + '-contract.log')
    with path.open('w') as log:
      command([sys.executable, scripts / 'contract-test.py', '--iso-source', fast],
        stdout=log, stderr=subprocess.STDOUT, timeout=120)
    logs.append((path.name, digest(path)))
  if overlap_source_fingerprint(bench, fast) != sources:
    raise RuntimeError('Overlap sources changed during contract validation')
  return _OverlapValidation(bench.resolve(), fast.resolve(), evidence.resolve(), sources, tuple(logs))


def prepare_finalization_overlap(bench, work, evidence, fast, payload, *, validation=None):
  """Layer verified components; standalone callers still run their contracts."""
  if validation is None:
    validation = validate_finalization_overlap(bench, evidence, fast)
  if (type(validation) is not _OverlapValidation or
      (validation.bench, validation.fast, validation.evidence) != (bench.resolve(), fast.resolve(), evidence.resolve())):
    raise RuntimeError('Overlap preparation requires its matching contract validation result')
  if overlap_source_fingerprint(bench, fast) != validation.sources:
    raise RuntimeError('Overlap sources changed after contract validation')
  logs = tuple((component + '-contract.log', digest(evidence / (component + '-contract.log')))
    for component in OVERLAP_COMPONENTS)
  if logs != validation.logs:
    raise RuntimeError('Overlap contract evidence changed after validation')
  preflight = bench / 'install-speed/image/direct-restore-preflight.sh'
  for component in OVERLAP_COMPONENTS:
    scripts = bench / 'install-speed' / component
    output = work / ('candidate-' + component + '-payload')
    argv = [sys.executable, scripts / 'prepare-payload.py', '--iso-source', fast,
      '--base-payload', payload, '--output', output]
    if component == 'animation-overlap':
      argv += ['--base-preflight', preflight]
    command(argv)
    manifest = output.with_name(output.name + '.manifest.json')
    shutil.copyfile(manifest, evidence / (component + '.manifest.json'))
    payload, preflight = output, scripts / 'preflight.sh'
  return payload, preflight, manifest


SETUP_COMPONENTS = ('firewall-overlap', 'logging-bind')
SETUP_SOURCE_FILES = (
  'firewall-overlap/patch.py', 'firewall-overlap/prepare-payload.py',
  'firewall-overlap/preflight.sh', 'firewall-overlap/contract-test.py',
  'firewall-overlap/fixtures/runtime/usr/share/omarchy/install/config/all.sh',
  'firewall-overlap/fixtures/runtime/usr/share/omarchy/install/config/firewall.sh',
  'logging-bind/patch.py', 'logging-bind/guard.py', 'logging-bind/prepare-payload.py',
  'logging-bind/preflight.sh', 'logging-bind/contract-test.py', 'logging-bind/payload-contract-test.py',
  'logging-bind/guest-contract.py',
)


class _SetupValidation(NamedTuple):
  bench: Path
  fast: Path
  evidence: Path
  sources: tuple
  logs: tuple


def setup_source_fingerprint(bench, fast):
  upstream, inherited = overlap_source_fingerprint(bench, fast)
  files = list(inherited)
  inputs = [(name, bench / 'install-speed' / name) for name in SETUP_SOURCE_FILES]
  inputs.extend((name, bench.parents[1] / name) for name in ('install/helpers/logging.sh', 'LICENSE'))
  for name, path in inputs:
    if path.is_symlink() or not path.is_file():
      raise RuntimeError(f'Setup optimization source must be a regular file: {name}')
    files.append((name, digest(path), path.stat().st_mode & 0o7777))
  return upstream, tuple(files)


def validate_setup_overlap(bench, evidence, fast):
  sources = setup_source_fingerprint(bench, fast)
  logs = []
  for component in SETUP_COMPONENTS:
    scripts = bench / 'install-speed' / component
    path = evidence / (component + '-contract.log')
    with path.open('w') as log:
      command([sys.executable, scripts / 'contract-test.py', '--iso-source', fast],
        stdout=log, stderr=subprocess.STDOUT, timeout=120)
    logs.append((path.name, digest(path)))
  if setup_source_fingerprint(bench, fast) != sources:
    raise RuntimeError('Setup optimization sources changed during contract validation')
  return _SetupValidation(bench.resolve(), fast.resolve(), evidence.resolve(), sources, tuple(logs))


def prepare_setup_overlap(bench, work, evidence, fast, payload, preflight, *, validation=None):
  if validation is None:
    validation = validate_setup_overlap(bench, evidence, fast)
  if (type(validation) is not _SetupValidation or
      (validation.bench, validation.fast, validation.evidence) != (bench.resolve(), fast.resolve(), evidence.resolve())):
    raise RuntimeError('Setup preparation requires its matching contract validation result')
  if setup_source_fingerprint(bench, fast) != validation.sources:
    raise RuntimeError('Setup optimization sources changed after contract validation')
  logs = tuple((component + '-contract.log', digest(evidence / (component + '-contract.log')))
    for component in SETUP_COMPONENTS)
  if logs != validation.logs:
    raise RuntimeError('Setup optimization contract evidence changed after validation')
  for component in SETUP_COMPONENTS:
    scripts = bench / 'install-speed' / component
    output = work / ('candidate-' + component + '-payload')
    command([sys.executable, scripts / 'prepare-payload.py', '--iso-source', fast,
      '--base-payload', payload, '--base-preflight', preflight, '--output', output])
    manifest = output.with_name(output.name + '.manifest.json')
    shutil.copyfile(manifest, evidence / (component + '.manifest.json'))
    payload, preflight = output, scripts / 'preflight.sh'
  return payload, preflight, manifest


def boot_arguments(boot_method, iso, kernel, initrd, *, builder=False, firmware_iso=None, standalone_reboot_timeout=600):
  if boot_method == 'firmware' and not builder:
    if firmware_iso is None:
      raise ValueError('Firmware installs require a separately verified derived ISO')
    return ['--iso', firmware_iso, '--verify-standalone-reboot',
      '--standalone-reboot-timeout', str(standalone_reboot_timeout)]
  return ['--iso', iso, '--kernel', kernel, '--initrd', initrd, '--append', CMDLINE]


def timeout_arguments(install_timeout, *, builder=False):
  return ['--timeout', '5400' if builder else str(install_timeout)]


def diagnostic_provenance(count):
  return {'measurement_valid': False, 'purpose': 'Installed boot diagnosis only',
    'pairs_per_variant': 0, 'variants': [],
    'diagnostic_calibrations': {'requested_count': count, 'status': 'preparing', 'completed_names': []}}


def calibration_stage(install, control_initrd, diagnostic_count, provenance, evidence):
  if diagnostic_count is None:
    return install('calibration', control_initrd)
  record = provenance['diagnostic_calibrations']
  record['status'] = 'running'
  provenance['status'] = 'diagnosing-installed-boot'
  for index in range(1, diagnostic_count + 1):
    name = f'diagnostic-calibration-{index:02d}'
    record['current_name'] = name
    save_json(evidence / 'experiment.json', provenance)
    try:
      # This is the same fresh install, validation, cleanup and bounded rescue
      # path as ordinary calibration. A failed attempt is never retried.
      install(name, control_initrd)
    except BaseException:
      record.update(status='failed', failed_name=name)
      provenance['status'] = 'diagnostic-failed'
      try:
        save_json(evidence / 'experiment.json', provenance)
      except OSError as capture_error:
        print(json.dumps({'event': 'diagnostic-progress-capture-failed', 'error': str(capture_error)}), flush=True)
      raise
    record['completed_names'].append(name)
    save_json(evidence / 'experiment.json', provenance)
  record.pop('current_name', None)
  record['status'] = 'complete'
  provenance['status'] = 'diagnostic-complete'
  save_json(evidence / 'experiment.json', provenance)
  # None is an explicit stop before image preparation or any comparisons.
  return None


def git_checkout(destination, revision):
  command(['git', 'init', destination])
  command(['git', '-C', destination, 'remote', 'add', 'origin', 'https://github.com/omacom-io/omarchy-iso.git'])
  command(['git', '-C', destination, 'fetch', '--depth', '1', 'origin', revision])
  command(['git', '-C', destination, 'checkout', '--detach', 'FETCH_HEAD'])
  actual = subprocess.check_output(['git', '-C', str(destination), 'rev-parse', 'HEAD'], text=True).strip()
  if actual != revision:
    raise ValueError('Upstream source pin mismatch')


def collect_small(run, evidence):
  evidence.mkdir(parents=True, exist_ok=True)
  for name in EVIDENCE_FILES:
    source = run / name
    if source.is_file():
      if source.stat().st_size > 20 * 1024**2:
        raise ValueError(f'Unexpectedly large evidence: {source}')
      shutil.copyfile(source, evidence / name)


def read_regular_json(path):
  if path.resolve() != path or path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024**2:
    raise ValueError(f'Missing, unsafe or oversized failure provenance: {path}')
  return json.loads(path.read_text())


def retained_failed_run(repo, run_root, evidence_root):
  """Resolve only this failed series' planned, unaccepted, retained target."""
  series_path = evidence_root / 'series.json'
  series = read_regular_json(series_path)
  plan = series['plan']
  if (series.get('status') != 'failed' or plan.get('run_root') != str(run_root)
      or plan.get('evidence_root') != str(evidence_root)):
    raise ValueError('Rescue requires this exact failed repeated-install series')
  failed = Path(series['failed_run_evidence'])
  if (not failed.is_absolute() or failed.resolve() != failed
      or failed.parent != evidence_root / 'failed-runs'):
    raise ValueError('Failed-run evidence is outside this series')
  name = failed.name
  planned = [row for row in plan['order'] if row.get('name') == name]
  if (len(planned) != 1 or planned[0].get('revision') not in {'control', 'candidate'}
      or any(row.get('name') == name for row in series['runs'])):
    raise ValueError('Rescue target is not a unique unaccepted sample in this plan')
  run = run_root / name
  if run.resolve() != run or not run.is_dir():
    raise ValueError('Retained source run is missing or uses a symlink')
  record_path = failed / 'failure-record.json'
  record = read_regular_json(record_path)
  if (record.get('schema_version') != 1 or record.get('status') != 'failed'
      or record.get('measurement_valid') is not False
      or record.get('source_run_directory') != str(run)
      or record.get('failure') != series.get('failure')):
    raise ValueError('Failure record does not identify this invalid source run')
  bench = repo / 'test/benchmarks'
  for key, source in (('runner_sha256', bench / 'iso-vm.py'),
      ('comparator_sha256', bench / 'compare-installs.py'),
      ('repeat_driver_sha256', bench / 'install-speed/repeat-installs.py')):
    if record.get(key) != digest(source) or record.get(key) != plan['source_provenance'].get(key):
      raise ValueError('Failed-run benchmark source provenance differs from this driver')
  # Verify the captured small-file hashes before trusting the recorded target.
  # There is deliberately no search for alternative disks if evidence is absent.
  files = record['files']
  if 'manifest.json' not in files or 'manifest.json' in record.get('capture_errors', {}):
    raise ValueError('Failed-run manifest was not preserved successfully')
  for leaf, entry in files.items():
    path = failed / leaf
    if (leaf not in EVIDENCE_FILES | {'installed-screen.png'} or path.resolve() != path
        or not path.is_file() or type(entry.get('bytes')) is not int
        or not 0 <= entry['bytes'] <= 20 * 1024**2
        or path.stat().st_size != entry['bytes'] or digest(path) != entry.get('sha256')):
      raise ValueError(f'Failed-run evidence is unsafe or changed: {leaf}')
  manifest_path = run / 'manifest.json'
  manifest = read_regular_json(manifest_path)
  if digest(manifest_path) != files['manifest.json']['sha256']:
    raise ValueError('Retained run manifest changed after failure capture')
  if manifest.get('mode') != 'install' or manifest.get('fresh_target') is not True:
    raise ValueError('Retained run is not a fresh installation target')
  target = run / 'target.qcow2'
  if target.resolve() != target or not target.is_file():
    raise ValueError('Retained installation target is missing or uses a symlink')
  argv = manifest['qemu_argv']
  if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
    raise ValueError('Missing recorded QEMU launch arguments')
  targets = []
  for index, value in enumerate(argv[:-1]):
    if value == '-drive':
      options = dict(piece.split('=', 1) for piece in argv[index + 1].split(',') if '=' in piece)
      if options.get('id') == 'target':
        targets.append(options)
  if len(targets) != 1 or targets[0].get('file') != str(target) or targets[0].get('format') != 'qcow2':
    raise ValueError('Recorded QEMU target does not match the retained source disk')
  return run, {'series': str(series_path), 'series_sha256': digest(series_path),
    'failure_record': str(record_path), 'failure_record_sha256': digest(record_path),
    'manifest_sha256': files['manifest.json']['sha256']}


def record_rescue_failure(directory, error):
  # Rescue diagnostics must never replace the original installation failure,
  # even if the evidence filesystem is full or unavailable.
  try:
    directory.mkdir(parents=True, exist_ok=True)
    save_json(directory / 'rescue-failure.json',
      {'error_type': type(error).__name__, 'error': str(error), 'measurement_valid': False})
  except OSError as capture_error:
    print(json.dumps({'event': 'rescue-error-capture-failed', 'error': str(capture_error)}), flush=True)


def rescue_failed_install(run, rescue_name, *, repo, work, evidence, iso, harness, kernel, initrd, provenance=None):
  if Path(rescue_name).name != rescue_name or rescue_name in {'', '.', '..'}:
    return False
  destination = evidence / rescue_name
  try:
    if run.resolve() != run or not run.is_relative_to(work) or not (run / 'target.qcow2').is_file():
      raise ValueError('Rescue requires an exact retained target inside this experiment')
    if (run / 'target.qcow2').resolve() != run / 'target.qcow2':
      raise ValueError('Refusing a symlinked rescue target')
    for name in ('supervisor.pid', 'qemu.pid'):
      path = run / name
      if path.resolve() != path or not path.is_file():
        raise ValueError(f'Missing stopped-process provenance: {name}')
      pid = int(path.read_text())
      if pid <= 0:
        raise ValueError(f'Invalid stopped-process provenance: {name}')
      try:
        os.kill(pid, 0)
      except ProcessLookupError:
        continue
      raise RuntimeError(f'Refusing rescue while {name} is still alive')
    destination.mkdir(parents=True, exist_ok=True)
    request = destination / 'rescue-request.json'
    with request.open('x') as output:
      json.dump({'measurement_valid': False, 'source_run_directory': str(run),
        'provenance': provenance, 'read_only_rescue': True}, output, indent=2)
      output.write('\n')
    # This existing script checks supervisor/QEMU PIDs before constructing the
    # fresh live VM, and attaches only this target with QEMU readonly=on.
    with (destination / 'rescue-driver.log').open('w') as log:
      command([sys.executable, repo / 'test/benchmarks/install-speed/native-ci/rescue-failed-install.py',
        '--repo', repo, '--work', work / rescue_name, '--evidence', destination,
        '--failed-run', run, '--iso', iso, '--harness', harness, '--kernel', kernel,
        '--initrd', initrd], stdout=log, stderr=subprocess.STDOUT, timeout=360)
    return True
  except Exception as error:
    record_rescue_failure(destination, error)
    return False


def rescue_failed_repeat(process, run_root, series_evidence, rescue_name, **context):
  if Path(rescue_name).name != rescue_name or rescue_name in {'', '.', '..'}:
    return False
  try:
    if process.poll() is None or process.returncode in (0, 2):
      raise ValueError('Read-only rescue requires a stopped, failed repeat driver')
    run, provenance = retained_failed_run(context['repo'], run_root, series_evidence)
  except Exception as error:
    record_rescue_failure(context['evidence'] / rescue_name, error)
    return False
  return rescue_failed_install(run, rescue_name, provenance=provenance, **context)


def mailbox(run, request, timeout=180):
  identifier = uuid.uuid4().hex
  target = run / 'requests' / (identifier + '.json')
  temporary = target.with_suffix('.tmp')
  temporary.write_text(json.dumps(request))
  temporary.rename(target)
  response = run / 'responses' / (identifier + '.json')
  deadline = time.monotonic() + timeout
  while not response.exists():
    if time.monotonic() > deadline:
      raise TimeoutError(f'Mailbox timed out: {request.get("action")}')
    time.sleep(1)
  result = json.loads(response.read_text())
  if not result.get('ok'):
    raise RuntimeError(result)
  return result


def execute(args):
  repo, work, evidence = args.repo.resolve(), args.work.resolve(), args.evidence.resolve()
  if work.exists():
    raise ValueError('Use a new empty work path; no reuse of disks or NVRAM')
  work.mkdir(parents=True)
  evidence.mkdir(parents=True, exist_ok=True)
  save_json(evidence / 'disk-budget.json', disk_budget(work, args.boot_method, args.variants))
  fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
  if fcntl.ioctl(fd, 0xae00, 0) != 12:
    raise RuntimeError('KVM API mismatch')
  vm = fcntl.ioctl(fd, 0xae01, 0)
  os.close(vm)
  os.close(fd)
  bench = repo / 'test/benchmarks'
  image = bench / 'install-speed/image'
  overlay = bench / 'install-speed/boot-overlay'
  if args.source_cache == 'cold':
    with (evidence / 'cold-cache-preflight.log').open('w') as log:
      command(['bash', repo / 'test/shell.d/iso-vm-source-cache-test.sh'],
        env=dict(os.environ, OMARCHY_REQUIRE_COLD_EVICTION='1'), cwd=repo,
        stdout=log, stderr=subprocess.STDOUT, timeout=120)
  harness, fast = work / 'iso-harness', work / 'iso-fast'
  git_checkout(harness, HARNESS_PIN)
  git_checkout(fast, FAST_PIN)
  provenance = {
    'schema_version': 1, 'status': 'preparing', 'iso_url': ISO_URL,
    'iso_sha256': ISO_SHA256, 'upstream_fast_pin': FAST_PIN, 'cidata_harness_pin': HARNESS_PIN,
    'repository_commit': subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
    'github_run_id': os.getenv('GITHUB_RUN_ID'), 'github_run_attempt': os.getenv('GITHUB_RUN_ATTEMPT'),
    'accelerator': 'kvm', 'cpus': 4, 'memory_mib': 8192, 'pairs_per_variant': 3,
    'variants': args.variants, 'comparisons': {},
    'boot_method': args.boot_method, 'firmware_fixtures': {},
    'install_timeout_seconds': args.install_timeout,
    'standalone_reboot_timeout_seconds': args.standalone_reboot_timeout,
    'source_cache': args.source_cache,
    'cache_policy': ('Integrity pre-reads condition source media before timing' if args.source_cache == 'conditioned' else 'Require verified source-page eviction after integrity reads and before timing; see per-run evidence'),
    'build_time_in_install_time': False,
  }
  if args.diagnostic_calibrations is not None:
    provenance.update(diagnostic_provenance(args.diagnostic_calibrations))
  save_json(evidence / 'experiment.json', provenance)
  overlap_validation = None
  setup_validation = None
  if args.diagnostic_calibrations is None and any(variant in args.variants for variant in OVERLAP_VARIANTS):
    overlap_validation = validate_finalization_overlap(bench, evidence, fast)
  if args.diagnostic_calibrations is None and SETUP_OVERLAP_VARIANT in args.variants:
    setup_validation = validate_setup_overlap(bench, evidence, fast)
  iso = work / 'omarchy-4.0.2.iso'
  command(['curl', '--fail', '--location', '--retry', '3', '--output', iso, ISO_URL])
  if digest(iso) != ISO_SHA256:
    raise ValueError('Official ISO checksum mismatch')
  kernel, initrd = work / 'vmlinuz-linux-t2', work / 'initramfs-linux-t2.img'
  for path in (kernel, initrd):
    command(['xorriso', '-osirrox', 'on', '-indev', iso, '-extract', '/arch/boot/x86_64/' + path.name, path])
  if digest(initrd) != INITRD_SHA256:
    raise ValueError('Official initramfs checksum mismatch')

  def make_initrd(mode, payload=None, preflight=None, *, label=None, disable_prefetch=False, early_preflight=False):
    label = label or mode
    output = work / f'initramfs-{label}.img'
    argv = [sys.executable, overlay / 'make-initramfs.py', '--initramfs', initrd,
        '--expected-initramfs-sha256', INITRD_SHA256, '--mode', mode, '--output', output]
    if payload:
      argv += ['--payload-dir', payload]
    if preflight:
      argv += ['--preflight-script', preflight]
    if disable_prefetch:
      argv += ['--disable-package-prefetch']
    if early_preflight:
      argv += ['--early-preflight']
    command(argv)
    manifest = output.with_suffix(output.suffix + '.manifest.json')
    if early_preflight and json.loads(manifest.read_text()).get('early_preflight') is not True:
      raise RuntimeError('Early-preflight initramfs lacks explicit opt-in provenance')
    shutil.copyfile(manifest, evidence / f'initramfs-{label}.manifest.json')
    return output

  control_initrd = make_initrd('control')

  firmware_isos = {}

  def firmware_iso(selected_initrd, initrd_digest):
    # A completed fixture is reused only for the exact immutable initramfs.
    # Each VM still verifies source media digests before its own timer starts.
    if initrd_digest not in firmware_isos:
      output = work / f'firmware-{selected_initrd.stem}.iso'
      repack_log = output.with_suffix('.iso.repack.log')
      try:
        command([sys.executable, bench / 'install-speed/firmware-fixture/repack-iso.py',
          '--source', iso, '--initramfs', selected_initrd, '--output', output])
      finally:
        if repack_log.is_file() and repack_log.stat().st_size <= 20 * 1024**2:
          shutil.copyfile(repack_log, evidence / repack_log.name)
      source_manifest = output.with_suffix('.iso.manifest.json')
      fixture = json.loads(source_manifest.read_text())
      if (fixture.get('source_sha256') != ISO_SHA256 or
          fixture.get('initramfs_sha256') != initrd_digest or
          fixture.get('status') != 'verified-content-not-yet-boot-tested'):
        raise RuntimeError('Firmware fixture failed source/initramfs provenance checks')
      shutil.copyfile(source_manifest, evidence / source_manifest.name)
      provenance['firmware_fixtures'][initrd_digest] = {
        'iso_sha256': fixture['output_sha256'], 'manifest': source_manifest.name,
        'iso_bytes': fixture['output_bytes'], 'standalone_reboot_required': True,
      }
      save_json(evidence / 'experiment.json', provenance)
      firmware_isos[initrd_digest] = output
    return firmware_isos[initrd_digest]

  def vm_args(name, selected_initrd, extra=None, builder=False, key=None):
    initrd_digest = digest(selected_initrd)
    selected_iso = firmware_iso(selected_initrd, initrd_digest) if args.boot_method == 'firmware' and not builder else None
    argv = [sys.executable, bench / 'iso-vm.py', 'run', '--iso-source', harness,
        '--run-dir', work / name, '--cpus', '4', '--memory', '8192', '--accelerator', 'kvm',
        '--poll-interval', '2',
        '--test-overlay-sha256', initrd_digest]
    argv += timeout_arguments(args.install_timeout, builder=builder)
    argv += boot_arguments(args.boot_method, iso, kernel, selected_initrd,
      builder=builder, firmware_iso=selected_iso, standalone_reboot_timeout=args.standalone_reboot_timeout)
    if extra:
      argv += ['--extra-qemu-args-json', json.dumps(extra)]
    if builder:
      argv += ['--mode', 'builder', '--ssh-key', key]
    else:
      argv += ['--source-cache', args.source_cache, '--installed-boot-timeout', '300']
    return list(map(str, argv))

  rescue_context = {'repo': repo, 'work': work, 'evidence': evidence,
    'iso': iso, 'harness': harness, 'kernel': kernel, 'initrd': initrd}

  def install(name, selected_initrd, extra=None):
    run = work / name
    try:
      command(vm_args(name, selected_initrd, extra),
        timeout=args.install_timeout + 300 + (args.standalone_reboot_timeout if args.boot_method == 'firmware' else 0))
      manifest = json.loads((run / 'manifest.json').read_text())
      validation = json.loads((run / 'validation.json').read_text())
      if manifest['status'] != 'installed-and-booted' or manifest.get('qemu_exit_status') != 0 or validation.get('package_files_exit_status') != 0:
        raise RuntimeError(f'Installation not validated: {name}')
      if args.boot_method == 'firmware':
        proof = json.loads((run / 'standalone-reboot.json').read_text())
        if manifest.get('standalone_reboot_passed') is not True or proof.get('passed') is not True:
          raise RuntimeError('Firmware installation lacks a successful standalone reboot proof')
    except Exception:
      # The failed supervisor and QEMU must be stopped before the same target
      # is attached read-only to a separate live VM. Rescue never makes a
      # failed install eligible for comparison and cannot replace its error.
      if (run / 'target.qcow2').is_file():
        rescue_failed_install(run, name + '-rescue', **rescue_context)
      raise
    finally:
      collect_small(run, evidence / name)
    # The VM supervisor has exited, so this disk and disposable credentials
    # are no longer needed. All comparison inputs were copied above.
    for leaf in ('target.qcow2', 'cidata.img', 'id_ed25519', 'id_ed25519.pub', 'OVMF_VARS_4M.fd'):
      (run / leaf).unlink(missing_ok=True)
    return evidence / name

  # Calibration supplies exact packages and installation reasons for the image.
  # It does not enter the paired comparison because media topology differs.
  calibration = calibration_stage(install, control_initrd, args.diagnostic_calibrations, provenance, evidence)
  if calibration is None:
    return 0
  bundles, payload = work / 'bundles', work / 'builder-payload'
  command([sys.executable, image / 'prepare-bundles.py', fast, bundles])
  build_scripts = payload / 'usr/local/lib/omarchy-benchmark/image-builder'
  build_scripts.mkdir(parents=True)
  with tarfile.open(bundles / 'builder-bundle.tar') as archive:
    archive.extractall(build_scripts, filter='data')
  key = work / 'builder-key'
  command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'disposable-ci-image-builder', '-f', key])
  shutil.copyfile(key.with_suffix('.pub'), payload / 'usr/local/lib/omarchy-benchmark/builder-key.pub')
  builder_initrd = make_initrd('builder', payload, overlay / 'builder-preflight.sh')
  raw = work / 'root-build.raw'
  command(['qemu-img', 'create', '-f', 'raw', raw, '24G'])
  extra = ['-drive', f'file={raw},if=none,id=imagebuild,format=raw,cache=none,discard=unmap,detect-zeroes=unmap',
      '-device', 'virtio-blk-pci,drive=imagebuild,serial=OMARCHY_IMAGE_BUILD']
  builder_run = work / 'builder'
  with (evidence / 'builder-supervisor.log').open('w') as log:
    process = subprocess.Popen(vm_args('builder', builder_initrd, extra, True, key), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
      deadline = time.monotonic() + 600
      while True:
        manifest_path = builder_run / 'manifest.json'
        if manifest_path.exists() and json.loads(manifest_path.read_text()).get('status') == 'builder-ssh-ready':
          break
        if process.poll() is not None or time.monotonic() > deadline:
          raise RuntimeError('Builder VM failed to become ready')
        time.sleep(2)
      command([sys.executable, image / 'drive-guest-build.py', builder_run, calibration, evidence / 'image-build', '--timeout', '4800'], timeout=5000)
      mailbox(builder_run, {'action': 'ssh', 'command': 'systemctl poweroff', 'timeout': 30})
      process.wait(timeout=120)
      if process.returncode:
        raise RuntimeError('Builder VM shutdown failed')
    finally:
      stop_process_group(process)
      collect_small(builder_run, evidence / 'builder')
  media = work / 'image-media'
  destination = media / 'arch/x86_64/omarchy-root.btrfs.qcow2'
  command([sys.executable, image / 'compress-root-image.py', raw, evidence / 'image-build', destination])
  raw.unlink()
  for leaf in ('installer-overlay.tar', 'installer-overlay.tar.sha256', 'installer-overlay.manifest.json'):
    shutil.copyfile(bundles / leaf, media / leaf)
  command([sys.executable, image / 'bundle-qemu-img.py', '/usr/bin/qemu-img', media / 'qemu-img-live.tar'])
  supplemental = work / 'fast-image.iso'
  command(['xorriso', '-as', 'mkisofs', '-iso-level', '3', '-r', '-J', '-V', 'OMARCHY_FAST_IMAGE', '-o', supplemental, media])
  command([sys.executable, image / 'verify-image-media.py', supplemental,
      destination.with_suffix('.qcow2.json'), evidence / 'image-media-verification.json'])
  shutil.copyfile(destination.with_suffix('.qcow2.json'), evidence / 'root-image.json')
  shutil.rmtree(media)
  candidate_payload = work / 'candidate-payload'
  (candidate_payload / 'usr/local/lib/omarchy-benchmark').mkdir(parents=True)
  shutil.copyfile(image / 'activate-installer-overlay.sh', candidate_payload / 'usr/local/lib/omarchy-benchmark/activate-installer-overlay.sh')
  same_media = ['-drive', f'file={supplemental},if=none,id=fastimage,format=raw,readonly=on,cache=writeback,media=cdrom',
         '-device', 'ide-cd,drive=fastimage,bus=ide.1' + (',id=fastimage-cd' if args.boot_method == 'firmware' else '')]
  control_launch = work / 'control-launch.json'
  save_json(control_launch, vm_args('control-template', control_initrd, same_media))

  def measure_variant(label, selected_initrd, output_name, *, selected_control_initrd=None):
    selected_control_launch = control_launch
    if selected_control_initrd is not None:
      selected_control_launch = work / f'{label}-control-launch.json'
      save_json(selected_control_launch, vm_args('control-template', selected_control_initrd, same_media))
    candidate_launch = work / f'{label}-launch.json'
    save_json(candidate_launch, vm_args('candidate-template', selected_initrd, same_media))
    # repeat-installs owns fresh-run allocation, complete validation, evidence
    # sealing, alternating order, shutdown and target disk reclamation.
    series_run_root = work / f'fresh-installs-{label}'
    series_evidence = evidence / output_name
    repeat_argv = list(map(str, [sys.executable, bench / 'install-speed/repeat-installs.py',
      '--control-launch', selected_control_launch, '--candidate-launch', candidate_launch,
      '--run-root', series_run_root, '--evidence-root', series_evidence, '--pairs', '3']))
    result = subprocess.Popen(repeat_argv, start_new_session=True)
    try:
      result.wait()
    except BaseException:
      # repeat-installs needs up to 45 seconds to stop its independently grouped
      # VM gracefully, preserve failure evidence and reap QEMU before it exits.
      stop_process_group(result, grace=55)
      raise
    if result.returncode not in (0, 2):
      rescue_failed_repeat(result, series_run_root, series_evidence, label + '-failed-run-rescue', **rescue_context)
      raise RuntimeError(f'Repeated install series failed: {label}: {result.returncode}')
    provenance['comparisons'][label] = {
      'status': 'complete', 'exit_status': result.returncode,
      'comparison': f'{output_name}/comparison.json',
      'full_clock_twofold_verified': result.returncode == 0,
    }
    save_json(evidence / 'experiment.json', provenance)
    return result.returncode

  fast_reboot = bench / 'install-speed/fast-reboot'
  fast_reboot_payload = None
  direct_restore_payload = None
  finalization_overlap_payload = None
  early_control_initrd = None

  def prepare_fast_reboot_payload():
    nonlocal fast_reboot_payload
    if fast_reboot_payload is None:
      # All opt-in variants use exactly the same pinned dashboard payload.
      # Never rebuild it over an existing payload or mutate a previous fixture.
      output = work / 'candidate-fast-reboot-payload'
      with (evidence / 'fast-reboot-contract.log').open('w') as log:
        command([sys.executable, fast_reboot / 'contract-test.py', '--iso-source', fast],
          stdout=log, stderr=subprocess.STDOUT, timeout=120)
      command([sys.executable, fast_reboot / 'prepare-payload.py', '--iso-source', fast,
        '--base-payload', candidate_payload, '--output', output])
      source_manifest = output.with_name(output.name + '.manifest.json')
      shutil.copyfile(source_manifest, evidence / 'fast-reboot.manifest.json')
      provenance['fast_reboot_variant'] = json.loads(source_manifest.read_text())
      save_json(evidence / 'experiment.json', provenance)
      fast_reboot_payload = output
    return fast_reboot_payload

  def prepare_direct_restore_payload():
    nonlocal direct_restore_payload
    if direct_restore_payload is None:
      output = work / 'candidate-direct-restore-payload'
      with (evidence / 'direct-restore-contract.log').open('w') as log:
        command([sys.executable, image / 'direct-restore-payload-test.py', '--iso-source', fast],
          stdout=log, stderr=subprocess.STDOUT, timeout=120)
      command([sys.executable, image / 'direct-restore-payload.py', '--iso-source', fast,
        '--base-payload', prepare_fast_reboot_payload(), '--output', output])
      source_manifest = output.with_name(output.name + '.manifest.json')
      shutil.copyfile(source_manifest, evidence / 'direct-restore.manifest.json')
      direct_restore_payload = output
    return direct_restore_payload

  def prepare_finalization_overlap_payload():
    nonlocal finalization_overlap_payload
    if finalization_overlap_payload is None:
      finalization_overlap_payload = prepare_finalization_overlap(bench, work, evidence, fast,
        prepare_direct_restore_payload(), validation=overlap_validation)
    return finalization_overlap_payload

  for variant in args.variants:
    if variant == 'upstream-image':
      candidate_initrd = make_initrd('candidate', candidate_payload, image / 'candidate-preflight.sh')
      measure_variant(variant, candidate_initrd, 'repetitions')
    elif variant == 'image-no-package-prefetch':
      # Preserve any first comparison, including a valid result below 2x.
      # This existing upstream switch changes package warming only; image
      # verification, package inventory and installed boot stay required.
      no_prefetch_initrd = make_initrd('candidate', candidate_payload, image / 'candidate-preflight.sh',
        label='candidate-no-prefetch', disable_prefetch=True)
      measure_variant(variant, no_prefetch_initrd, 'no-prefetch-repetitions')
    elif variant in ('image-no-package-prefetch-fast-reboot', *EARLY_VARIANTS):
      # Separate opt-in experiment: retain PR145's release-gated dashboard,
      # which the original image overlay deliberately does not replace.
      payload = prepare_fast_reboot_payload()
      preflight = fast_reboot / 'candidate-preflight.sh'
      if variant in (DIRECT_RESTORE_VARIANT, *OVERLAP_VARIANTS):
        direct_payload = prepare_direct_restore_payload()
        source_manifest = direct_payload.with_name(direct_payload.name + '.manifest.json')
        payload = direct_payload
        preflight = image / 'direct-restore-preflight.sh'
        # The supplemental ISO retains its ordinary overlay. This small,
        # verified initramfs payload installs the direct patch only after
        # ordinary activation, without building another root-image ISO.
      if variant in OVERLAP_VARIANTS:
        payload, preflight, source_manifest = prepare_finalization_overlap_payload()
      if variant == SETUP_OVERLAP_VARIANT:
        payload, preflight, source_manifest = prepare_setup_overlap(bench, work, evidence, fast,
          payload, preflight, validation=setup_validation)
      if variant in EARLY_VARIANTS:
        configuration = early_variant_configuration(variant)
        early_control_initrd, early_candidate = early_preflight_pair(make_initrd, payload, preflight,
          variant=variant, control=early_control_initrd)
        provenance[configuration['provenance_key']] = early_preflight_provenance(variant,
          early_control_initrd, early_candidate, preflight, args.source_cache, args.boot_method)
        if variant in (DIRECT_RESTORE_VARIANT, *OVERLAP_VARIANTS):
          provenance[configuration['provenance_key']]['payload_manifest_sha256'] = digest(source_manifest)
        save_json(evidence / 'experiment.json', provenance)
        measure_variant(variant, early_candidate, configuration['output_name'],
          selected_control_initrd=early_control_initrd)
      else:
        fast_reboot_initrd = make_initrd('candidate', payload, fast_reboot / 'candidate-preflight.sh',
          label='candidate-no-prefetch-fast-reboot', disable_prefetch=True)
        measure_variant(variant, fast_reboot_initrd, 'no-prefetch-fast-reboot-repetitions')
  provenance['status'] = 'comparisons-complete'
  save_json(evidence / 'experiment.json', provenance)
  # Each variant's actual result remains separate. Never present a failed
  # first variant as successful merely because the follow-up reaches the goal.
  return 0 if any(row['full_clock_twofold_verified'] for row in provenance['comparisons'].values()) else 2


class ArgumentParser(argparse.ArgumentParser):
  def error(self, message):
    # Exit 2 is reserved for complete, valid comparisons below the speed goal.
    self.print_usage(sys.stderr)
    self.exit(1, f'{self.prog}: error: {message}\n')


def parse_arguments(argv=None):
  parser = ArgumentParser(description=__doc__)
  parser.add_argument('--repo', required=True, type=Path)
  parser.add_argument('--work', required=True, type=Path)
  parser.add_argument('--evidence', required=True, type=Path)
  parser.add_argument('--install-timeout', type=int, default=1800, metavar='SECONDS',
    help='Positive readiness timeout shared by all installation VMs; builder timeout remains 5400 seconds')
  parser.add_argument('--standalone-reboot-timeout', type=int, default=600, metavar='SECONDS',
    help='Positive standalone reboot timeout shared by firmware calibration and measured installations')
  parser.add_argument('--diagnostic-calibrations', type=int, metavar='N',
    help='Run 1..6 fresh stock calibration installs for boot diagnosis, then stop before image preparation/comparisons')
  parser.add_argument('--boot-method', choices=('direct', 'firmware'), default='direct',
    help='Direct kernel boot (default), or verified ISO repacking with firmware boot and standalone reboot validation')
  parser.add_argument('--source-cache', choices=('conditioned', 'cold'), default='conditioned',
    help='Source media cache policy; cold requires verified eviction in the VM runner')
  parser.add_argument('--variants', nargs='+', choices=('upstream-image', 'image-no-package-prefetch', 'image-no-package-prefetch-fast-reboot', *EARLY_VARIANTS),
    default=['upstream-image', 'image-no-package-prefetch'], help='Variants to measure, in order; three fresh pairs each')
  args = parser.parse_args(argv)
  if args.install_timeout <= 0:
    parser.error('--install-timeout must be a positive integer')
  if args.standalone_reboot_timeout <= 0:
    parser.error('--standalone-reboot-timeout must be a positive integer')
  if args.diagnostic_calibrations is not None:
    if not 1 <= args.diagnostic_calibrations <= 6:
      parser.error('--diagnostic-calibrations must be an integer from 1 to 6')
    if args.source_cache != 'cold' or args.boot_method != 'firmware':
      parser.error('--diagnostic-calibrations requires --source-cache cold --boot-method firmware')
  if len(set(args.variants)) != len(args.variants):
    parser.error('variants must be distinct')
  for variant in EARLY_VARIANTS:
    if variant in args.variants and (args.source_cache != 'cold' or args.boot_method != 'firmware'):
      parser.error(f'{variant} requires --source-cache cold --boot-method firmware')
  return args


def main():
  args = parse_arguments()
  args.evidence.mkdir(parents=True, exist_ok=True)
  failure = None
  try:
    return execute(args)
  except BaseException as error:
    failure = {'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)}
    raise
  finally:
    if failure:
      save_json(args.evidence / 'failure.json', failure)
    for name in ('calibration', 'builder'):
      source = args.work / name
      if source.exists():
        collect_small(source, args.evidence / name)


if __name__ == '__main__':
  def interrupted(signum, frame):
    raise InterruptedError(f'Received signal {signum}')
  signal.signal(signal.SIGTERM, interrupted)
  signal.signal(signal.SIGINT, interrupted)
  raise SystemExit(main())
