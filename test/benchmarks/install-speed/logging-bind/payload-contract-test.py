#!/usr/bin/env python3
"""Check pinned logging payload composition and activation without mounts or VMs."""

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  sys.modules[name] = module
  spec.loader.exec_module(module)
  return module


payload = load("logging_bind_payload", HERE / "prepare-payload.py")


class PayloadContract(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    mounts = load("logging_payload_mounts", HERE.parent / "image/root_image_mounts.py")
    direct = load("logging_payload_direct", HERE.parent / "image/direct_restore.py")
    localdb = load("logging_payload_localdb", HERE.parent / "localdb-overlap/patch.py")
    animation = load("logging_payload_animation", HERE.parent / "animation-overlap/dashboard_patch.py")
    original = subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show",
      f"{payload.PIN}:configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py"])
    # Construct exact source bytes from checked-in patch producers and the ISO
    # pin, without depending on a prior machine's cached payload or media image.
    cls.localdb = localdb.patch_source(direct.patch_source(mounts.patch_source(original)))
    cls.dashboard = animation.patch_source(subprocess.check_output(["git", "-C", str(ISO_SOURCE), "show",
      f"{payload.PIN}:{animation.SOURCE_PATH}"]))
    cls.phase_sources = {payload.BASE_VARIANT: cls.localdb}
    cls.preflights = {payload.BASE_VARIANT: HERE.parent / "animation-overlap/preflight.sh"}
    firewall_variant = payload.BASE_VARIANT + "-firewall"
    if firewall_variant in payload.BASES:
      firewall = load("logging_payload_firewall", HERE.parent / "firewall-overlap/patch.py")
      cls.phase_sources[firewall_variant] = firewall.patch_source(cls.localdb)
      cls.preflights[firewall_variant] = HERE.parent / "firewall-overlap/preflight.sh"

  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory(prefix="omarchy-logging-payload-", dir="/tmp")
    self.addCleanup(self.temporary.cleanup)
    self.work = Path(self.temporary.name)
    self.base = self.work / "base"
    self.base_manifest_path = self.work / "base.manifest.json"
    self.output = self.work / "output"
    self.make_base(payload.BASE_VARIANT)

  def make_base(self, variant):
    if self.base.exists():
      shutil.rmtree(self.base)
    self.base.mkdir(mode=0o750)
    sources = {
      payload.BASES[variant]["phases_path"]: (self.phase_sources[variant], 0o644),
      str(payload.ANIMATION_PATH): (self.dashboard, 0o755),
      "independent/file with spaces": (b"inherited data\n", 0o640),
      "independent/executable": (b"#!/bin/bash\nexit 0\n", 0o751),
    }
    for name, (data, mode) in sources.items():
      path = self.base / name
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_bytes(data)
      path.chmod(mode)
    (self.base / "independent").chmod(0o750)
    self.base_preflight = self.preflights[variant]
    self.before = payload.inventory(self.base)
    self.manifest = {
      "schema_version": 1, "upstream_commit": payload.PIN, "variant": variant,
      "component": "foreground-animation-overlap" if variant == payload.BASE_VARIANT else "firewall-overlap",
      "dashboard_sha256": payload.DASHBOARD_SHA256,
      "firewall_phases_sha256": payload.digest(self.phase_sources[variant]),
      "preflight_sha256": payload.digest(self.base_preflight.read_bytes()), "files": self.before,
    }
    self.save_manifest()

  def save_manifest(self):
    self.base_manifest_path.write_text(json.dumps(self.manifest, indent=2) + "\n")

  def prepare(self):
    return payload.prepare(ISO_SOURCE, self.base, self.base_preflight, self.output)

  def reject(self, message=None):
    with self.assertRaisesRegex(ValueError, message or ".+"):
      self.prepare()
    self.assertFalse(self.output.exists())
    self.assertFalse(self.output.with_suffix(".manifest.json").exists())

  def test_exact_variants_preserve_every_inherited_file_and_mode(self):
    self.assertEqual(set(self.phase_sources), set(payload.BASES))
    for variant in payload.BASES:
      with self.subTest(variant=variant):
        self.make_base(variant)
        self.output = self.work / ("output-" + variant)
        manifest = self.prepare()
        staged = self.output / payload.PAYLOAD_PATH
        self.assertEqual(payload.inventory(self.base), self.before)
        after = {row["path"]: row for row in payload.inventory(self.output)}
        self.assertEqual([after[row["path"]] for row in self.before], self.before)
        self.assertEqual(manifest["files"], list(after.values()))
        self.assertEqual(manifest["variant"], payload.BASES[variant]["variant"])
        self.assertEqual(manifest["logging_scope"], "serial-system-finalizer-only")
        self.assertEqual(json.loads((staged / "activation.json").read_text())["logging_scope"], manifest["logging_scope"])
        self.assertEqual(manifest["source_phases_sha256"], payload.patch.SOURCE_SHA256S[variant])
        self.assertEqual(manifest["phases_sha256"], payload.digest((staged / "phases_impl.py").read_bytes()))
        self.assertEqual((staged / "phases_impl.py").read_bytes(), payload.patch.patch_source(self.phase_sources[variant]))
        self.assertEqual((staged / "logging.sh").read_bytes(), (payload.REPO / "install/helpers/logging.sh").read_bytes())
        self.assertEqual((staged / "guard.py").read_bytes(), (HERE / "guard.py").read_bytes())
        self.assertEqual((staged / "base-preflight.sh").read_bytes(), self.base_preflight.read_bytes())
        self.assertEqual(manifest["base_payload_manifest_sha256"], payload.digest(self.base_manifest_path.read_bytes()))
        self.assertFalse(manifest["target_package_files_changed"])
        self.assertFalse(manifest["supplemental_image_changed"])
        for name, mode in payload.directory_modes(self.base).items():
          self.assertEqual(payload.directory_modes(self.output)[name], mode)
        for name, mode in payload.STAGED_MODES.items():
          self.assertEqual(stat.S_IMODE((staged / name).stat().st_mode), mode)
        result = subprocess.run(["sha256sum", "--check", "--strict", "payload.sha256"],
          cwd=staged, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ValueError, "fresh output"):
          self.prepare()

  def test_changed_inherited_inventory_and_exact_source_pins_fail(self):
    for change in ("bytes", "mode", "unlisted", "phase-reinventoried", "dashboard-reinventoried"):
      with self.subTest(change=change):
        self.make_base(payload.BASE_VARIANT)
        if change == "bytes":
          (self.base / "independent/file with spaces").write_text("changed\n")
        elif change == "mode":
          (self.base / "independent/executable").chmod(0o755)
        elif change == "unlisted":
          (self.base / "unlisted").write_text("extra\n")
        else:
          name = payload.BASES[payload.BASE_VARIANT]["phases_path"] if change.startswith("phase") else payload.ANIMATION_PATH
          source = self.base / name
          source.write_bytes(source.read_bytes() + b"\n")
          self.manifest["files"] = payload.inventory(self.base)
          self.save_manifest()
        self.reject()

  def test_unapproved_variant_pin_or_preflight_fails_before_staging(self):
    for field, value in (("variant", payload.BASE_VARIANT + "-unexpected"),
        ("upstream_commit", "0" * 40), ("preflight_sha256", "0" * 64),
        ("component", "unexpected"), ("schema_version", True), ("dashboard_sha256", "0" * 64)):
      with self.subTest(field=field):
        self.make_base(payload.BASE_VARIANT)
        self.manifest[field] = value
        self.save_manifest()
        self.reject()
    self.make_base(payload.BASE_VARIANT)
    self.base_preflight = self.work / "changed-preflight.sh"
    self.base_preflight.write_bytes(self.preflights[payload.BASE_VARIANT].read_bytes() + b"\n")
    self.manifest["preflight_sha256"] = payload.digest(self.base_preflight.read_bytes())
    self.save_manifest()
    self.reject("Base preflight")

  def test_symlinks_and_output_inside_base_are_rejected(self):
    (self.base / "inherited-symlink").symlink_to(self.base / "independent/executable")
    self.reject("regular files")
    (self.base / "inherited-symlink").unlink()
    original_preflight = self.base_preflight
    self.base_preflight = self.work / "preflight-link"
    self.base_preflight.symlink_to(original_preflight)
    self.reject("symlink")
    self.base_preflight = original_preflight
    self.output.symlink_to(self.work / "absent-target")
    self.reject("symlink")
    self.output.unlink()
    self.output = self.base / "nested-output"
    self.reject("outside the base")
    self.output = self.work / "output"
    real_manifest = self.work / "real-manifest.json"
    self.base_manifest_path.rename(real_manifest)
    self.base_manifest_path.symlink_to(real_manifest)
    self.reject("symlink")

  def test_changed_repository_logger_is_rejected(self):
    fake_repo = self.work / "fake-repo"
    logger = fake_repo / "install/helpers/logging.sh"
    logger.parent.mkdir(parents=True)
    logger.write_bytes((payload.REPO / "install/helpers/logging.sh").read_bytes() + b"\n")
    with patch.object(payload, "REPO", fake_repo):
      self.reject("Optimized logger")

  def phase_namespace(self, variant, guard_path=None):
    # Execute the actual generated helper and setup function, retaining the
    # original setup finally block without importing unrelated installer phases.
    source = payload.patch.patch_source(self.phase_sources[variant]).decode()
    if guard_path is not None:
      self.assertEqual(source.count(repr(payload.patch.GUARD_PATH)), 1)
      source = source.replace(repr(payload.patch.GUARD_PATH), repr(str(guard_path)))
    parsed = ast.parse(source)
    selected = [node for node in parsed.body if
      isinstance(node, ast.FunctionDef) and node.name in
        ("_run_logging_bind_setup", "_run_target_setup_command") or
      isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
        target.id == "_LOGGING_BIND_GUARD" for target in node.targets)]
    self.assertEqual(len(selected), 3)
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
      names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    namespace = {"Path": Path, "hashlib": hashlib, "subprocess": subprocess, "shutil": shutil}
    exec(compile(ast.fix_missing_locations(module), "<actual-logging-phases>", "exec"), namespace)
    return namespace

  def test_generated_phase_loads_exact_guard_once_and_preserves_argv(self):
    guard_path = self.work / "guard.py"
    guard_path.write_bytes((HERE / "guard.py").read_bytes())
    namespace = self.phase_namespace(payload.BASE_VARIANT, guard_path)
    ctx = SimpleNamespace(target=self.work / "target")
    commands = [["unshare", "literal;$value", "argument with spaces"], ["second"]]
    module = SimpleNamespace(run=Mock(return_value=0))
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
    with patch.object(importlib.util, "spec_from_file_location", return_value=spec) as locate, \
        patch.object(importlib.util, "module_from_spec", return_value=module) as create:
      for command in commands:
        namespace["_run_logging_bind_setup"](ctx, command)
      locate.assert_called_once_with("omarchy_logging_bind_guard", guard_path)
      create.assert_called_once_with(spec)
      spec.loader.exec_module.assert_called_once_with(module)
    self.assertEqual(module.run.call_args_list, [unittest.mock.call(ctx.target, command) for command in commands])
    self.assertIs(namespace["_LOGGING_BIND_GUARD"], module)

  def test_generated_phase_rejects_missing_changed_and_symlink_guard_before_loading(self):
    guard_path = self.work / "guard.py"
    for kind in ("missing", "changed", "symlink"):
      with self.subTest(kind=kind):
        if guard_path.exists() or guard_path.is_symlink():
          guard_path.unlink()
        if kind == "changed":
          guard_path.write_bytes((HERE / "guard.py").read_bytes() + b"\n")
        elif kind == "symlink":
          guard_path.symlink_to(HERE / "guard.py")
        namespace = self.phase_namespace(payload.BASE_VARIANT, guard_path)
        with patch.object(importlib.util, "spec_from_file_location") as locate:
          with self.assertRaisesRegex(RuntimeError, "differs from the prepared source"):
            namespace["_run_logging_bind_setup"](SimpleNamespace(target=self.work), ["unused"])
          locate.assert_not_called()
        self.assertIsNone(namespace["_LOGGING_BIND_GUARD"])

  def test_actual_generated_setup_finally_runs_for_success_child_failure_and_guard_refusal(self):
    for variant in payload.BASES:
      for status in (0, 37, -15, "refused"):
        with self.subTest(variant=variant, status=status):
          namespace = self.phase_namespace(variant)
          target = self.work / (variant + str(status))
          target.mkdir()
          live_log = target / "live.log"
          live_log.write_text("original unified log\n")
          ctx = SimpleNamespace(target=target, log_path=live_log,
            username="bench", full_name="Benchmark User", email="bench@example.test")
          events = []
          def launch(command, **kwargs):
            events.append(command[0])
            self.assertIn(command[0], ("mount", "umount"))
            return SimpleNamespace(returncode=0)
          def run_guard(actual_target, command):
            self.assertEqual(actual_target, target)
            self.assertEqual(command[:7], ["unshare", "--mount", "--propagation", "private", "--", "arch-chroot", str(target)])
            self.assertEqual(command[-3:], ["bash", "-c", "literal;$value"])
            self.assertIn("OMARCHY_START_TIME=inherited", command)
            events.append("guard")
            live_log.write_text("log retained even on failure\n")
            if status == "refused":
              raise RuntimeError("guard refusal")
            return status
          namespace.update({
            "_LOGGING_BIND_GUARD": SimpleNamespace(run=run_guard),
            "_prepare_target_setup": lambda ctx: None,
            "_ensure_finalizer_log_started": lambda ctx: ("inherited", "123"),
            "_read_omarchy_mirror": lambda: "stable", "_iso_ref": lambda: payload.PIN,
            "_omarchy_runtime_package": lambda: "omarchy", "_omarchy_settings_package": lambda: "settings",
            "_omarchy_nvim_package": lambda: "nvim", "_install_debug_enabled": lambda: False,
            "_private_arch_chroot_command": lambda ctx, user=None:
              ["unshare", "--mount", "--propagation", "private", "--", "arch-chroot", str(ctx.target)],
          })
          with patch.object(subprocess, "run", side_effect=launch):
            if status == "refused":
              with self.assertRaisesRegex(RuntimeError, "guard refusal"):
                namespace["_run_target_setup_command"](ctx, ["bash", "-c", "literal;$value"], logging_bind=True)
            elif status:
              with self.assertRaises(subprocess.CalledProcessError) as failure:
                namespace["_run_target_setup_command"](ctx, ["bash", "-c", "literal;$value"], logging_bind=True)
              self.assertEqual(failure.exception.returncode, status)
              self.assertEqual(failure.exception.cmd[-3:], ["bash", "-c", "literal;$value"])
            else:
              namespace["_run_target_setup_command"](ctx, ["bash", "-c", "literal;$value"], logging_bind=True)
          self.assertEqual(events, ["mount", "guard", "umount"])
          target_log = target / "var/log/omarchy-install.log"
          self.assertEqual(target_log.read_bytes(), live_log.read_bytes())
          self.assertEqual(stat.S_IMODE(target_log.stat().st_mode), 0o644)

  def test_default_setup_keeps_original_child_execution_and_finally_cleanup(self):
    for variant in payload.BASES:
      for user in (None, "bench"):
        for status in (0, 37):
          with self.subTest(variant=variant, user=user, status=status):
            namespace = self.phase_namespace(variant)
            target = self.work / f"default-{variant}-{user}-{status}"
            target.mkdir()
            live_log = target / "live.log"
            live_log.write_text("original unified log\n")
            ctx = SimpleNamespace(target=target, log_path=live_log,
              username="bench", full_name="Benchmark User", email="bench@example.test")
            events = []
            guard = Mock(side_effect=AssertionError("default setup must not load or run the logging guard"))
            command = ["bash", "-c", "literal;$value"]
            private = ["unshare", "--mount", "--propagation", "private", "--", "arch-chroot", str(target)]
            private_command = Mock(return_value=private.copy())
            user_env = Mock(return_value=["HOME=/home/bench"])
            def launch(actual, **kwargs):
              events.append(actual[0])
              if actual[0] == "unshare":
                self.assertEqual(kwargs, {"check": True})
                self.assertEqual(actual[:len(private)], private)
                self.assertEqual(actual[-len(command):], command)
                self.assertEqual("HOME=/home/bench" in actual, user is not None)
                self.assertIn("OMARCHY_START_TIME=inherited", actual)
                live_log.write_text("original child output retained\n")
                if status:
                  raise subprocess.CalledProcessError(status, actual)
              else:
                self.assertIn(actual[0], ("mount", "umount"))
              return SimpleNamespace(returncode=0)
            namespace.update({
              "_run_logging_bind_setup": guard,
              "_prepare_target_setup": lambda ctx: None,
              "_ensure_finalizer_log_started": lambda ctx: ("inherited", "123"),
              "_read_omarchy_mirror": lambda: "stable", "_iso_ref": lambda: payload.PIN,
              "_omarchy_runtime_package": lambda: "omarchy", "_omarchy_settings_package": lambda: "settings",
              "_omarchy_nvim_package": lambda: "nvim", "_install_debug_enabled": lambda: False,
              "_private_arch_chroot_command": private_command, "_target_user_env": user_env,
            })
            with patch.object(subprocess, "run", side_effect=launch):
              options = {} if user is None else {"user": user}
              if status:
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                  namespace["_run_target_setup_command"](ctx, command, **options)
                self.assertEqual(failure.exception.returncode, status)
              else:
                namespace["_run_target_setup_command"](ctx, command, **options)
            private_command.assert_called_once_with(ctx, user=user)
            if user is None:
              user_env.assert_not_called()
            else:
              user_env.assert_called_once_with(ctx, user)
            guard.assert_not_called()
            self.assertIsNone(namespace["_LOGGING_BIND_GUARD"])
            self.assertEqual(events, ["mount", "unshare", "umount"])
            target_log = target / "var/log/omarchy-install.log"
            self.assertEqual(target_log.read_bytes(), live_log.read_bytes())
            self.assertEqual(stat.S_IMODE(target_log.stat().st_mode), 0o644)

  def test_system_finalizer_is_sole_opt_in_and_always_retires_hook_mask(self):
    for variant in payload.BASES:
      source = payload.patch.patch_source(self.phase_sources[variant])
      parsed = ast.parse(source)
      system = next(node for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_system_finalizer")
      setup_calls = [node for node in ast.walk(parsed) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "_run_target_setup_command"]
      opted_in = [node for node in setup_calls if any(keyword.arg == "logging_bind" for keyword in node.keywords)]
      self.assertEqual(len(opted_in), 1)
      self.assertIn(opted_in[0], list(ast.walk(system)))
      self.assertGreater(len(setup_calls), 1)
      for deferred in (False, True):
        for status in (0, 37, "refused"):
          with self.subTest(variant=variant, deferred=deferred, status=status):
            ctx = SimpleNamespace(target=self.work / "target", username="bench", defer_provisioning=deferred)
            hooks = ["required-hook"]
            events = []
            expected_command = ["/usr/bin/omarchy-apply-system"] + (
              ["--defer-provisioning", "--first-install"] if deferred else
              ["--install-user", "bench", "--first-install"])
            def mask(actual_ctx, target, actual_hooks):
              self.assertIs(actual_ctx, ctx)
              self.assertEqual(target, ctx.target)
              self.assertIs(actual_hooks, hooks)
              events.append("mask")
            def unmask(actual_ctx, target, actual_hooks):
              self.assertIs(actual_ctx, ctx)
              self.assertEqual(target, ctx.target)
              self.assertIs(actual_hooks, hooks)
              events.append("unmask")
            def schedule(name):
              def apply(actual_ctx, command):
                self.assertIs(actual_ctx, ctx)
                self.assertEqual(command, expected_command)
                events.append(name)
                return command
              return apply
            def setup(actual_ctx, command, **kwargs):
              self.assertIs(actual_ctx, ctx)
              self.assertEqual(command, expected_command)
              self.assertEqual(kwargs, {"logging_bind": True})
              events.append("setup")
              if status == "refused":
                raise RuntimeError("guard refusal")
              if status:
                raise subprocess.CalledProcessError(status, command)
            namespace = {
              "TARGET_DEFERRED_BOOT_HOOKS": hooks,
              "_mask_mkinitcpio_pacman_hooks": mask, "_unmask_mkinitcpio_pacman_hooks": unmask,
              "_localdb_overlap_system_command": schedule("localdb"),
              "_firewall_overlap_system_command": schedule("firewall"), "_run_target_setup_command": setup,
            }
            module = ast.Module(body=[ast.ImportFrom(module="__future__",
              names=[ast.alias(name="annotations")], level=0), system], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), "<actual-system-finalizer>", "exec"), namespace)
            if status == "refused":
              with self.assertRaisesRegex(RuntimeError, "guard refusal"):
                namespace["run_system_finalizer"](ctx)
            elif status:
              with self.assertRaises(subprocess.CalledProcessError) as failure:
                namespace["run_system_finalizer"](ctx)
              self.assertEqual(failure.exception.returncode, status)
            else:
              namespace["run_system_finalizer"](ctx)
            schedule_events = ["localdb", "firewall"] if variant.endswith("-firewall") else ["localdb"]
            self.assertEqual(events, ["mask", *schedule_events, "setup", "unmask"])

  def exercise(self, name, *, mutate=None, **settings):
    box = self.work / name
    box.mkdir()
    staged = box / "payload"
    shutil.copytree(self.output / payload.PAYLOAD_PATH, staged)
    variant = json.loads((staged / "activation.json").read_text())["base_variant"]
    (box / "original-phases").write_bytes(self.phase_sources[variant])
    (box / "live-phases").write_bytes(b"untouched before inherited activation\n")
    if mutate:
      mutate(staged)
    shims = box / "shims"
    shims.mkdir()
    # Only the inherited activation boundary is simulated. The actual logging
    # preflight validates the real staged bytes, installs and compares files.
    shim = shims / "bash"
    shim.write_text('''#!/bin/bash
set -euo pipefail
[[ $# == 1 && $1 == "$BOX/payload/base-preflight.sh" ]] || exit 91
printf 'base\\n' >>"$BOX/calls"
[[ ${BASE_FAIL:-0} == 0 ]] || exit 19
cp "$BOX/original-phases" "$BOX/live-phases"
if [[ ${WRONG_LIVE:-0} == 1 ]]; then printf '# drift\\n' >>"$BOX/live-phases"; fi
if [[ ${LIVE_SYMLINK:-0} == 1 ]]; then
  mv "$BOX/live-phases" "$BOX/symlink-target"
  ln -s "$BOX/symlink-target" "$BOX/live-phases"
fi
''')
    shim.chmod(0o755)
    source = (HERE / "preflight.sh").read_text()
    for old, new in {
      "payload=/usr/local/lib/omarchy-benchmark/logging-bind": "payload=" + shlex.quote(str(staged)),
      "live=/usr/share/omarchy-iso/orchestrator/phases_impl.py": "live=" + shlex.quote(str(box / "live-phases")),
    }.items():
      self.assertEqual(source.count(old), 1)
      source = source.replace(old, new)
    result = subprocess.run(["/bin/bash"], input=source, text=True, capture_output=True, timeout=10,
      env={**os.environ, "BOX": str(box), "PATH": str(shims) + os.pathsep + os.environ["PATH"],
        **{key: str(value) for key, value in settings.items()}})
    calls = (box / "calls").read_text().splitlines() if (box / "calls").exists() else []
    return result, calls, box / "live-phases"

  def test_activation_order_and_fail_closed_live_boundaries(self):
    for variant in payload.BASES:
      with self.subTest(variant=variant):
        self.make_base(variant)
        self.output = self.work / ("prepared-" + variant)
        self.prepare()
        result, calls, live = self.exercise("success-" + variant)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["base"])
        self.assertEqual(live.read_bytes(), (self.output / payload.PAYLOAD_PATH / "phases_impl.py").read_bytes())
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o644)
        for setting in ("BASE_FAIL", "WRONG_LIVE", "LIVE_SYMLINK"):
          result, calls, live = self.exercise(setting + variant, **{setting: 1})
          self.assertNotEqual(result.returncode, 0)
          self.assertEqual(calls, ["base"])
          if setting == "BASE_FAIL":
            self.assertEqual(result.returncode, 19)
            self.assertEqual(live.read_bytes(), b"untouched before inherited activation\n")
          else:
            self.assertNotEqual(live.read_bytes(), (self.output / payload.PAYLOAD_PATH / "phases_impl.py").read_bytes())

  def test_corrupt_missing_extra_duplicate_symlink_and_mode_fail_before_activation(self):
    self.prepare()
    mutations = {
      "corrupt-phases": lambda staged: (staged / "phases_impl.py").write_bytes(b"corrupt\n"),
      "corrupt-logger": lambda staged: (staged / "logging.sh").write_bytes(b"corrupt\n"),
      "corrupt-guard": lambda staged: (staged / "guard.py").write_bytes(b"corrupt\n"),
      "mode": lambda staged: (staged / "logging.sh").chmod(0o755),
      "missing": lambda staged: (staged / "guard.py").unlink(),
      "extra": lambda staged: (staged / "extra").write_text("extra\n"),
      "extra-directory": lambda staged: (staged / "extra-directory").mkdir(),
      "duplicate-checksum": lambda staged: (staged / "payload.sha256").write_text(
        (staged / "payload.sha256").read_text() * 2),
      "missing-checksum": lambda staged: (staged / "payload.sha256").write_text(""),
      "symlink": lambda staged: ((staged / "logging.sh").unlink(),
        (staged / "logging.sh").symlink_to(payload.REPO / "install/helpers/logging.sh")),
    }
    for name, mutate in mutations.items():
      with self.subTest(name=name):
        result, calls, live = self.exercise(name, mutate=mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(live.read_bytes(), b"untouched before inherited activation\n")

  def test_rehashed_unapproved_activation_is_rejected(self):
    self.prepare()
    changes = [(key, "unexpected") for key in ("base_variant", "source_phases_sha256",
      "base_preflight_sha256", "original_logger_sha256", "logging_scope")]
    changes.append(("logging_scope", None))
    for key, value in changes:
      def mutate(staged):
        path = staged / "activation.json"
        activation = json.loads(path.read_text())
        if value is None:
          activation.pop(key)
        else:
          activation[key] = value
        path.write_text(json.dumps(activation) + "\n")
        checksums = staged / "payload.sha256"
        rows = checksums.read_text().splitlines()
        checksums.write_text("\n".join(payload.digest(path.read_bytes()) + "  activation.json"
          if row.endswith("  activation.json") else row for row in rows) + "\n")
      with self.subTest(key=key, value=value):
        result, calls, live = self.exercise(f"changed-{key}-{value}", mutate=mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(live.read_bytes(), b"untouched before inherited activation\n")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  args, remaining = parser.parse_known_args()
  ISO_SOURCE = args.iso_source.resolve()
  unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
