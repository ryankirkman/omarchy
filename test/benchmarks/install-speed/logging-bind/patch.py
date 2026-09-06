#!/usr/bin/env python3
"""Wrap only serial system setup in a private, read-only logger mount."""
import hashlib
from pathlib import Path

BASE_VARIANT = 'image-no-package-prefetch-fast-reboot-early-verify-direct-restore-overlap'
SOURCE_SHA256S = {
  BASE_VARIANT: '6914997592990435c723688e594ed189192e961423324b505a66be0de1948128',
  BASE_VARIANT + '-firewall': 'f5235ae1ed7e6a783978d2f51e49fc3e0d44c687f218967a599a104101e0070c',
}
SOURCE_SHA256 = SOURCE_SHA256S[BASE_VARIANT]
ORIGINAL_LOGGER_SHA256 = '61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27'
LOGGER_SHA256 = '1d8151adb150bc1dfe930b30e7039978591add500a132b9951152d7a8a23d715'
GUARD_PATH = '/usr/local/lib/omarchy-benchmark/logging-bind/guard.py'


def patch_source(source):
  source_hash = hashlib.sha256(source).hexdigest()
  if source_hash not in SOURCE_SHA256S.values():
    raise ValueError('Logging bind requires an exact supported finalization phases source')
  text = source.decode('utf-8')
  anchor = '        subprocess.run(chroot_cmd, check=True)\n'
  function = 'def _run_target_setup_command(ctx: InstallContext, cmd: list[str], *, user: str | None = None) -> None:\n'
  system_command = '_localdb_overlap_system_command(ctx, cmd)'
  if source_hash == SOURCE_SHA256S[BASE_VARIANT + '-firewall']:
    system_command = '_firewall_overlap_system_command(ctx, ' + system_command + ')'
  system_call = '        _run_target_setup_command(ctx, ' + system_command + ')\n'
  if text.count(anchor) != 1 or text.count(function) != 1 or text.count(system_call) != 1:
    raise ValueError('Logging bind requires unique exact target setup command anchors')
  guard_sha256 = hashlib.sha256(Path(__file__).with_name('guard.py').read_bytes()).hexdigest()
  injected = '''_LOGGING_BIND_GUARD = None


def _run_logging_bind_setup(ctx: InstallContext, command: list[str]) -> None:
    global _LOGGING_BIND_GUARD
    if _LOGGING_BIND_GUARD is None:
        import importlib.util
        path = Path(__GUARD_PATH__)
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != __GUARD_SHA256__:
            raise RuntimeError("Logging bind guard differs from the prepared source")
        spec = importlib.util.spec_from_file_location("omarchy_logging_bind_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOGGING_BIND_GUARD = module
    status = _LOGGING_BIND_GUARD.run(ctx.target, command)
    if status:
        raise subprocess.CalledProcessError(status, command)


'''.replace('__GUARD_PATH__', repr(GUARD_PATH)).replace('__GUARD_SHA256__', repr(guard_sha256))
  scoped_function = function.replace('None) -> None:', 'None, logging_bind: bool = False) -> None:')
  text = text.replace(function, injected + scoped_function)
  text = text.replace(system_call, system_call[:-2] + ', logging_bind=True)\n')
  result = text.replace(anchor,
    '        if logging_bind:\n'
    '            _run_logging_bind_setup(ctx, chroot_cmd)\n'
    '        else:\n'
    '            subprocess.run(chroot_cmd, check=True)\n').encode('utf-8')
  compile(result, '<logging-bind-phases>', 'exec')
  return result
