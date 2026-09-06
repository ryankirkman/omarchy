#!/usr/bin/env python3
"""Check joined locate-index scheduling against exact ISO runtime scripts."""

import __future__
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
IMAGE = HERE.parent / "image"
sys.path.insert(0, str(IMAGE))
import direct_restore
import root_image_mounts

spec = importlib.util.spec_from_file_location("localdb_overlap_patch", HERE / "patch.py")
overlap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlap)


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def digest(data):
  return hashlib.sha256(data).hexdigest()


class LocaldbOverlapContract(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.temporary = tempfile.TemporaryDirectory(prefix="omarchy-localdb-contract-", dir="/tmp")
    cls.addClassCleanup(cls.temporary.cleanup)
    cls.work = Path(cls.temporary.name)
    cls.upstream = subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show",
      f"{direct_restore.UPSTREAM_COMMIT}:configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py"])
    cls.base = direct_restore.patch_source(root_image_mounts.patch_source(cls.upstream))
    cls.patched = overlap.patch_source(cls.base)
    cls.runtime = {name: (RUNTIME_SOURCE / name).read_bytes() for name in overlap.TARGET_SHA256}
    for name, expected in overlap.TARGET_SHA256.items():
      if digest(cls.runtime[name]) != expected:
        raise AssertionError(f"Runtime fixture differs from official ISO: {name}")
    cls.producer = load("localdb_payload", HERE / "prepare-payload.py")
    fast = load("localdb_fast_payload", HERE.parent / "fast-reboot/prepare-payload.py")
    direct = load("localdb_direct_payload", IMAGE / "direct-restore-payload.py")
    ordinary = cls.work / "ordinary-payload"
    ordinary.mkdir()
    (ordinary / "unchanged").write_text("unchanged ordinary payload\n")
    (ordinary / "unchanged").chmod(0o640)
    fast_path = cls.work / "fast-payload"
    fast.prepare(ISO_SOURCE, ordinary, fast_path)
    cls.base_payload = cls.work / "direct-payload"
    direct.prepare(ISO_SOURCE, fast_path, cls.base_payload)
    cls.output = cls.work / "localdb-payload"
    cls.manifest = cls.producer.prepare(ISO_SOURCE, cls.base_payload, cls.output)

  def namespace(self):
    names = {"_localdb_overlap_sources", "_localdb_overlap_system_command", "finalize_localdb",
      "run_system_finalizer", "_run_finalization_branch", "finalize_boot_and_user_setup"}
    nodes = [node for node in ast.parse(self.patched).body if isinstance(node, ast.FunctionDef) and node.name in names]
    self.assertEqual({node.name for node in nodes}, names)
    namespace = {"hashlib": hashlib, "os": os, "time": time, "ThreadPoolExecutor": ThreadPoolExecutor,
      "shutil": SimpleNamespace(which=lambda command: "/usr/bin/unshare"),
      "LOCALDB_OVERLAP_TARGET_SHA256": dict(overlap.TARGET_SHA256), "info": lambda message: None,
      "TARGET_DEFERRED_BOOT_HOOKS": ("test-hook",),
      "_mask_mkinitcpio_pacman_hooks": lambda *args: None,
      "_unmask_mkinitcpio_pacman_hooks": lambda *args: None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "actual-localdb-branches", "exec",
      flags=__future__.annotations.compiler_flag), namespace)
    return namespace

  def fixture(self, deferred=False):
    box = Path(tempfile.mkdtemp(prefix="case-", dir=self.work))
    target = box / "target"
    for name, data in self.runtime.items():
      path = target / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_bytes(data)
      if name == "usr/bin/omarchy-apply-system":
        path.chmod(0o755)
    (box / "trace").write_text("")
    context = SimpleNamespace(target=target, username="test-user", defer_provisioning=deferred, state={})
    return box, context, self.namespace()

  def test_exact_source_pins_and_only_expected_functions_change(self):
    self.assertEqual(digest(self.base), overlap.SOURCE_SHA256)
    before = {node.name: ast.dump(node) for node in ast.parse(self.base).body if isinstance(node, ast.FunctionDef)}
    after = {node.name: ast.dump(node) for node in ast.parse(self.patched).body if isinstance(node, ast.FunctionDef)}
    self.assertEqual(set(after) - set(before), {"_localdb_overlap_sources", "_localdb_overlap_system_command", "finalize_localdb"})
    self.assertEqual({name for name in before if before[name] != after[name]},
      {"run_system_finalizer", "finalize_boot_and_user_setup"})
    # Existing branch timing/join logic and the boot validator/snapshot remain
    # byte-for-byte the same AST; only the two call-site functions may change.
    for name in ("_run_finalization_branch", "validate_boot", "create_factory_snapshot"):
      self.assertEqual(before[name], after[name])

  def test_source_and_each_patch_anchor_fail_closed(self):
    for source in (self.base + b"\n", self.patched, self.upstream):
      with self.subTest(source=digest(source)), self.assertRaises(ValueError):
        overlap.patch_source(source)
    anchors = (b"def run_system_finalizer(ctx: InstallContext) -> None:\n",
      b"        _run_target_setup_command(ctx, cmd)\n",
      b'            ("Configuring DNS resolver", configure_dns_resolver),\n')
    for anchor in anchors:
      self.assertEqual(self.base.count(anchor), 1)
      for source in (self.base.replace(anchor, b""), self.base + anchor):
        with self.subTest(anchor=anchor), patch.object(overlap, "SOURCE_SHA256", digest(source)):
          with self.assertRaises(ValueError):
            overlap.patch_source(source)

  def test_every_target_source_drift_is_rejected_before_setup(self):
    for name in self.runtime:
      box, ctx, namespace = self.fixture()
      (ctx.target / name).write_bytes(self.runtime[name] + b"\n")
      calls = []
      namespace["_run_target_setup_command"] = lambda *args: calls.append(args)
      with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "target source differs"):
        namespace["run_system_finalizer"](ctx)
      self.assertEqual(calls, [])
      self.assertNotIn("localdb_overlap_pending", ctx.state)

  def test_missing_symlink_and_shell_anchor_drift_are_rejected(self):
    name = "usr/share/omarchy/install/post-install/all.sh"
    for mode in ("missing", "symlink", "anchor"):
      box, ctx, namespace = self.fixture()
      path = ctx.target / name
      if mode == "missing":
        path.unlink()
      elif mode == "symlink":
        source = box / "exact-all.sh"
        source.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(source)
      else:
        data = path.read_bytes().replace(b'run_logged "$OMARCHY_INSTALL/post-install/localdb.sh"\n', b"")
        path.write_bytes(data)
        namespace["LOCALDB_OVERLAP_TARGET_SHA256"][name] = digest(data)
      with self.subTest(mode=mode), self.assertRaises(RuntimeError):
        namespace["_localdb_overlap_system_command"](ctx, ["/usr/bin/omarchy-apply-system"])

  def test_nonexecutable_system_script_is_rejected(self):
    box, ctx, namespace = self.fixture()
    (ctx.target / "usr/bin/omarchy-apply-system").chmod(0o644)
    with self.assertRaisesRegex(RuntimeError, "executable"):
      namespace["_localdb_overlap_system_command"](ctx, ["/usr/bin/omarchy-apply-system"])

  def test_payload_preserves_every_base_file_and_records_full_provenance(self):
    original_manifest = self.base_payload.with_name(self.base_payload.name + ".manifest.json")
    original = json.loads(original_manifest.read_text())
    for entry in original["files"]:
      source = self.base_payload / entry["path"]
      copied = self.output / entry["path"]
      self.assertEqual(copied.read_bytes(), source.read_bytes(), entry["path"])
      self.assertEqual(copied.stat().st_mode, source.stat().st_mode, entry["path"])
      self.assertEqual(digest(source.read_bytes()), entry["sha256"])
    files = {str(path.relative_to(self.output)): path for path in self.output.rglob("*") if path.is_file()}
    self.assertEqual(len(self.manifest["files"]), len(files))
    self.assertEqual({entry["path"] for entry in self.manifest["files"]}, set(files))
    for entry in self.manifest["files"]:
      path = files[entry["path"]]
      self.assertEqual(entry["sha256"], digest(path.read_bytes()))
      self.assertEqual(entry["mode"], oct(path.stat().st_mode & 0o7777))
      self.assertEqual(entry["bytes"], path.stat().st_size)
    self.assertEqual(self.manifest["base_payload_manifest_sha256"], digest(original_manifest.read_bytes()))
    self.assertEqual(self.manifest["target_source_sha256"], overlap.TARGET_SHA256)
    self.assertEqual(self.manifest["source_phases_sha256"], digest(self.base))
    self.assertEqual(self.manifest["localdb_phases_sha256"], digest(self.patched))
    self.assertEqual(self.manifest["preflight_sha256"], digest((HERE / "preflight.sh").read_bytes()))
    self.assertFalse(self.manifest["supplemental_image_changed"])
    self.assertEqual(json.loads(self.output.with_name(self.output.name + ".manifest.json").read_text()), self.manifest)

  def test_payload_rejects_changed_base_and_output_collisions(self):
    for destination in (self.output, self.base_payload / "nested-output"):
      with self.subTest(destination=str(destination)), self.assertRaises(ValueError):
        self.producer.prepare(ISO_SOURCE, self.base_payload, destination)
    for change in ("bytes", "mode"):
      base = self.work / ("changed-base-" + change)
      shutil.copytree(self.base_payload, base)
      shutil.copyfile(self.base_payload.with_name(self.base_payload.name + ".manifest.json"),
        base.with_name(base.name + ".manifest.json"))
      if change == "bytes":
        (base / "unchanged").write_text("changed\n")
      else:
        (base / "unchanged").chmod(0o600)
      output = self.work / ("rejected-output-" + change)
      with self.subTest(change=change), self.assertRaisesRegex(ValueError, "inventory"):
        self.producer.prepare(ISO_SOURCE, base, output)
      self.assertFalse(output.exists())

  def test_actual_preflight_composes_once_and_rejects_corruption_and_drift(self):
    for case in ("success", "corrupt-stage", "drifted-base", "failed-base"):
      with self.subTest(case=case):
        box = Path(tempfile.mkdtemp(prefix="activation-", dir=self.work))
        output = box / "payload"
        shutil.copytree(self.output, output)
        staged = output / self.producer.PAYLOAD_PATH
        (box / "calls").write_text("")
        (box / "base.py").write_bytes(self.base)
        stub = '''#!/bin/bash
set -euo pipefail
printf 'direct-activation\\n' >>"$BOX/calls"
cp "$BOX/base.py" "$BOX/live.py"
if [[ $CASE == drifted-base ]]; then printf '# source drift\\n' >>"$BOX/live.py"; fi
if [[ $CASE == failed-base ]]; then exit 37; fi
'''
        (staged / "direct-restore-preflight.sh").write_text(stub)
        checksum = staged / "payload.sha256"
        names = [line.split("  ", 1)[1] for line in checksum.read_text().splitlines()]
        self.assertTrue(all(Path(name).name == name for name in names))
        checksum.write_text("".join(f"{digest((staged / name).read_bytes())}  {name}\n" for name in names))
        if case == "corrupt-stage":
          with (staged / "phases_impl.py").open("ab") as changed:
            changed.write(b"# corrupt stage\n")
        script = (HERE / "preflight.sh").read_text()
        for old, new in (
            ("payload=/usr/local/lib/omarchy-benchmark/localdb-overlap", "payload=" + shlex.quote(str(staged))),
            ("live=/usr/share/omarchy-iso/orchestrator/phases_impl.py", "live=" + shlex.quote(str(box / "live.py")))):
          self.assertEqual(script.count(old), 1)
          script = script.replace(old, new)
        result = subprocess.run(["bash"], input=script, text=True, capture_output=True, timeout=10,
          env=dict(os.environ, BOX=str(box), CASE=case, PATH="/usr/bin:/bin"))
        self.assertEqual((box / "calls").read_text().splitlines(),
          [] if case == "corrupt-stage" else ["direct-activation"])
        if case == "success":
          self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
          self.assertEqual((box / "live.py").read_bytes(), self.patched)
          self.assertEqual((box / "live.py").stat().st_mode & 0o7777, 0o644)
        else:
          self.assertNotEqual(result.returncode, 0)
          if case == "corrupt-stage":
            self.assertFalse((box / "live.py").exists())
          elif case == "failed-base":
            self.assertEqual(result.returncode, 37)
            self.assertEqual((box / "live.py").read_bytes(), self.base)
          else:
            self.assertEqual((box / "live.py").read_bytes(), self.base + b"# source drift\n")

  def shell_fixture(self, deferred=False, updatedb_status=0):
    if os.geteuid() != 0:
      self.skipTest("Unmodified apply-system enforces root; the sandbox shell checks need root")
    box, ctx, namespace = self.fixture(deferred)
    runtime = ctx.target / "usr/share/omarchy"
    install = runtime / "install"
    binary = runtime / "bin"
    binary.mkdir()
    scripts = {
      "config/all.sh": '''printf 'config\\n' >>"$BOX/trace"
printf '%s\\n' "$0" "$OMARCHY_INSTALL_USER" "$OMARCHY_FIRST_INSTALL" "$OMARCHY_UPGRADE" >"$BOX/argv-environment"
''',
      "login/all.sh": 'printf "login\\n" >>"$BOX/trace"\n',
      "post-install/pacman.sh": 'printf "pacman\\n" >>"$BOX/trace"\n',
      "post-install/udev.sh": 'printf "udev\\n" >>"$BOX/trace"\n',
    }
    for name, body in scripts.items():
      path = install / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(body)
    commands = {
      "getent": '[[ $1 == passwd && $2 == test-user ]] || exit 1\nprintf "test-user:x:1000:1000::/home/test-user:/bin/bash\\n"\n',
      "omarchy-apply-hardware": 'printf "hardware %s\\n" "$*" >>"$BOX/trace"\n',
      "updatedb": 'printf "updatedb\\n" >>"$BOX/trace"\nprintf "fixture updatedb output\\n"\nexit "$UPDATEDB_STATUS"\n',
    }
    for name, body in commands.items():
      path = binary / name
      path.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
      path.chmod(0o755)
    env = dict(os.environ, BOX=str(box), OMARCHY_PATH=str(runtime), OMARCHY_INSTALL=str(install),
      OMARCHY_INSTALL_LOG_FILE=str(box / "install.log"), OMARCHY_LOG_TO_STDOUT="1",
      UPDATEDB_STATUS=str(updatedb_status), PATH=str(binary) + ":/usr/bin:/bin")
    records = []

    def execute(context, command):
      # Execute the exact production wrapper body and $0/options, with every
      # external setup command confined to the explicit fixture stubs above.
      argv = list(command)
      if argv[0] == "/usr/bin/omarchy-apply-system":
        argv = ["bash", "-c", self.runtime["usr/bin/omarchy-apply-system"].decode(), *argv]
      result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=10)
      records.append({"command": command, "result": result})
      result.check_returncode()

    namespace["_run_target_setup_command"] = execute
    return box, ctx, namespace, records

  def test_real_shell_index_runs_exactly_once_late_with_original_logging(self):
    box, ctx, namespace, records = self.shell_fixture()
    before = {name: (ctx.target / name).read_bytes() for name in self.runtime}
    namespace["run_system_finalizer"](ctx)
    self.assertEqual((box / "trace").read_text().splitlines(),
      ["config", "hardware --install-user test-user", "login", "pacman", "udev"])
    self.assertEqual((box / "argv-environment").read_text().splitlines(),
      ["/usr/bin/omarchy-apply-system", "test-user", "1", "0"])
    with self.assertRaisesRegex(RuntimeError, "exactly once"):
      namespace["run_system_finalizer"](ctx)
    namespace["finalize_localdb"](ctx)
    self.assertEqual((box / "trace").read_text().splitlines()[-1], "updatedb")
    self.assertEqual((box / "trace").read_text().splitlines().count("updatedb"), 1)
    localdb = str(ctx.target / "usr/share/omarchy/install/post-install/localdb.sh")
    self.assertNotIn(localdb, records[0]["result"].stdout)
    late_log = records[1]["result"].stdout
    self.assertIn("Starting: " + localdb, late_log)
    self.assertIn("Completed: " + localdb, late_log)
    self.assertIn("fixture updatedb output", late_log)
    self.assertNotIn("localdb_overlap_pending", ctx.state)
    with self.assertRaisesRegex(RuntimeError, "no pending"):
      namespace["finalize_localdb"](ctx)
    self.assertEqual({name: (ctx.target / name).read_bytes() for name in self.runtime}, before)

  def test_real_updatedb_error_propagates_and_keeps_failure_log(self):
    box, ctx, namespace, records = self.shell_fixture(updatedb_status=23)
    namespace["run_system_finalizer"](ctx)
    with self.assertRaises(subprocess.CalledProcessError) as raised:
      namespace["finalize_localdb"](ctx)
    self.assertEqual(raised.exception.returncode, 23)
    self.assertIn("Failed: ", records[-1]["result"].stdout)
    self.assertIn("localdb.sh (exit code: 23)", records[-1]["result"].stdout)
    self.assertNotIn("Completed: " + str(ctx.target / "usr/share/omarchy/install/post-install/localdb.sh"),
      records[-1]["result"].stdout)
    self.assertIs(ctx.state["localdb_overlap_pending"], True)
    self.assertEqual((box / "trace").read_text().splitlines().count("updatedb"), 1)

  def test_deferred_provisioning_retains_original_serial_command(self):
    box, ctx, namespace, records = self.shell_fixture(deferred=True)
    command = ["/usr/bin/omarchy-apply-system", "--defer-provisioning", "--first-install"]
    self.assertIs(namespace["_localdb_overlap_system_command"](ctx, command), command)
    namespace["run_system_finalizer"](ctx)
    self.assertEqual(records[0]["command"], command)
    self.assertEqual((box / "trace").read_text().splitlines(),
      ["config", "hardware --defer-provisioning", "login", "pacman", "udev", "updatedb"])
    namespace["finalize_localdb"](ctx)
    self.assertEqual(len(records), 1)
    self.assertNotIn("localdb_overlap_pending", ctx.state)

  def parallel_branches(self, fail_index=False):
    box, ctx, namespace = self.fixture()
    namespace["_localdb_overlap_system_command"](ctx, ["/usr/bin/omarchy-apply-system"])
    boot_started, index_started, boot_finished, permit_finish = (threading.Event() for _ in range(4))
    calls = []
    lock = threading.Lock()

    def record(name):
      with lock:
        calls.append(name)

    def boot(context):
      record("boot-start")
      boot_started.set()
      if not index_started.wait(5):
        raise RuntimeError("Index did not start concurrently with boot finalization")
      if fail_index:
        if not permit_finish.wait(5):
          raise RuntimeError("Test never released boot finalization")
      record("boot-finish")
      boot_finished.set()

    def index(context, command):
      record("index-start")
      index_started.set()
      if not boot_started.wait(5):
        raise RuntimeError("Boot finalization did not overlap indexing")
      if fail_index:
        raise RuntimeError("updatedb failed with status 23")
      if not permit_finish.wait(5):
        raise RuntimeError("Test never released indexing")
      record("index-finish")

    namespace["finalize_limine_boot"] = boot
    namespace["_run_target_setup_command"] = index
    for function_name in ("run_chroot_finalizer", "configure_login", "configure_ssh_access",
        "configure_tailscale", "configure_dns_resolver"):
      namespace[function_name] = lambda context, name=function_name: record(name)
    with ThreadPoolExecutor(max_workers=1) as driver:
      future = driver.submit(namespace["finalize_boot_and_user_setup"], ctx)
      try:
        self.assertTrue(index_started.wait(5))
        if not fail_index:
          self.assertTrue(boot_finished.wait(5))
        self.assertFalse(future.done(), "Finalization returned before the held branch completed")
      finally:
        permit_finish.set()
      if fail_index:
        with self.assertRaisesRegex(RuntimeError, "parallel finalization failed .*Indexing installed files.*updatedb failed"):
          future.result(timeout=5)
      else:
        future.result(timeout=5)
    self.assertTrue(boot_finished.is_set())
    self.assertLess(calls.index("configure_dns_resolver"), calls.index("index-start"))
    self.assertEqual(calls.count("index-start"), 1)
    records = ctx.state["phase_substeps"]
    final = next(item for item in records if item["name"] == "Indexing installed files")
    self.assertEqual(final["branch"], "user")
    self.assertEqual(final["status"], "failed" if fail_index else "ok")
    self.assertGreaterEqual(final["elapsed"], 0)
    self.assertEqual(ctx.state.get("localdb_overlap_pending", False), fail_index)

  def test_existing_parallel_branch_overlaps_and_joins_required_index(self):
    self.parallel_branches()

  def test_index_failure_is_joined_before_parallel_phase_raises(self):
    self.parallel_branches(fail_index=True)


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  parser.add_argument("--runtime-source", type=Path, default=HERE / "fixtures/runtime")
  arguments, remaining = parser.parse_known_args()
  ISO_SOURCE = arguments.iso_source.resolve()
  RUNTIME_SOURCE = arguments.runtime_source.resolve()
  unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
