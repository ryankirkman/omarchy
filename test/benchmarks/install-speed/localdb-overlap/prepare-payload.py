#!/usr/bin/env python3
"""Stage required locate indexing in the existing joined finalization branch."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('localdb_overlap_patch', HERE / 'patch.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
PIN = 'dbffaa6c65344d644627a023c28661e08382b8fa'
BASE_VARIANT = 'image-no-package-prefetch-fast-reboot-early-verify-direct-restore'
VARIANT = BASE_VARIANT + '-localdb-overlap'
PAYLOAD_PATH = Path('usr/local/lib/omarchy-benchmark/localdb-overlap')
PHASES_SHA256 = '6914997592990435c723688e594ed189192e961423324b505a66be0de1948128'


def digest(data):
  return hashlib.sha256(data).hexdigest()


def file_inventory(directory):
  files = []
  for path in sorted(directory.rglob('*')):
    name = path.relative_to(directory).as_posix()
    if path.is_symlink() or not (path.is_file() or path.is_dir()) or '\n' in name or '\r' in name:
      raise ValueError('Payload requires regular files/directories with unambiguous names')
    if path.is_file():
      files.append({'path': name, 'sha256': digest(path.read_bytes()),
        'mode': oct(stat.S_IMODE(path.stat().st_mode)), 'bytes': path.stat().st_size})
  return files


def prepare(checkout, base_payload, output):
  manifest_path = output.with_name(output.name + '.manifest.json')
  if output.resolve().is_relative_to(base_payload.resolve()):
    raise ValueError('Localdb overlap output must be outside the base payload')
  if output.exists() or manifest_path.exists():
    raise ValueError('Localdb overlap requires fresh output paths')
  base_manifest_path = base_payload.with_name(base_payload.name + '.manifest.json')
  base_manifest = json.loads(base_manifest_path.read_text())
  if (base_manifest.get('upstream_commit') != PIN or base_manifest.get('variant') != BASE_VARIANT
      or base_manifest.get('direct_phases_sha256') != patch.SOURCE_SHA256
      or base_manifest.get('target_cache') != 'none'):
    raise ValueError('Localdb overlap requires the pinned direct-restore payload')
  if base_manifest.get('files') != file_inventory(base_payload):
    raise ValueError('Direct-restore payload differs from its complete recorded inventory')
  base_preflight = (HERE.parent / 'image/direct-restore-preflight.sh').read_bytes()
  if base_manifest.get('preflight_sha256') != digest(base_preflight):
    raise ValueError('Direct-restore preflight differs from the base provenance')
  if (base_payload / PAYLOAD_PATH).exists():
    raise ValueError('Base payload already contains localdb overlap staging')
  original = base_payload / 'usr/local/lib/omarchy-benchmark/direct-restore/phases_impl.py'
  patched = patch.patch_source(original.read_bytes())
  if digest(patched) != PHASES_SHA256:
    raise ValueError('Localdb overlap patch differs from the expected source')
  license_data = subprocess.check_output(['git', '-C', str(checkout), 'show', f'{PIN}:LICENSE'])
  shutil.copytree(base_payload, output)
  staged = output / PAYLOAD_PATH
  staged.mkdir(parents=True)
  sources = {
    'phases_impl.py': (patched, 0o644),
    'direct-restore-preflight.sh': (base_preflight, 0o755),
    'LICENSE': (license_data, 0o644),
  }
  for name, (data, mode) in sources.items():
    path = staged / name
    path.write_bytes(data)
    path.chmod(mode)
  (staged / 'payload.sha256').write_text(''.join(
    f'{digest(data)}  {name}\n' for name, (data, _) in sorted(sources.items())))
  (staged / 'payload.sha256').chmod(0o644)
  manifest = {
    'schema_version': 1, 'variant': VARIANT, 'component': 'localdb-overlap',
    'base_variant': BASE_VARIANT, 'upstream_commit': PIN,
    'source_phases_sha256': patch.SOURCE_SHA256, 'localdb_phases_sha256': PHASES_SHA256,
    'target_source_sha256': patch.TARGET_SHA256,
    'base_payload_manifest_sha256': digest(base_manifest_path.read_bytes()),
    'preflight_sha256': digest((HERE / 'preflight.sh').read_bytes()),
    'supplemental_image_changed': False, 'target_cache': 'none',
    'activation_order': ['ordinary image overlay', 'guarded fast reboot', 'direct restore', 'localdb overlap'],
    'timing_substep': 'Indexing installed files',
    'changes': ['Run unchanged updatedb once at the end of the existing user branch, joined before validation and snapshot creation'],
    'files': file_inventory(output),
  }
  manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
  return manifest


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--iso-source', required=True, type=Path)
  parser.add_argument('--base-payload', required=True, type=Path)
  parser.add_argument('--output', required=True, type=Path)
  args = parser.parse_args()
  print(json.dumps(prepare(args.iso_source, args.base_payload, args.output), indent=2))


if __name__ == '__main__':
  main()
