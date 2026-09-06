#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

repo = Path(sys.argv[1])
script = repo / 'test/benchmarks/compare-installs.py'
spec = importlib.util.spec_from_file_location('sealed_comparison', script)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)
repeat_script = repo / 'test/benchmarks/install-speed/repeat-installs.py'
spec = importlib.util.spec_from_file_location('sealed_repeat', repeat_script)
repeat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repeat)
results = repo / 'test/benchmarks/install-speed/results'
historical = results / 'kvm-attempts/33989571580/control-pair01'
raw_calibration = results / 'kvm-attempts/33988339199/calibration'


def rejects(function, message):
  try:
    function()
  except ValueError as error:
    assert message in str(error), str(error)
  else:
    raise AssertionError('Accepted invalid evidence: ' + message)


# These are captured historical files, never rewritten to match today's code.
original_seal = (historical / 'seal.json').read_bytes()
seal = comparison.verify_seal(historical)
assert seal['comparator_sha256'] != hashlib.sha256(script.read_bytes()).hexdigest()
assert comparison.read_run(historical)['standalone_reboot']['passed']
assert (historical / 'seal.json').read_bytes() == original_seal
assert comparison.verify_seal(historical) == json.loads(original_seal)
# Unpaired functional smoke seals predate code-hash provenance; do not invent it.
smoke = results / 'local-image-smoke-2026-09-05/passed-02'
assert 'comparator_sha256' not in comparison.verify_seal(smoke)
assert comparison.read_run(smoke)['standalone_reboot']['passed']
rejects(lambda: comparison.read_run(raw_calibration), 'evidence seal required')
assert comparison.read_run(raw_calibration, allow_unsealed=True)['packages']

with tempfile.TemporaryDirectory(prefix='omarchy-seal-contract-') as temporary:
  root = Path(temporary)
  copied = root / 'historical'
  shutil.copytree(historical, copied)
  seal_path = copied / 'seal.json'
  # Even semantically identical JSON must match the captured bytes.
  manifest = copied / 'manifest.json'
  original_manifest = manifest.read_bytes()
  manifest.write_bytes(original_manifest + b'\n')
  for allow in (False, True):
    rejects(lambda: comparison.read_run(copied, allow_unsealed=allow), 'sealed evidence changed: manifest.json')
  manifest.write_bytes(original_manifest)
  # Optional recorded logs are protected too, not just parsed timing inputs.
  log = copied / 'serial.log'
  original_log = log.read_bytes()
  log.write_bytes(original_log + b'\n')
  rejects(lambda: comparison.read_run(copied), 'sealed evidence changed: serial.log')
  log.write_bytes(original_log)
  for name, message in (('package-files.txt', 'omits comparison inputs'),
                        ('standalone-reboot.json', 'omits standalone inputs')):
    incomplete = json.loads(original_seal)
    del incomplete['files'][name]
    seal_path.write_text(json.dumps(incomplete))
    rejects(lambda: comparison.read_run(copied), message)
  seal_path.write_bytes(original_seal)
  assert comparison.read_run(copied)['packages'] == comparison.read_run(historical)['packages']
  seal_path.unlink()
  rejects(lambda: comparison.read_run(copied), 'evidence seal required')
  assert comparison.read_run(copied, allow_unsealed=True)['packages']
  cli = [sys.executable, str(script), '--baseline', str(copied), '--candidate', str(copied),
         '--output', str(root / 'never-a-performance-result.json')]
  strict = subprocess.run(cli, capture_output=True, text=True)
  assert strict.returncode != 0 and 'evidence seal required' in strict.stderr
  compatible = subprocess.run([*cli, '--allow-unsealed'], capture_output=True, text=True)
  assert compatible.returncode != 0 and 'each sample must be a distinct fresh installation' in compatible.stderr
  assert not (root / 'never-a-performance-result.json').exists()

  # Exercise the actual series path: changing an older seal's file between
  # samples must fail before a new comparison can be accepted or reclaimed.
  state, evidence = root / 'state', root / 'evidence'
  run_root = state / 'runs'
  template = root / 'launch.json'
  template.write_text(json.dumps([sys.executable, str(repeat.RUNNER), 'run', '--iso', '/unused-fixture.iso']))
  launches = []
  def execute(argv, run, log, timeout):
    shutil.copytree(raw_calibration, run)
    (run / 'target.qcow2').write_bytes(b'disposable contract sentinel')
    launches.append(run)
    if len(launches) == 2:
      older = evidence / 'runs' / launches[0].name / 'manifest.json'
      older.chmod(0o600)
      older.write_bytes(older.read_bytes() + b'\n')
    return 0
  argv = [str(repeat_script), '--control-launch', str(template), '--candidate-launch', str(template),
          '--run-root', str(run_root), '--evidence-root', str(evidence), '--vm-state-root', str(state)]
  with patch.object(sys, 'argv', argv), patch.object(repeat, 'execute_sample', execute):
    rejects(repeat.main, 'sealed evidence changed: manifest.json')
  assert len(launches) == 2
  assert not (launches[0] / 'target.qcow2').exists()
  assert (launches[1] / 'target.qcow2').exists()
  series = json.loads((evidence / 'series.json').read_text())
  assert series['status'] == 'failed' and len(series['runs']) == 1
  assert not (evidence / 'comparison.json').exists()

print('ok - sealed inputs reject changed bytes and omissions while retaining historical provenance and explicit raw compatibility')
PY
