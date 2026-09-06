#!/bin/bash
# Apply after inherited overlap activation; only the serial system finalizer opts in.
set -euo pipefail
payload=/usr/local/lib/omarchy-benchmark/logging-bind
live=/usr/share/omarchy-iso/orchestrator/phases_impl.py
source_sha=$(python3 - "$payload" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

payload = Path(sys.argv[1])
expected_modes = {
  "activation.json": 0o644,
  "base-preflight.sh": 0o755,
  "guard.py": 0o644,
  "LICENSE.omarchy": 0o644,
  "LICENSE.omarchy-iso": 0o644,
  "logging.sh": 0o644,
  "phases_impl.py": 0o644,
  "payload.sha256": 0o644,
}
base_variant = "image-no-package-prefetch-fast-reboot-early-verify-direct-restore-overlap"
approved_bases = {
  base_variant: ("6914997592990435c723688e594ed189192e961423324b505a66be0de1948128",
    "510a502d0f4388a43b137e2e22e1b65eaa10a271b299c8c31131365bbbd94b5a", base_variant + "-logging-bind"),
  base_variant + "-firewall": ("f5235ae1ed7e6a783978d2f51e49fc3e0d44c687f218967a599a104101e0070c",
    "e58a6ed01ff672115ebf8a95be4cb40b622e17c1066393a4aa7954d8378a44eb", base_variant + "-firewall-logging"),
}
for component in (payload, *payload.parents):
  if component.is_symlink():
    raise SystemExit(f"Logging bind payload traverses a symlink: {component}")
if not payload.is_dir() or {path.name for path in payload.iterdir()} != set(expected_modes):
  raise SystemExit("Logging bind payload has missing or unexpected staged files")
for name, mode in expected_modes.items():
  path = payload / name
  if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != mode:
    raise SystemExit(f"Logging bind payload requires the exact regular-file mode: {name}")
checksums = {}
for row in (payload / "payload.sha256").read_text().splitlines():
  match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", row)
  if not match or match[2] in checksums:
    raise SystemExit("Logging bind payload checksum inventory is malformed or duplicated")
  checksums[match[2]] = match[1]
if set(checksums) != set(expected_modes) - {"payload.sha256"}:
  raise SystemExit("Logging bind payload checksum inventory is incomplete")
for name, expected in checksums.items():
  if hashlib.sha256((payload / name).read_bytes()).hexdigest() != expected:
    raise SystemExit(f"Logging bind payload checksum differs: {name}")
activation = json.loads((payload / "activation.json").read_text())
if activation.get("logging_scope") != "serial-system-finalizer-only":
  raise SystemExit("Logging bind activation requires serial-system-finalizer-only scope")
approved = approved_bases.get(activation.get("base_variant"))
if (type(activation.get("schema_version")) is not int or activation.get("schema_version") != 1 or approved is None
    or (activation.get("source_phases_sha256"), activation.get("base_preflight_sha256"),
      activation.get("variant")) != approved):
  raise SystemExit("Logging bind activation does not identify an exact approved base")
for key, filename in (("phases_sha256", "phases_impl.py"), ("base_preflight_sha256", "base-preflight.sh"),
    ("logger_sha256", "logging.sh"), ("guard_sha256", "guard.py")):
  if activation.get(key) != checksums[filename]:
    raise SystemExit(f"Logging bind activation hash differs: {filename}")
if (activation.get("original_logger_sha256") != "61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27"
    or activation.get("logger_sha256") != "1d8151adb150bc1dfe930b30e7039978591add500a132b9951152d7a8a23d715"):
  raise SystemExit("Logging bind activation requires the pinned original and optimized loggers")
print(activation["source_phases_sha256"])
PY
)
bash "$payload/base-preflight.sh"
python3 - "$live" "$source_sha" <<'PY'
import hashlib
from pathlib import Path
import sys

live = Path(sys.argv[1])
if any(path.is_symlink() for path in (live, *live.parents)) or not live.is_file():
  raise SystemExit("Logging bind requires a regular inherited live phases source")
if hashlib.sha256(live.read_bytes()).hexdigest() != sys.argv[2]:
  raise SystemExit("Logging bind inherited live phases differ from the pinned source")
PY
install -m 0644 "$payload/phases_impl.py" "$live"
cmp "$payload/phases_impl.py" "$live"
echo 'Verified logging bind activated only for the serial system finalizer; other setup calls retain the original logger.'
