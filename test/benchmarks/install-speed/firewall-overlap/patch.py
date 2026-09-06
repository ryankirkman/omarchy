#!/usr/bin/env python3
"""Move the pinned firewall leaf to the first joined user-branch step."""
import hashlib

PIN = 'dbffaa6c65344d644627a023c28661e08382b8fa'
SOURCE_SHA256 = '6914997592990435c723688e594ed189192e961423324b505a66be0de1948128'
TARGET_SHA256 = {
  'usr/bin/omarchy-apply-system': '403b83536e69812667464bd188d0a4f90466e867f5192a30710b90d26bdd93ca',
  'usr/share/omarchy/install/config/all.sh': 'dbd47b68f8c7b9ef5a6a74919cc022789244a21b5e4da460249d688b15c751dd',
  'usr/share/omarchy/install/config/firewall.sh': 'c15b76478355ae633b990520b7f8db829e2c3ab4a850922bb2c75072a99d4fcd',
  'usr/share/omarchy/install/helpers/logging.sh': '61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27',
}

FUNCTIONS = '''# Opt-in scheduling: every target package script remains unchanged.
FIREWALL_OVERLAP_TARGET_SHA256 = __TARGET_SHA256__


def _firewall_overlap_sources(ctx: InstallContext) -> dict[str, str]:
    sources = {}
    for name, expected in FIREWALL_OVERLAP_TARGET_SHA256.items():
        path = ctx.target / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"firewall overlap requires the regular pinned file: {name}")
        if name == "usr/bin/omarchy-apply-system" and not os.access(path, os.X_OK):
            raise RuntimeError("firewall overlap requires executable system setup")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"firewall overlap target source differs: {name}")
        sources[name] = data.decode("utf-8")
    return sources


def _firewall_overlap_system_command(ctx: InstallContext, cmd: list[str]) -> list[str]:
    if ctx.defer_provisioning:
        return cmd
    sources = _firewall_overlap_sources(ctx)
    config = sources["usr/share/omarchy/install/config/all.sh"]
    source_call = 'source "$OMARCHY_INSTALL/config/all.sh"\\n'
    firewall_call = 'run_logged "$OMARCHY_INSTALL/config/firewall.sh"\\n'
    if (len(cmd) < 4 or cmd[:2] != ["bash", "-c"] or
            cmd[3] != "/usr/bin/omarchy-apply-system" or ctx.state.get("firewall_overlap_pending")):
        raise RuntimeError("firewall overlap requires the once-only localdb system wrapper")
    if cmd[2].count(source_call) != 1 or config.count(firewall_call) != 1:
        raise RuntimeError("firewall overlap requires unique pinned shell anchors")
    body = cmd[2].replace(source_call, config.replace(firewall_call, ""))
    ctx.state["firewall_overlap_pending"] = True
    # Retain the existing bash body, original $0, options and all other calls.
    return [*cmd[:2], body, *cmd[3:]]


def finalize_firewall(ctx: InstallContext) -> None:
    if ctx.defer_provisioning:
        return
    if ctx.state.get("firewall_overlap_pending") is not True:
        raise RuntimeError("firewall overlap has no pending required firewall setup")
    _firewall_overlap_sources(ctx)
    _run_target_setup_command(ctx, ["bash", "-c",
        'set -euo pipefail\\n'
        'export OMARCHY_FIRST_INSTALL=1 OMARCHY_UPGRADE=0\\n'
        'export PATH="$OMARCHY_PATH/bin:$PATH"\\n'
        'source "$OMARCHY_INSTALL/helpers/logging.sh"\\n'
        'run_logged "$OMARCHY_INSTALL/config/firewall.sh"\\n',
        "/usr/bin/omarchy-apply-system"])
    ctx.state.pop("firewall_overlap_pending")


'''.replace('__TARGET_SHA256__', repr(TARGET_SHA256))


def patch_source(source):
  if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
    raise ValueError('Firewall overlap requires the exact localdb-overlap phases source')
  text = source.decode('utf-8')
  replacements = (
    ('def run_system_finalizer(ctx: InstallContext) -> None:\n',
      FUNCTIONS + 'def run_system_finalizer(ctx: InstallContext) -> None:\n'),
    ('        _run_target_setup_command(ctx, _localdb_overlap_system_command(ctx, cmd))\n',
      '        _run_target_setup_command(ctx, _firewall_overlap_system_command(ctx, _localdb_overlap_system_command(ctx, cmd)))\n'),
    ('            ("Finalizing user", run_chroot_finalizer),\n',
      '            ("Configuring firewall", finalize_firewall),\n'
      '            ("Finalizing user", run_chroot_finalizer),\n'),
  )
  for old, new in replacements:
    if text.count(old) != 1:
      raise ValueError('Firewall overlap source anchor is absent or ambiguous')
    text = text.replace(old, new)
  return text.encode('utf-8')
