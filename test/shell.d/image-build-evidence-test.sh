#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

repo = Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix='omarchy-build-evidence-') as temporary:
  root = Path(temporary)
  run = root / 'builder'
  baseline = root / 'baseline'
  output = root / 'evidence'
  (run / 'requests').mkdir(parents=True)
  (run / 'responses').mkdir()
  baseline.mkdir()
  (baseline / 'package-manifest.txt').write_text('filesystem 1-1\n')
  (baseline / 'package-explicit.txt').write_text('filesystem\n')
  data = io.BytesIO()
  with tarfile.open(fileobj=data, mode='w:gz') as archive:
    for name, content in {'image-package-files.txt': b'warning: filesystem: /etc/resolv.conf (No such file or directory)\n',
                          'build-status.txt': b'BUILD_COMPLETE\n'}.items():
      info = tarfile.TarInfo(name)
      info.size = len(content)
      archive.addfile(info, io.BytesIO(content))
  encoded = base64.b64encode(data.getvalue()).decode()
  child = subprocess.Popen([sys.executable, str(repo / 'test/benchmarks/install-speed/image/drive-guest-build.py'),
    str(run), str(baseline), str(output), '--timeout', '20'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
  handled = set()
  deadline = time.monotonic() + 15
  while child.poll() is None and time.monotonic() < deadline:
    for path in (run / 'requests').glob('*.json'):
      if path.name in handled:
        continue
      request = json.loads(path.read_text())
      command = request['command']
      if command.startswith('systemctl show'):
        stdout = 'ActiveState=failed\nResult=exit-code\nExecMainStatus=1\n'
      elif command.startswith('cat /var/log/'):
        stdout = 'Finished package install; validation failed\n'
      elif command.startswith('python3 -c '):
        stdout = encoded
      else:
        stdout = ''
      response = run / 'responses' / path.name
      staged = response.with_suffix('.tmp')
      staged.write_text(json.dumps({'ok':True, 'result':{'returncode':0,'stdout':stdout,'stderr':''}}))
      staged.rename(response)
      handled.add(path.name)
    time.sleep(0.02)
  try:
    stdout, stderr = child.communicate(timeout=2)
  except subprocess.TimeoutExpired:
    child.kill();child.wait()
    raise AssertionError('failed-unit evidence collection did not terminate')
  assert child.returncode != 0, 'a failed unit was accepted using a stale completion marker'
  assert 'Do not compress' in stderr
  assert (output / 'image-package-files.txt').read_text().startswith('warning: filesystem:')
  assert (output / 'guest-build.log').read_text().startswith('Finished package install')
  metadata = json.loads((output / 'build-run.json').read_text())
  assert metadata['status'] == 'failed' and metadata['unit_status']['ExecMainStatus'] == '1'
  assert len(handled) == 4, 'unit failure skipped partial-output collection'
print('ok - failed real mailbox build retains validation diagnostics and rejects stale completion')
PY
