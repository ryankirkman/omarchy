#!/usr/bin/env python3
"""Move the required locate index into the existing joined user branch."""
import hashlib

SOURCE_SHA256 = '8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458'
TARGET_SHA256 = {
  'usr/bin/omarchy-apply-system': '403b83536e69812667464bd188d0a4f90466e867f5192a30710b90d26bdd93ca',
  'usr/share/omarchy/install/post-install/all.sh': 'c1b482de88d9ae94cc3c94a468c2d59b8209f7cfc59c5d711318e5c5716113ab',
  'usr/share/omarchy/install/post-install/localdb.sh': '55df186f6436d92c8bb7e0c9721b596b2a63813053782c636384db4fa00d6f57',
  'usr/share/omarchy/install/helpers/logging.sh': '61a13abcc44fd5241e9882f1bcfed833e10e0ed19ad42c34a08efe1973b70d27',
}

FUNCTIONS = '''# Opt-in scheduling only: package-owned scripts remain unchanged on disk.
LOCALDB_OVERLAP_TARGET_SHA256 = __TARGET_SHA256__


def _localdb_overlap_sources(ctx: InstallContext) -> dict[str, str]:
    sources = {}
    for name, expected in LOCALDB_OVERLAP_TARGET_SHA256.items():
        path = ctx.target / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"localdb overlap requires the regular pinned file: {name}")
        if name == "usr/bin/omarchy-apply-system" and not os.access(path, os.X_OK):
            raise RuntimeError("localdb overlap requires executable system setup")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"localdb overlap target source differs: {name}")
        sources[name] = data.decode("utf-8")
    return sources


def _localdb_overlap_system_command(ctx: InstallContext, cmd: list[str]) -> list[str]:
    if ctx.defer_provisioning:
        return cmd
    sources = _localdb_overlap_sources(ctx)
    system = sources["usr/bin/omarchy-apply-system"]
    post = sources["usr/share/omarchy/install/post-install/all.sh"]
    source_call = 'source "$OMARCHY_INSTALL/post-install/all.sh"\\n'
    index_call = 'run_logged "$OMARCHY_INSTALL/post-install/localdb.sh"\\n'
    if system.count(source_call) != 1 or post.count(index_call) != 1:
        raise RuntimeError("localdb overlap requires unique pinned shell anchors")
    if cmd[0] != "/usr/bin/omarchy-apply-system" or ctx.state.get("localdb_overlap_pending"):
        raise RuntimeError("localdb overlap system setup must run exactly once")
    body = system.replace(source_call, post.replace(index_call, ""))
    ctx.state["localdb_overlap_pending"] = True
    # bash -c's next argument remains the original $0; all original options
    # follow unchanged. The body retains set -euo pipefail and every other call.
    return ["bash", "-c", body, *cmd]


def finalize_localdb(ctx: InstallContext) -> None:
    if ctx.defer_provisioning:
        return
    if ctx.state.get("localdb_overlap_pending") is not True:
        raise RuntimeError("localdb overlap has no pending required index")
    _localdb_overlap_sources(ctx)
    # Root, same private chroot and log bind as the earlier system setup.
    # The preceding user command has already retired its log bind.
    _run_target_setup_command(ctx, ["bash", "-c",
        'set -euo pipefail\\n'
        'source "$OMARCHY_INSTALL/helpers/logging.sh"\\n'
        'run_logged "$OMARCHY_INSTALL/post-install/localdb.sh"\\n',
        "/usr/bin/omarchy-apply-system"])
    ctx.state.pop("localdb_overlap_pending")


'''.replace('__TARGET_SHA256__', repr(TARGET_SHA256))


def patch_source(source):
  if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
    raise ValueError('Localdb overlap requires the exact direct-restore phases source')
  text = source.decode('utf-8')
  replacements = (
    ('def run_system_finalizer(ctx: InstallContext) -> None:\n',
      FUNCTIONS + 'def run_system_finalizer(ctx: InstallContext) -> None:\n'),
    ('        _run_target_setup_command(ctx, cmd)\n',
      '        _run_target_setup_command(ctx, _localdb_overlap_system_command(ctx, cmd))\n'),
    ('            ("Configuring DNS resolver", configure_dns_resolver),\n',
      '            ("Configuring DNS resolver", configure_dns_resolver),\n'
      '            ("Indexing installed files", finalize_localdb),\n'),
  )
  for old, new in replacements:
    if text.count(old) != 1:
      raise ValueError('Localdb overlap source anchor is absent or ambiguous')
    text = text.replace(old, new)
  return text.encode('utf-8')
