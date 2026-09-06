#!/usr/bin/env python3
"""Append pinned firewall scheduling to the complete finalization-overlap payload."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('firewall_overlap_patch', HERE / 'patch.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
BASE_VARIANT = 'image-no-package-prefetch-fast-reboot-early-verify-direct-restore-overlap'
VARIANT = BASE_VARIANT + '-firewall'
BASE_PHASES = Path('usr/local/lib/omarchy-benchmark/localdb-overlap/phases_impl.py')
PAYLOAD_PATH = Path('usr/local/lib/omarchy-benchmark/firewall-overlap')


def digest(data):
  return hashlib.sha256(data).hexdigest()


def inventory(directory):
  rows = []
  for path in sorted(directory.rglob('*')):
    name = path.relative_to(directory).as_posix()
    if path.is_symlink() or not (path.is_file() or path.is_dir()) or '\n' in name or '\r' in name:
      raise ValueError('Payload requires regular files/directories with unambiguous names')
    if path.is_file():
      rows.append({'path': name, 'sha256': digest(path.read_bytes()),
        'mode': oct(stat.S_IMODE(path.stat().st_mode)), 'bytes': path.stat().st_size})
  return rows


def prepare(checkout, base_payload, base_preflight, output):
  manifest_path = output.with_name(output.name + '.manifest.json')
  if output.exists() or manifest_path.exists() or base_payload.resolve() in output.resolve().parents:
    raise ValueError('Firewall payload requires fresh output outside its base')
  base_manifest_bytes = base_payload.with_name(base_payload.name + '.manifest.json').read_bytes()
  base_manifest = json.loads(base_manifest_bytes)
  before = inventory(base_payload)
  if (base_manifest.get('upstream_commit') != patch.PIN or base_manifest.get('variant') != BASE_VARIANT or
      base_manifest.get('component') != 'foreground-animation-overlap' or base_manifest.get('files') != before):
    raise ValueError('Base payload differs from its complete pinned overlap inventory')
  if base_preflight.is_symlink() or not base_preflight.is_file():
    raise ValueError('Base preflight must be a regular file')
  base_preflight_bytes = base_preflight.read_bytes()
  if base_manifest.get('preflight_sha256') != digest(base_preflight_bytes):
    raise ValueError('Base preflight differs from its provenance')
  if (base_payload / PAYLOAD_PATH).exists():
    raise ValueError('Base payload already contains firewall overlap')
  patched = patch.patch_source((base_payload / BASE_PHASES).read_bytes())
  compile(patched, 'firewall-overlap-phases', 'exec')
  sources = {
    'phases_impl.py': (patched, 0o644),
    'base-preflight.sh': (base_preflight_bytes, 0o755),
    'LICENSE': (subprocess.check_output(['git', '-C', str(checkout), 'show', f'{patch.PIN}:LICENSE']), 0o644),
  }
  shutil.copytree(base_payload, output)
  staged = output / PAYLOAD_PATH
  staged.mkdir(parents=True)
  for name, (data, mode) in sources.items():
    path = staged / name
    path.write_bytes(data)
    path.chmod(mode)
  checksum = staged / 'payload.sha256'
  checksum.write_text(''.join(f'{digest(data)}  {name}\n' for name, (data, _) in sorted(sources.items())))
  checksum.chmod(0o644)
  after = inventory(output)
  if [row for row in after if row['path'] in {item['path'] for item in before}] != before:
    raise ValueError('Firewall staging changed inherited payload bytes or metadata')
  manifest = {
    'schema_version': 1, 'variant': VARIANT, 'component': 'firewall-overlap',
    'base_variant': BASE_VARIANT, 'upstream_commit': patch.PIN,
    'source_phases_sha256': patch.SOURCE_SHA256, 'firewall_phases_sha256': digest(patched),
    'target_source_sha256': patch.TARGET_SHA256,
    'base_payload_manifest_sha256': digest(base_manifest_bytes),
    'base_preflight_sha256': digest(base_preflight_bytes),
    'preflight_sha256': digest((HERE / 'preflight.sh').read_bytes()),
    'supplemental_image_changed': False, 'target_package_files_changed': False,
    'activation_order': ['inherited preflight exactly once', 'verify localdb phases', 'install verified firewall phases'],
    'timing_substep': 'Configuring firewall',
    'changes': ['Run the unchanged logged firewall leaf first in the existing joined user branch',
      'Retain serial firewall setup for deferred provisioning',
      'Complete all firewall, SSH and Tailscale changes before unchanged indexing and boot validation'],
    'files': after,
  }
  manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
  return manifest


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--iso-source', type=Path, required=True)
  parser.add_argument('--base-payload', type=Path, required=True)
  parser.add_argument('--base-preflight', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  print(json.dumps(prepare(args.iso_source, args.base_payload, args.base_preflight, args.output), indent=2))


if __name__ == '__main__':
  main()
