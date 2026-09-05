"""Opt-in destination direct I/O for the exact, mount-corrected PR145 restore."""

import hashlib


UPSTREAM_COMMIT = "dbffaa6c65344d644627a023c28661e08382b8fa"
UPSTREAM_SOURCE_SHA256 = "4088b7e930d2da7729f69c4506483d8e9c661a0488de913255c868f1154de977"
PREPARED_SOURCE_SHA256 = "8c802ec9ad8b94478ad16d4ca434fa6197741b4d1b3195b0a78d0c876b8682bf"
OLD_ARGUMENTS = b'["qemu-img", "convert", "-q", "-f", "qcow2", "-O", "raw", "-W", "-n",'
NEW_ARGUMENTS = b'["qemu-img", "convert", "-q", "-f", "qcow2", "-O", "raw", "-W", "-n", "-t", "none",'


def patch_source(source):
  """Change only the output cache option after the existing child-mount fix.

  Keep ordinary zero writes and QEMU's supported zero-offload fallback. The
  target is an existing device, whose previous contents are not assumed zero.
  """
  if hashlib.sha256(source).hexdigest() != PREPARED_SOURCE_SHA256:
    raise ValueError("Direct restore requires the exact mount-corrected PR145 phases_impl.py")
  if source.count(OLD_ARGUMENTS) != 1 or NEW_ARGUMENTS in source:
    raise ValueError("Unexpected pinned qemu-img restore command")
  patched = source.replace(OLD_ARGUMENTS, NEW_ARGUMENTS)
  compile(patched, "direct-restore-phases_impl.py", "exec")
  return patched
