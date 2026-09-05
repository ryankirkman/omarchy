#!/usr/bin/env python3
"""Execute early activation and pinned verification gates without disks or systemd."""

import argparse
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tarfile
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
PIN = "dbffaa6c65344d644627a023c28661e08382b8fa"
SERVICE = "omarchy-benchmark-preflight.service"
VERIFY_SERVICE = "omarchy-root-image-verify.service"
spec = importlib.util.spec_from_file_location("early_verifier_overlay", HERE.parent / "boot-overlay/make-initramfs.py")
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
ISO_SOURCE = None


def executable(path, text):
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text)
  path.chmod(0o755)


def pinned_file(relative):
  return subprocess.run(
    ["git", "-C", str(ISO_SOURCE), "show", f"{PIN}:configs/airootfs/{relative}"],
    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
  ).stdout


class Sandbox:
  """Run actual shell control flow; redirect every live-root write into a tempdir."""

  def __init__(self, root, wait_helper, verify_unit):
    self.root = root
    self.live = root / "live"
    self.media = root / "media"
    self.bin = root / "bin"
    self.calls = root / "calls"
    self.boot = self.live / "run/archiso/bootmnt/arch/x86_64"
    self.preflight_state = root / "preflight-state"
    self.preflight_status = root / "preflight-status"
    for directory in (self.live, self.media, self.bin, self.boot):
      directory.mkdir(parents=True, exist_ok=True)
    self.calls.write_text("")
    self.preflight_state.write_text("inactive\n")
    self.preflight_status.write_text("0\n")
    (root / "verify-state").write_text("inactive\n")
    (self.boot / "airootfs.sfs").write_bytes(b"already-mounted-live-root\n")

    # The actual activation archive is tiny but its checksums and tar extraction
    # are real. The pinned wait helper retains all of its decision logic.
    members = {
      "usr/local/bin/omarchy-wait-root-image-verify": self.remap(wait_helper.decode()).encode(),
      "etc/systemd/system/omarchy-root-image-verify.service": verify_unit,
      "activation-proof": b"pinned-overlay-extracted\n",
    }
    archive = self.media / "installer-overlay.tar"
    with tarfile.open(archive, "w") as output:
      for name, data in members.items():
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        entry.mode = 0o755 if name.startswith("usr/local/bin/") else 0o644
        output.addfile(entry, io.BytesIO(data))
    self.checksum(archive)
    image = self.media / "arch/x86_64/omarchy-root.btrfs.qcow2"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"tiny-image-stream-for-real-sha256-verification\n")
    self.checksum(image)

    stubs = {
      "id": "[[ $* == '-u' ]] || exit 90\necho 0",
      "systemd-detect-virt": "[[ $* == '--vm' ]] || exit 90\necho qemu",
      "qemu-img": "[[ $* == '--version' ]] || exit 90\necho 'QEMU fixture'",
      "pgrep": "exit 1",
      "tty": "echo /dev/tty1",
      "findmnt": '''if [[ $* == *OPTIONS* ]]; then echo ro; else echo /dev/fixture; fi''',
      "lsblk": "echo fixture",
      "mount": '''echo "mount $*" >>"$BOX/calls"
if [[ $1 == --bind && $# == 3 && $2 == "$BOX/media/arch/x86_64" && $3 == "$BOX/live/run/archiso/bootmnt/arch/x86_64" ]]; then
  cp "$2/omarchy-root.btrfs.qcow2" "$2/omarchy-root.btrfs.qcow2.sha256" "$3/"
elif [[ $* != "-o remount,bind,ro $BOX/live/run/archiso/bootmnt/arch/x86_64" ]]; then
  exit 90
fi''',
      "journalctl": '''cat "$BOX/verify-output"''',
      "systemctl": '''echo "systemctl $*" >>"$BOX/calls"
if [[ $* == daemon-reload ]]; then exit 0; fi
if [[ $* == 'start --no-block omarchy-root-image-verify.service' ]]; then
  echo verifier-start >>"$BOX/calls"
  if (cd "$BOX/live/run/archiso/bootmnt/arch/x86_64" && /usr/bin/sha256sum --check --strict omarchy-root.btrfs.qcow2.sha256) >"$BOX/verify-output" 2>&1; then
    echo active >"$BOX/verify-state"
  else
    echo failed >"$BOX/verify-state"
  fi
  # A real --no-block start returns before the hash verdict is available.
  exit 0
fi
if [[ $1 == is-active && $* == *omarchy-benchmark-preflight.service* ]]; then
  [[ $(cat "$BOX/preflight-state") == active ]]
  exit
fi
if [[ $1 == show ]]; then
  case "$*" in
    *omarchy-benchmark-preflight.service*ExecMainStatus*) cat "$BOX/preflight-status"; exit "${EXEC_STATUS_QUERY_RC:-0}" ;;
    *omarchy-root-image-verify.service*LoadState*) echo loaded ;;
    *omarchy-root-image-verify.service*ActiveState*) cat "$BOX/verify-state" ;;
    *omarchy-root-image-verify.service*Result*) echo exit-code ;;
    *) echo "Unexpected systemctl show: $*" >&2; exit 90 ;;
  esac
  exit 0
fi
echo "Unexpected systemctl command: $*" >&2
exit 90''',
    }
    for name, body in stubs.items():
      executable(self.bin / name, "#!/bin/bash\nset -euo pipefail\n" + body + "\n")

    activation = self.remap((HERE.parent / "image/activate-installer-overlay.sh").read_text())
    # Both potential archive extraction destinations are literal root paths.
    # Keep real tar, real checksum validation, and all activation decisions.
    activation = activation.replace(" -C /\n", " -C " + shlex.quote(str(self.live)) + "\n")
    executable(self.live / "usr/local/lib/omarchy-benchmark/activate-installer-overlay.sh", activation)
    executable(self.live / "usr/local/lib/omarchy-benchmark/preflight.sh", f'''#!/bin/bash
set -euo pipefail
echo activation >>"$BOX/calls"
exec bash {shlex.quote(str(self.live / 'usr/local/lib/omarchy-benchmark/activate-installer-overlay.sh'))} {shlex.quote(str(self.media))}
''')
    executable(self.live / "root/.automated_script.benchmark-original.sh", f'''#!/bin/bash
set -euo pipefail
echo autoinstall >>"$BOX/calls"
bash {shlex.quote(str(self.live / 'usr/local/bin/omarchy-wait-root-image-verify'))}
echo mock-disk-preparation >>"$BOX/calls"
printf 'prefetch=%s\\n' "${{OMARCHY_NO_PREFETCH-unset}}" >>"$BOX/calls"
''')
    self.environment = dict(os.environ, BOX=str(root), PATH=str(self.bin) + ":/usr/bin:/bin")
    self.environment.pop("OMARCHY_NO_PREFETCH", None)
    self.environment.pop("OMARCHY_VERIFY_PROGRESS", None)

  @staticmethod
  def checksum(path):
    path.with_name(path.name + ".sha256").write_text(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")

  def remap(self, source):
    # Keep normal shell utilities and read-only proc diagnostics. Redirect
    # optional serial output too, so a host serial device is never opened.
    for prefix in ("/usr/local/lib/omarchy-benchmark", "/root/.automated_script.benchmark-original.sh",
                   "/run/omarchy-benchmark", "/run/archiso/bootmnt", "/etc/systemd/system", "/var/log", "/dev/ttyS0"):
      source = source.replace(prefix, str(self.live) + prefix)
    return source

  def run(self, script):
    return subprocess.run(["bash"], input=self.remap(script.decode()), env=self.environment,
                          text=True, capture_output=True, timeout=15)

  def start_preflight(self, mode="candidate"):
    result = self.run(overlay.early_preflight_script(mode))
    self.preflight_state.write_text("active\n" if result.returncode == 0 else "failed\n")
    self.preflight_status.write_text(str(result.returncode) + "\n")
    return result

  def start_original(self, *, disabled=False, mode="candidate"):
    return self.run(overlay.wrapper(mode, disable_package_prefetch=disabled, early_preflight=True))

  def events(self):
    return self.calls.read_text().splitlines()

  def marker(self, name):
    return self.live / "run/omarchy-benchmark" / name


class EarlyVerifierContracts(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.wait_helper = pinned_file("usr/local/bin/omarchy-wait-root-image-verify")
    cls.verify_unit = pinned_file("etc/systemd/system/omarchy-root-image-verify.service")
    assert b"ExecStart=/usr/bin/sha256sum --check --strict omarchy-root.btrfs.qcow2.sha256" in cls.verify_unit

  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory(prefix="omarchy-early-verifier-contract-")
    self.addCleanup(self.temporary.cleanup)
    self.box = Sandbox(Path(self.temporary.name), self.wait_helper, self.verify_unit)

  def assert_success(self, result):
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

  def test_success_activates_and_hashes_once_before_disk_preparation(self):
    self.assert_success(self.box.start_preflight())
    self.assertEqual(self.box.marker("preflight-complete").read_text().strip(), "candidate")
    self.assertTrue(self.box.marker("early-preflight-started").exists())
    self.assert_success(self.box.start_original(disabled=True))
    events = self.box.events()
    for event in ("activation", "verifier-start", "autoinstall", "mock-disk-preparation"):
      self.assertEqual(events.count(event), 1, events)
    self.assertLess(events.index("activation"), events.index("verifier-start"))
    self.assertLess(events.index("verifier-start"), events.index("autoinstall"))
    self.assertLess(events.index("autoinstall"), events.index("mock-disk-preparation"))
    self.assertIn("prefetch=1", events)
    self.assertTrue((self.box.live / "activation-proof").exists())

  def test_archive_corruption_never_activates_installer_or_verifier(self):
    with (self.box.media / "installer-overlay.tar").open("ab") as output:
      output.write(b"archive-corruption")
    self.assertNotEqual(self.box.start_preflight().returncode, 0)
    self.assertFalse(self.box.marker("preflight-complete").exists())
    self.assertNotEqual(self.box.start_original().returncode, 0)
    self.assertEqual(self.box.events().count("activation"), 1)
    for event in ("verifier-start", "autoinstall", "mock-disk-preparation"):
      self.assertNotIn(event, self.box.events())
    self.assertFalse((self.box.live / "activation-proof").exists())

  def test_hash_mismatch_is_rejected_by_pinned_helper_before_disk_preparation(self):
    (self.box.media / "arch/x86_64/omarchy-root.btrfs.qcow2").write_bytes(b"bad-root-image\n")
    self.assert_success(self.box.start_preflight())
    result = self.box.start_original()
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("sha256 mismatch on the root image", result.stderr)
    self.assertEqual(self.box.events().count("verifier-start"), 1)
    self.assertEqual(self.box.events().count("autoinstall"), 1)
    self.assertNotIn("mock-disk-preparation", self.box.events())

  def test_duplicate_activation_is_refused_without_retrying_preflight(self):
    self.assert_success(self.box.start_preflight())
    self.assertNotEqual(self.box.start_preflight().returncode, 0)
    self.assertEqual(self.box.events().count("activation"), 1)
    self.assertEqual(self.box.events().count("verifier-start"), 1)
    self.assertNotEqual(self.box.start_original().returncode, 0)
    self.assertNotIn("autoinstall", self.box.events())

  def test_failed_activation_cannot_be_retried_after_payload_repair(self):
    archive = self.box.media / "installer-overlay.tar"
    with archive.open("ab") as output:
      output.write(b"corruption")
    self.assertNotEqual(self.box.start_preflight().returncode, 0)
    self.box.checksum(archive)
    self.assertNotEqual(self.box.start_preflight().returncode, 0)
    self.assertEqual(self.box.events().count("activation"), 1)
    self.assertNotIn("verifier-start", self.box.events())

  def test_missing_inactive_and_failed_service_all_block_original(self):
    self.assert_success(self.box.start_preflight())
    for state in ("missing", "inactive", "failed", "activating"):
      with self.subTest(state=state):
        self.box.preflight_state.write_text(state + "\n")
        self.assertNotEqual(self.box.start_original().returncode, 0)
    self.assertNotIn("autoinstall", self.box.events())
    self.assertEqual(self.box.events().count("activation"), 1)

  def test_active_service_requires_successful_exec_status(self):
    self.assert_success(self.box.start_preflight())
    for status in ("1\n", "\n", "invalid\n"):
      with self.subTest(status=status):
        self.box.preflight_status.write_text(status)
        self.assertNotEqual(self.box.start_original().returncode, 0)
    self.assertNotIn("autoinstall", self.box.events())

  def test_failed_status_query_blocks_original_even_if_it_prints_zero(self):
    self.assert_success(self.box.start_preflight())
    self.box.environment["EXEC_STATUS_QUERY_RC"] = "1"
    self.assertNotEqual(self.box.start_original().returncode, 0)
    self.assertNotIn("autoinstall", self.box.events())

  def test_missing_and_wrong_mode_completion_markers_block_original(self):
    self.assert_success(self.box.start_preflight())
    marker = self.box.marker("preflight-complete")
    marker.unlink()
    self.assertNotEqual(self.box.start_original().returncode, 0)
    for contents in ("", "control\n", "candidate extra\n"):
      with self.subTest(contents=contents):
        marker.write_text(contents)
        self.assertNotEqual(self.box.start_original().returncode, 0)
    self.assertNotIn("autoinstall", self.box.events())
    self.assertEqual(self.box.events().count("activation"), 1)

  def test_missing_or_empty_timing_marker_blocks_original(self):
    self.assert_success(self.box.start_preflight())
    for name in ("early-preflight-started", "early-preflight-finished"):
      marker = self.box.marker(name)
      original = marker.read_bytes()
      with self.subTest(marker=name, state="missing"):
        marker.unlink()
        self.assertNotEqual(self.box.start_original().returncode, 0)
      with self.subTest(marker=name, state="empty"):
        marker.write_bytes(b"")
        self.assertNotEqual(self.box.start_original().returncode, 0)
      marker.write_bytes(original)
    self.assertNotIn("autoinstall", self.box.events())
    self.assertEqual(self.box.events().count("activation"), 1)

  def test_control_preserves_prefetch_and_uses_same_early_gate(self):
    self.assert_success(self.box.start_preflight(mode="control"))
    self.assert_success(self.box.start_original(mode="control"))
    self.assertIn("prefetch=unset", self.box.events())
    self.assertEqual(self.box.marker("preflight-complete").read_text().strip(), "control")

  def test_archive_enables_oneshot_before_getty_and_keeps_default_unchanged(self):
    original = overlay.make_cpio({
      "config": (stat.S_IFREG | 0o644, b'LATEHOOKS="archiso"\n'),
      "init": (stat.S_IFREG | 0o755, b'"$mount_handler" /new_root\nrun_hookfunctions \'run_latehook\'\n'),
    })
    default, _ = overlay.build(original, "candidate", b"#!/bin/bash\ntrue\n")
    self.assertNotIn(SERVICE.encode(), default)
    combined, metadata = overlay.build(original, "candidate", b"#!/bin/bash\ntrue\n", early_preflight=True)
    entries = overlay.initramfs_files(combined)
    prefix = "omarchy-benchmark-payload/"
    unit = entries[prefix + "etc/systemd/system/" + SERVICE][1].decode()
    for setting in ("Type=oneshot", "RemainAfterExit=yes", "TimeoutStartSec=300"):
      self.assertIn(setting, unit)
    self.assertNotIn("DefaultDependencies=no", unit)
    dropins = [data.decode() for name, (_, data) in entries.items()
               if name.startswith(prefix + "etc/systemd/system/getty@tty1.service.d/") and name.endswith(".conf")]
    self.assertEqual(len(dropins), 1)
    self.assertIn("Requires=" + SERVICE, dropins[0])
    self.assertIn("After=" + SERVICE, dropins[0])
    self.assertTrue(metadata["early_preflight"])
    command = next(line.removeprefix("ExecStart=") for line in unit.splitlines() if line.startswith("ExecStart="))
    words = shlex.split(command)
    script = words[-1].lstrip("/")
    self.assertEqual(entries[prefix + script][1], overlay.early_preflight_script("candidate"))
    self.assertEqual(stat.S_IMODE(entries[prefix + script][0]), 0o755)
    with self.assertRaises(ValueError):
      overlay.build(original, "builder", b"#!/bin/bash\ntrue\n", early_preflight=True)


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True, help="Checkout containing the exact pinned PR145 git object")
  args, remaining = parser.parse_known_args()
  ISO_SOURCE = args.iso_source
  unittest.main(argv=[__file__, *remaining], verbosity=2)
