#!/usr/bin/env python3
"""Check actual private logger binds in a disposable guest's minimal /tmp chroot."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import statistics
import subprocess
import tempfile
import time


def digest(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def minimal_bash_root(target):
  binaries = [Path(shutil.which(name)) for name in ('bash', 'date')]
  linked = ''.join(subprocess.run(['ldd', str(binary)], check=True, capture_output=True, text=True).stdout for binary in binaries)
  libraries = {Path(path) for path in re.findall(r'(/[^\s()]+)', linked)}
  if not libraries or len(libraries) > 12 or sum(path.stat().st_size for path in libraries) > 32 * 1024**2:
    raise RuntimeError('Unexpected minimal Bash shared-library inventory')
  copied = {}
  for source in [*binaries, *sorted(libraries)]:
    destination = target / source.relative_to('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(stat.S_IMODE(source.stat().st_mode))
    copied[str(source)] = digest(source)
  bash = target / 'usr/bin/bash'
  if not bash.exists():
    bash.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binaries[0], bash)
    bash.chmod(0o755)
  # Preserve either /bin or /usr/bin loader paths without copying a full root.
  bin_bash = target / 'bin/bash'
  if not bin_bash.exists():
    bin_bash.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bash, bin_bash)
    bin_bash.chmod(0o755)
  for name in ('proc', 'dev', 'sys', 'run', 'tmp', 'etc'):
    (target / name).mkdir(exist_ok=True)
  (target / 'tmp').chmod(0o1777)
  (target / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\nbench:x:65534:65534:bench:/tmp:/bin/bash\n')
  (target / 'etc/group').write_text('root:x:0:\nbench:x:65534:\n')
  (target / 'etc/resolv.conf').touch()
  return copied


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--guard', type=Path, required=True)
  parser.add_argument('--original-helper', type=Path, required=True)
  parser.add_argument('--optimized-helper', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  parser.add_argument('--timing-pairs', type=int, default=3)
  args = parser.parse_args()
  if os.geteuid() != 0 or args.output.exists() or not 0 <= args.timing_pairs <= 5:
    parser.error('A disposable root guest and fresh output path are required')
  for tool in ('unshare', 'mount', 'umount', 'findmnt', 'arch-chroot', 'bash', 'ldd'):
    if not shutil.which(tool):
      raise RuntimeError('Guest contract dependency missing: ' + tool)
  spec = importlib.util.spec_from_file_location('actual_logging_guard', args.guard)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  assert digest(args.original_helper) == module.ORIGINAL_LOGGER_SHA256
  assert digest(args.optimized_helper) == module.LOGGER_SHA256
  report = {'schema_version': 1, 'scope': 'Actual private file bind and arch-chroot on an owned minimal /tmp fixture; no installed disk, package changes or installation-speed claim',
    'source_sha256': {name: digest(path) for name, path in (('guard.py', args.guard), ('original-helper', args.original_helper), ('optimized-helper', args.optimized_helper))},
    'cases': []}
  with tempfile.TemporaryDirectory(prefix='omarchy-logging-bind-guest-', dir='/tmp') as temporary:
    work = Path(temporary)
    target = work / 'target'
    target.mkdir(mode=0o755)
    report['copied_guest_binary_sha256'] = minimal_bash_root(target)
    original_date = (target / 'usr/bin/date').read_bytes()
    staged = work / 'payload'
    staged.mkdir()
    shutil.copyfile(args.guard, staged / 'guard.py')
    shutil.copyfile(args.optimized_helper, staged / 'logging.sh')
    (staged / 'guard.py').chmod(0o644)
    (staged / 'logging.sh').chmod(0o644)
    logger = target / module.RELATIVE_LOGGER
    logger.parent.mkdir(parents=True)
    shutil.copyfile(args.original_helper, logger)
    logger.chmod(0o644)
    original = module.file_record(logger, module.ORIGINAL_LOGGER_SHA256)
    # arch-chroot mounts its own /tmp, so fixture inputs and the handshake
    # live under an ordinary target directory that survives its API mounts.
    fixture = target / 'opt/logging-bind-contract'
    fixture.mkdir(parents=True)
    (fixture / 'leaf.sh').write_text("printf 'LEAF_OUTPUT\\n'\n")
    (fixture / 'leaf.sh').chmod(0o644)
    # If the optimized helper falls back to an external date, fail visibly.
    (target / 'usr/bin/date').write_text('#!/bin/bash\nprintf "UNEXPECTED_DATE\\n" >&2\nexit 91\n')
    (target / 'usr/bin/date').chmod(0o755)
    environment = {**os.environ, 'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'TZ': 'UTC',
      'OMARCHY_LOG_TO_STDOUT': '1', 'OMARCHY_START_TIME': '2026-09-06 00:00:00', 'OMARCHY_START_EPOCH': '1788652800'}
    for label, user, expected_status in (('root-success', None, 0), ('user-success', 'bench', 0), ('root-failure', None, 37)):
      expected_uid = 65534 if user else 0
      script = f'''set -euo pipefail
[[ $EUID == {expected_uid} ]]
source /usr/share/omarchy/install/helpers/logging.sh
[[ $OMARCHY_START_TIME == '2026-09-06 00:00:00' ]]
start_install_log
if {{ printf corrupt >>/usr/share/omarchy/install/helpers/logging.sh; }} 2>/dev/null; then exit 71; fi
run_logged /opt/logging-bind-contract/leaf.sh
bash -c 'source /usr/share/omarchy/install/helpers/logging.sh; run_logged /opt/logging-bind-contract/leaf.sh'
stop_install_log
printf 'GUARD_GUEST_UID=%s\\n' "$EUID"
'''
      handshake = label == 'root-success'
      if handshake:
        os.mkfifo(fixture / 'release', 0o600)
        script += "printf ready >/opt/logging-bind-contract/ready\nread -r release </opt/logging-bind-contract/release\n[[ $release == continue ]]\n"
      script += f'exit {expected_status}\n'
      command = [*module.PRIVATE_PREFIX, 'arch-chroot', *(['-u', user] if user else []), str(target), '/usr/bin/bash', '-c', script]
      started = time.monotonic()
      child = subprocess.Popen(['/usr/bin/python3', str(staged / 'guard.py'), '--target', str(target), '--', *command],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
      try:
        parent_observed = False
        if handshake:
          deadline = time.monotonic() + 20
          while not (fixture / 'ready').exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
          if not (fixture / 'ready').exists():
            if child.poll() is None:
              os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=10)
            raise RuntimeError(f'Real mounted child did not reach the isolation handshake; '
              f'exit status {child.returncode}: {stdout}\n{stderr}')
          if module.mount_record(logger) is not None or module.file_record(logger, module.ORIGINAL_LOGGER_SHA256) != original:
            raise RuntimeError('Private logger overlay became visible to its parent')
          parent_observed = True
          with (fixture / 'release').open('w') as release:
            release.write('continue\n')
        stdout, stderr = child.communicate(timeout=30)
      finally:
        if child.poll() is None:
          os.killpg(child.pid, signal.SIGKILL)
          child.wait(timeout=10)
      if child.returncode != expected_status:
        raise RuntimeError(f'{label}: unexpected status {child.returncode}: {stdout}\n{stderr}')
      if stdout.count('LEAF_OUTPUT\n') != 2 or f'GUARD_GUEST_UID={expected_uid}\n' not in stdout or 'UNEXPECTED_DATE' in stdout + stderr:
        raise RuntimeError(f'{label}: logged child work, UID or builtin clock proof failed')
      if len(re.findall(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] (?:Starting|Completed): /opt/logging-bind-contract/leaf.sh', stdout)) != 4:
        raise RuntimeError('Timestamp format or leaf success records differ')
      if module.mount_record(logger) is not None or module.file_record(logger, module.ORIGINAL_LOGGER_SHA256) != original:
        raise RuntimeError('Original package file/mount state changed after child exit')
      report['cases'].append({'case': label, 'passed': True, 'exit_status': child.returncode,
        'uid': expected_uid, 'parent_observed_original_while_child_mounted': parent_observed,
        'original_file_and_metadata_unchanged': True, 'parent_mount_absent': True,
        'seconds_for_functional_check_only': time.monotonic() - started, 'stdout': stdout, 'stderr': stderr})
    report['fixture'] = str(work)
    report['source_helper_unchanged'] = digest(staged / 'logging.sh') == module.LOGGER_SHA256
    if args.timing_pairs:
      # Use the real guest date executable for before/after lifecycle timing.
      # The preceding refusal sentinel is deliberately not a timed executable.
      (target / 'usr/bin/date').write_bytes(original_date)
      (target / 'usr/bin/date').chmod(0o755)
      staged_spec = importlib.util.spec_from_file_location('staged_logging_guard', staged / 'guard.py')
      staged_guard = importlib.util.module_from_spec(staged_spec)
      staged_spec.loader.exec_module(staged_guard)
      samples = []
      for pair in range(args.timing_pairs):
        order = ('baseline', 'candidate') if pair % 2 == 0 else ('candidate', 'baseline')
        for label in order:
          phase_rows = []
          total_started = time.monotonic()
          # The firewall candidate has 49 serial system leaves, followed by
          # firewall, user and index invocations. This fixture runs them in
          # sequence to include all four mount lifecycles, not to simulate
          # native boot-branch overlap or claim installation timing.
          for number, leaves in enumerate((49, 1, 12, 1)):
            output = work / f'{pair}-{label}-{number}.log'
            body = 'set -euo pipefail\nsource /usr/share/omarchy/install/helpers/logging.sh\n'
            if number == 0:
              body += 'start_install_log\n'
            body += f'for ((i=0;i<{leaves};i++)); do run_logged /opt/logging-bind-contract/leaf.sh; done\n'
            if number == 0:
              body += 'stop_install_log\n'
            command = [*module.PRIVATE_PREFIX, 'arch-chroot', str(target), '/usr/bin/bash', '-c', body]
            phase_started = time.monotonic()
            with output.open('w') as log:
              if label == 'baseline':
                result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False)
                status = result.returncode
              else:
                # Match production's module call: no extra outer interpreter.
                old_environment = os.environ.copy()
                old_stdout, old_stderr = os.dup(1), os.dup(2)
                try:
                  os.environ.update(environment)
                  os.dup2(log.fileno(), 1)
                  os.dup2(log.fileno(), 2)
                  status = staged_guard.run(target, command)
                finally:
                  os.dup2(old_stdout, 1)
                  os.dup2(old_stderr, 2)
                  os.close(old_stdout)
                  os.close(old_stderr)
                  os.environ.clear()
                  os.environ.update(old_environment)
            elapsed = time.monotonic() - phase_started
            text = output.read_text()
            if status != 0 or text.count('LEAF_OUTPUT\n') != leaves or text.count('] Completed: /opt/logging-bind-contract/leaf.sh\n') != leaves:
              raise RuntimeError(f'Timed component omitted or failed logged work: {pair}/{label}/{number}: {text}')
            phase_rows.append({'leaves': leaves, 'seconds': elapsed})
          samples.append({'pair': pair + 1, 'label': label, 'seconds': time.monotonic() - total_started, 'invocations': phase_rows})
      medians = {label: statistics.median(row['seconds'] for row in samples if row['label'] == label) for label in ('baseline', 'candidate')}
      report['component_timing'] = {'scope': 'TCG guest only; real Bash/date and all four private chroot/guard lifecycles, 63 trivial leaves; no native or full-install inference',
        'samples': samples, 'median_seconds': medians, 'median_saved_seconds': medians['baseline'] - medians['candidate'],
        'median_speedup': medians['baseline'] / medians['candidate'], 'all_logged_leaves_verified': True}
  report['fixture_removed'] = not Path(report['fixture']).exists()
  report['status'] = 'passed'
  args.output.write_text(json.dumps(report, indent=2) + '\n')
  print(json.dumps(report, indent=2))


if __name__ == '__main__':
  main()
