#!/usr/bin/env python3
"""Exercise the exact scheduling wrapper with real updatedb/plocate in /tmp."""

import __future__
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace


PHASES_SHA256 = "6914997592990435c723688e594ed189192e961423324b505a66be0de1948128"
COMMAND_TIMEOUT = 30


def digest(path):
  return hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv, *, env=None):
  child = subprocess.Popen(list(map(str, argv)), env=env, text=True,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
  try:
    stdout, stderr = child.communicate(timeout=COMMAND_TIMEOUT)
  except subprocess.TimeoutExpired:
    os.killpg(child.pid, signal.SIGKILL)
    child.communicate()
    raise RuntimeError(f"Command exceeded {COMMAND_TIMEOUT}s: {argv[0]}")
  return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)


def required_engine(path, expected, options):
  if not path.is_file() or not os.access(path, os.X_OK):
    raise RuntimeError(f"Required executable is unavailable: {path}")
  actual = digest(path)
  if expected and actual != expected:
    raise RuntimeError(f"Executable SHA256 differs: {path}")
  result = command([path, "--help"])
  if result.returncode:
    raise RuntimeError(f"Cannot run {path} --help: {result.stderr.strip()}")
  help_text = result.stdout + result.stderr
  absent = [option for option in options if option not in help_text]
  if absent:
    raise RuntimeError(f"{path} lacks required options: {', '.join(absent)}")
  return {"path": str(path), "sha256": actual, "help": help_text}


def load_helpers(path):
  if digest(path) != PHASES_SHA256:
    raise RuntimeError("Guest contract requires the exact patched localdb phases")
  tree = ast.parse(path.read_bytes())
  constants = next(node for node in tree.body if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "LOCALDB_OVERLAP_TARGET_SHA256" for target in node.targets))
  expected = ast.literal_eval(constants.value)
  selected = {"_localdb_overlap_sources", "_localdb_overlap_system_command", "finalize_localdb", "run_system_finalizer"}
  nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
  if {node.name for node in nodes} != selected:
    raise RuntimeError("Patched scheduling entrypoints are absent")
  namespace = {"hashlib": hashlib, "os": os, "LOCALDB_OVERLAP_TARGET_SHA256": expected,
    "TARGET_DEFERRED_BOOT_HOOKS": (),
    "_mask_mkinitcpio_pacman_hooks": lambda *args: None,
    "_unmask_mkinitcpio_pacman_hooks": lambda *args: None}
  exec(compile(ast.Module(body=nodes, type_ignores=[]), "actual-localdb-guest-helpers", "exec",
    flags=__future__.annotations.compiler_flag), namespace)
  return namespace, expected


def exercise(args, *, fail_output):
  namespace, expected = load_helpers(args.phases)
  with tempfile.TemporaryDirectory(prefix="omarchy-localdb-guest-", dir="/tmp") as temporary:
    box = Path(temporary)
    target = box / "target"
    for name, sha256 in expected.items():
      source = args.runtime_source / name
      if not source.is_file() or source.is_symlink() or digest(source) != sha256:
        raise RuntimeError(f"Official runtime fixture differs: {name}")
      destination = target / name
      destination.parent.mkdir(parents=True, exist_ok=True)
      destination.write_bytes(source.read_bytes())
      destination.chmod(0o755 if name == "usr/bin/omarchy-apply-system" else 0o644)
    before = {name: (digest(target / name), (target / name).stat().st_mode) for name in expected}
    runtime = target / "usr/share/omarchy"
    install, binary = runtime / "install", runtime / "bin"
    binary.mkdir()
    trace = box / "trace"
    trace.write_text("")
    scripts = {
      "config/all.sh": '''printf 'config\\n' >>"$BOX/trace"
printf '%s\\n' "$0" "$OMARCHY_INSTALL_USER" "$OMARCHY_FIRST_INSTALL" >"$BOX/argv-environment"
''',
      "login/all.sh": 'printf "login\\n" >>"$BOX/trace"\n',
      "post-install/pacman.sh": 'printf "pacman\\n" >>"$BOX/trace"\n',
      "post-install/udev.sh": 'printf "udev\\n" >>"$BOX/trace"\n',
    }
    for name, body in scripts.items():
      path = install / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(body)
    stubs = {
      "getent": '[[ $1 == passwd && $2 == test-user ]] || exit 1\nprintf "test-user:x:1000:1000::/home/test-user:/bin/bash\\n"\n',
      "omarchy-apply-hardware": 'printf "hardware %s\\n" "$*" >>"$BOX/trace"\n',
      "updatedb": '''printf 'updatedb\\n' >>"$BOX/trace"
exec "$ACTUAL_UPDATEDB" --database-root "$INDEX_TREE" --output "$INDEX_DB" \\
  --require-visibility 0 --prunefs '' --prunepaths '' --prunenames '' --prune-bind-mounts 0
''',
    }
    for name, body in stubs.items():
      path = binary / name
      path.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
      path.chmod(0o755)
    index_tree = box / "index-tree"
    index_tree.mkdir()
    database = box / ("absent-parent/index.db" if fail_output else "index.db")
    env = dict(os.environ, BOX=str(box), OMARCHY_PATH=str(runtime), OMARCHY_INSTALL=str(install),
      OMARCHY_INSTALL_LOG_FILE=str(box / "install.log"), OMARCHY_LOG_TO_STDOUT="1",
      ACTUAL_UPDATEDB=str(args.updatedb), INDEX_TREE=str(index_tree), INDEX_DB=str(database),
      PATH=str(binary) + ":/usr/bin:/bin")
    records = []

    def execute(context, argv):
      result = command(argv, env=env)
      records.append(result)
      result.check_returncode()

    namespace["_run_target_setup_command"] = execute
    context = SimpleNamespace(target=target, username="test-user", defer_provisioning=False, state={})
    namespace["run_system_finalizer"](context)
    expected_trace = ["config", "hardware --install-user test-user", "login", "pacman", "udev"]
    if trace.read_text().splitlines() != expected_trace or database.exists():
      raise RuntimeError("Early setup ran locate indexing or changed setup ordering")
    if (box / "argv-environment").read_text().splitlines() != ["/usr/bin/omarchy-apply-system", "test-user", "1"]:
      raise RuntimeError("Original command identity or options changed")

    # This file does not exist during early system setup. The late, real index
    # must include it; stubbing updatedb cannot satisfy the plocate query.
    user_file = index_tree / "home/test-user/new-user-fixture.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("Created after early system setup.\n")
    with trace.open("a") as output:
      output.write("created-user-file\n")
    try:
      namespace["finalize_localdb"](context)
    except subprocess.CalledProcessError:
      if not fail_output:
        raise RuntimeError("Real updatedb failed: " + records[-1].stdout + records[-1].stderr)
    else:
      if fail_output:
        raise RuntimeError("Real updatedb unexpectedly accepted an absent output directory")
    if trace.read_text().splitlines() != expected_trace + ["created-user-file", "updatedb"]:
      raise RuntimeError("Late index did not run exactly once after user fixture creation")
    late = records[-1]
    localdb_path = str(install / "post-install/localdb.sh")
    if "Starting: " + localdb_path not in late.stdout:
      raise RuntimeError("Original run_logged start record is absent")
    if fail_output:
      if late.returncode == 0 or "Failed: " + localdb_path + f" (exit code: {late.returncode})" not in late.stdout:
        raise RuntimeError("Real updatedb failure did not propagate through the original logger")
      if context.state.get("localdb_overlap_pending") is not True:
        raise RuntimeError("Failed required index was incorrectly marked complete")
      query = None
    else:
      if "Completed: " + localdb_path not in late.stdout or "localdb_overlap_pending" in context.state:
        raise RuntimeError("Late index did not complete through the original logger")
      query = command([args.plocate, "--database", database, "--basename", "--", user_file.name])
      if query.returncode != 0 or query.stdout.splitlines() != [str(user_file)]:
        raise RuntimeError("Real plocate failed to find the newly created user file: " + query.stdout + query.stderr)
      try:
        namespace["finalize_localdb"](context)
      except RuntimeError as error:
        if "no pending" not in str(error):
          raise
      else:
        raise RuntimeError("A duplicate late index was accepted")
    after = {name: (digest(target / name), (target / name).stat().st_mode) for name in expected}
    if after != before:
      raise RuntimeError("Package-owned runtime fixture files were modified")
    return {"case": "real-updatedb-error" if fail_output else "real-index-and-query", "passed": True,
      "index_invocations": trace.read_text().splitlines().count("updatedb"),
      "trace": trace.read_text().splitlines(), "late_exit_status": late.returncode,
      "late_stdout": late.stdout, "late_stderr": late.stderr,
      "query_exit_status": query.returncode if query else None,
      "query_found_new_user_file": bool(query), "runtime_files_unchanged": True,
      "index_scope": "Temporary fixture tree only; no installed root or system database",
      "temporary_directory_removed_on_return": True}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--phases", required=True, type=Path)
  parser.add_argument("--runtime-source", required=True, type=Path)
  parser.add_argument("--updatedb", type=Path, default=Path("/usr/bin/updatedb"))
  parser.add_argument("--plocate", type=Path, default=Path("/usr/bin/plocate"))
  parser.add_argument("--expected-updatedb-sha256")
  parser.add_argument("--expected-plocate-sha256")
  args = parser.parse_args()
  args.phases, args.runtime_source = args.phases.resolve(), args.runtime_source.resolve()
  args.updatedb, args.plocate = args.updatedb.resolve(), args.plocate.resolve()
  try:
    if os.geteuid() != 0:
      raise RuntimeError("The unchanged apply-system root check requires root")
    engines = {
      "updatedb": required_engine(args.updatedb, args.expected_updatedb_sha256,
        ("--database-root", "--output", "--require-visibility", "--prunefs", "--prunepaths", "--prunenames", "--prune-bind-mounts")),
      "plocate": required_engine(args.plocate, args.expected_plocate_sha256, ("--database", "--basename")),
    }
    cases = [exercise(args, fail_output=False), exercise(args, fail_output=True)]
  except Exception as error:
    print(json.dumps({"status": "blocked", "error_type": type(error).__name__, "error": str(error)}, indent=2))
    return 1
  print(json.dumps({"status": "passed", "phases_sha256": digest(args.phases), "engines": engines,
    "cases": cases, "system_database_changed": False, "target_disk_used": False}, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
