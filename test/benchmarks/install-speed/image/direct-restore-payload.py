#!/usr/bin/env python3
"""Stage direct restore after ordinary image activation without another media ISO."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess

from direct_restore import patch_source as patch_direct_restore
from root_image_mounts import patch_source as patch_mounts

PIN = 'dbffaa6c65344d644627a023c28661e08382b8fa'
UPSTREAM_SHA256 = '4088b7e930d2da7729f69c4506483d8e9c661a0488de913255c868f1154de977'
PREPARED_SHA256 = '8c802ec9ad8b94478ad16d4ca434fa6197741b4d1b3195b0a78d0c876b8682bf'
DIRECT_SHA256 = '8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458'
SOURCE_PATH = 'configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py'
PAYLOAD_PATH = Path('usr/local/lib/omarchy-benchmark/direct-restore')
HERE = Path(__file__).resolve().parent


def digest(data):
  return hashlib.sha256(data).hexdigest()


def file_inventory(directory):
  entries = []
  for path in sorted(directory.rglob('*')):
    name = path.relative_to(directory).as_posix()
    if path.is_symlink() or not (path.is_file() or path.is_dir()) or '\n' in name or '\r' in name:
      raise ValueError('Payload requires regular files/directories with unambiguous names')
    if path.is_file():
      entries.append({'path': name, 'sha256': digest(path.read_bytes()),
        'mode': oct(stat.S_IMODE(path.stat().st_mode)), 'bytes': path.stat().st_size})
  return entries


def prepare(checkout, base_payload, output):
  manifest_path = output.with_name(output.name + '.manifest.json')
  if output.exists() or manifest_path.exists():
    raise ValueError('Direct restore payload requires fresh output paths')
  base_manifest_path = base_payload.with_name(base_payload.name + '.manifest.json')
  base_manifest = json.loads(base_manifest_path.read_text())
  base_files = {entry['path']: entry for entry in file_inventory(base_payload)}
  if base_manifest.get('upstream_commit') != PIN or base_manifest.get('variant') != 'image-no-package-prefetch-fast-reboot':
    raise ValueError('Direct restore requires the prepared pinned fast-reboot payload')
  for entry in base_manifest['files']:
    if entry['path'] not in base_files or base_files[entry['path']]['sha256'] != entry['sha256']:
      raise ValueError('Fast-reboot payload differs from its recorded provenance')
  if (base_payload / PAYLOAD_PATH).exists():
    raise ValueError('Base payload already contains direct restore staging')
  original = subprocess.check_output(['git', '-C', str(checkout), 'show', f'{PIN}:{SOURCE_PATH}'])
  if digest(original) != UPSTREAM_SHA256:
    raise ValueError('Pinned upstream phases source differs')
  prepared = patch_mounts(original)
  if digest(prepared) != PREPARED_SHA256:
    raise ValueError('Ordinary image-mount correction differs from the live overlay contract')
  direct = patch_direct_restore(prepared)
  if digest(direct) != DIRECT_SHA256:
    raise ValueError('Direct restore patch differs from the expected source')
  shutil.copytree(base_payload, output)
  staged = output / PAYLOAD_PATH
  staged.mkdir(parents=True)
  sources = {
    'phases_impl.py': (direct, 0o644),
    'fast-reboot-preflight.sh': ((HERE.parent / 'fast-reboot/candidate-preflight.sh').read_bytes(), 0o755),
    'LICENSE': (subprocess.check_output(['git', '-C', str(checkout), 'show', f'{PIN}:LICENSE']), 0o644),
  }
  for name, (data, mode) in sources.items():
    path = staged / name
    path.write_bytes(data)
    path.chmod(mode)
  checksum = staged / 'payload.sha256'
  checksum.write_text(''.join(f'{digest(data)}  {name}\n' for name, (data, _) in sorted(sources.items())))
  checksum.chmod(0o644)
  manifest = {
    'schema_version': 1, 'variant': 'image-no-package-prefetch-fast-reboot-early-verify-direct-restore',
    'base_variant': 'image-no-package-prefetch-fast-reboot-early-verify',
    'upstream_commit': PIN, 'upstream_source_path': SOURCE_PATH,
    'upstream_source_sha256': UPSTREAM_SHA256, 'ordinary_phases_sha256': PREPARED_SHA256,
    'direct_phases_sha256': DIRECT_SHA256, 'target_cache': 'none',
    'base_payload_manifest_sha256': digest(base_manifest_path.read_bytes()),
    'preflight_sha256': digest((HERE / 'direct-restore-preflight.sh').read_bytes()),
    'supplemental_image_changed': False,
    'activation_order': ['ordinary image overlay', 'guarded fast reboot', 'verify ordinary phases', 'install verified direct phases'],
    'changes': ['Add only -t none to the existing qemu-img convert command after the image-mount correction'],
    'files': file_inventory(output),
  }
  manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
  return manifest


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--iso-source', type=Path, required=True)
  parser.add_argument('--base-payload', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  print(json.dumps(prepare(args.iso_source, args.base_payload, args.output), indent=2))


if __name__ == '__main__':
  main()
