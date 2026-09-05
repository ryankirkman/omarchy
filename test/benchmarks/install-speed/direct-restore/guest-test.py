#!/usr/bin/env python3
"""Verify the actual direct restore on disposable QEMU block devices.

This is a byte-correctness matrix, not an installation speed benchmark. It
requires four explicitly named, otherwise unused 96 MiB virtio target disks.
All targets are overwritten. No filesystem is mounted and no failed target is
repaired. Physical hardware and power-loss durability are outside this test.
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import traceback


MIB = 1024 * 1024
IMAGE_BYTES = 64 * MIB
TARGET_BYTES = 96 * MIB
CHUNK_BYTES = MIB
SOURCE_SERIAL = "OMARCHY_DIO_SOURCE"
PHASES_SHA256 = "8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458"
CASES = [(512, True), (512, False), (4096, True), (4096, False)]
ACTIVE_COMMANDS = None


def require(condition, message):
  if not condition:
    raise RuntimeError(message)


def now():
  return datetime.now(timezone.utc).isoformat()


def run(arguments):
  return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout.strip()


def save(path, value):
  temporary = path.with_suffix(path.suffix + ".tmp")
  require(not temporary.is_symlink(), f"Refusing output symlink: {temporary}")
  with temporary.open("w") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
  temporary.replace(path)


def audit(event, arguments):
  # Observe the real subprocess boundary without wrapping or replacing run().
  if event == "subprocess.Popen" and ACTIVE_COMMANDS is not None:
    executable, argv, cwd, _environment = arguments
    ACTIVE_COMMANDS.append({"executable": os.fsdecode(executable),
      "argv": [os.fsdecode(value) for value in argv], "cwd": cwd})


def read_exact(stream, length):
  data = bytearray()
  while len(data) < length:
    part = stream.read(length - len(data))
    require(bool(part), f"Unexpected end of device after {len(data)} of {length} bytes")
    data.extend(part)
  return bytes(data)


def digest_range(device, offset, length):
  checksum = hashlib.sha256()
  with device.open("rb", buffering=0) as stream:
    stream.seek(offset)
    remaining = length
    while remaining:
      data = read_exact(stream, min(CHUNK_BYTES, remaining))
      checksum.update(data)
      remaining -= len(data)
  return checksum.hexdigest()


def expected_chunk(offset, length):
  """Generate logical bytes independently of qemu-img and its map output."""
  require(0 <= offset and offset + length <= IMAGE_BYTES, "Invalid fixture byte range")
  data = bytearray(length)
  regions = [
    (0, 8 * MIB, b"\x3d"),
    (8 * MIB, 16 * MIB, bytes(range(256))),
    (40 * MIB, 48 * MIB, b"\x6b" * 4096 + b"\x00" * 4096),
    (63 * MIB + 512, 63 * MIB + 4096, b"\x95"),
    (IMAGE_BYTES - 512, IMAGE_BYTES, b"\xe1"),
  ]
  for start, end, pattern in regions:
    left, right = max(start, offset), min(end, offset + length)
    if left < right:
      count = right - left
      phase = (left - start) % len(pattern)
      repeated = pattern * ((phase + count + len(pattern) - 1) // len(pattern))
      data[left - offset:right - offset] = repeated[phase:phase + count]
  return bytes(data)


def expected_hashes():
  image = hashlib.sha256()
  sentinel = hashlib.sha256()
  for offset in range(0, IMAGE_BYTES, CHUNK_BYTES):
    image.update(expected_chunk(offset, CHUNK_BYTES))
  for _ in range((TARGET_BYTES - IMAGE_BYTES) // CHUNK_BYTES):
    sentinel.update(b"\xa7" * CHUNK_BYTES)
  prefill = hashlib.sha256()
  for _ in range(TARGET_BYTES // CHUNK_BYTES):
    prefill.update(b"\xa7" * CHUNK_BYTES)
  return {"image_sha256": image.hexdigest(), "trailing_sha256": sentinel.hexdigest(),
    "prefill_sha256": prefill.hexdigest()}


def inspect_device(serial, *, size=None, sector=None, readonly=False, offload=None):
  by_id = Path("/dev/disk/by-id") / ("virtio-" + serial)
  device = by_id.resolve(strict=True)
  metadata = device.stat()
  require(stat.S_ISBLK(metadata.st_mode), f"Not a block device: {by_id}")
  number = f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"
  sysfs = (Path("/sys/dev/block") / number).resolve(strict=True)
  require(not (sysfs / "partition").exists(), f"Refusing partition: {device}")
  require("virtio" in str(sysfs), f"Not a virtio block device: {device}")
  require((sysfs / "serial").read_text().strip() == serial,
    f"Unexpected serial for {device}")
  # Reject active children as well as direct mounts; no target is partitioned.
  children = [child for child in sysfs.iterdir() if (child / "partition").exists()]
  require(not children, f"Refusing partitioned fixture disk: {device}")
  for relation in ("holders", "slaves"):
    require(not list((sysfs / relation).iterdir()), f"Device has {relation}: {device}")
  mount_numbers = {line.split()[2] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
  require(number not in mount_numbers, f"Device is mounted: {device}")
  for line in Path("/proc/swaps").read_text().splitlines()[1:]:
    swap = Path(line.split()[0])
    swap_stat = swap.stat()
    if stat.S_ISBLK(swap_stat.st_mode):
      require(swap_stat.st_rdev != metadata.st_rdev, f"Device is active swap: {device}")
  actual_size = int(run(["blockdev", "--getsize64", str(device)]))
  logical = int(run(["blockdev", "--getss", str(device)]))
  physical = int(run(["blockdev", "--getpbsz", str(device)]))
  ro = int(run(["blockdev", "--getro", str(device)]))
  require(ro == int(readonly), f"Unexpected read-only flag {ro}: {device}")
  if size is not None:
    require(actual_size == size, f"Unexpected size {actual_size}: {device}")
  if sector is not None:
    require(logical == sector, f"Unexpected logical sector {logical}: {device}")
  require(physical >= logical and physical % logical == 0,
    f"Incompatible physical sector {physical}: {device}")
  capabilities = {name: int((sysfs / "queue" / name).read_text())
    for name in ("write_zeroes_max_bytes", "discard_max_bytes")}
  if offload is not None:
    require(all((value > 0) == offload for value in capabilities.values()),
      f"Unexpected zero/discard capabilities: {device}: {capabilities}")
  return {"serial": serial, "by_id": str(by_id), "device": str(device),
    "major_minor": number, "sysfs": str(sysfs), "size_bytes": actual_size,
    "logical_sector_bytes": logical, "physical_sector_bytes": physical,
    "read_only": bool(ro), "capabilities": capabilities,
    "unmounted": True, "no_partitions_holders_slaves_or_swap": True}


def flush(device):
  with device.open("rb", buffering=0) as stream:
    os.fsync(stream.fileno())
  run(["blockdev", "--flushbufs", str(device)])


def prefill(device):
  # O_EXCL also lets the kernel reject a newly mounted block device.
  descriptor = os.open(device, os.O_WRONLY | os.O_EXCL)
  try:
    sentinel = b"\xa7" * CHUNK_BYTES
    for _ in range(TARGET_BYTES // CHUNK_BYTES):
      remaining = memoryview(sentinel)
      while remaining:
        written = os.write(descriptor, remaining)
        require(written > 0, "Short prefill write")
        remaining = remaining[written:]
    os.fsync(descriptor)
  finally:
    os.close(descriptor)
  run(["blockdev", "--flushbufs", str(device)])


def compare_target(device):
  image, trailing, whole = hashlib.sha256(), hashlib.sha256(), hashlib.sha256()
  first_mismatch = None
  with device.open("rb", buffering=0) as stream:
    for offset in range(0, TARGET_BYTES, CHUNK_BYTES):
      actual = read_exact(stream, CHUNK_BYTES)
      expected = (expected_chunk(offset, CHUNK_BYTES) if offset < IMAGE_BYTES
        else b"\xa7" * CHUNK_BYTES)
      whole.update(actual)
      (image if offset < IMAGE_BYTES else trailing).update(actual)
      if actual != expected and first_mismatch is None:
        index = next(index for index, (left, right) in enumerate(zip(actual, expected)) if left != right)
        first_mismatch = {"offset": offset + index, "actual_byte": actual[index],
          "expected_byte": expected[index]}
  return {"image_sha256": image.hexdigest(), "trailing_sha256": trailing.hexdigest(),
    "whole_device_sha256": whole.hexdigest(), "bytes_compared": TARGET_BYTES,
    "first_mismatch": first_mismatch}


def load_restore(phases):
  source = phases.read_bytes()
  require(hashlib.sha256(source).hexdigest() == PHASES_SHA256,
    "Prepared phases source does not match the reviewed direct-restore pin")
  functions = [node for node in ast.parse(source).body
    if isinstance(node, ast.FunctionDef) and node.name == "_restore_root_image"]
  require(len(functions) == 1, "Expected one actual _restore_root_image function")
  namespace = {"Path": Path, "os": os, "subprocess": subprocess}
  exec(compile(ast.Module(body=functions, type_ignores=[]), str(phases), "exec"), namespace)
  return namespace["_restore_root_image"]


def invoke_restore(restore, source, device, record, expect_error=False):
  global ACTIVE_COMMANDS
  commands = []
  ACTIVE_COMMANDS = commands
  caught = None
  try:
    restore(source, str(device))
  except RuntimeError as error:
    caught = str(error)
  finally:
    ACTIVE_COMMANDS = None
    record["restore_commands"] = commands
    record["restore_runtime_error"] = caught
  expected = ["qemu-img", "convert", "-q", "-f", "qcow2", "-O", "raw", "-W", "-n",
    "-t", "none", "-m", str(min(max(1, os.cpu_count() or 1) * 2, 16)), str(source), str(device)]
  require(len(commands) == 1 and commands[0]["argv"] == expected,
    "Restore must execute exactly one reviewed direct command without fallback")
  require(not any("target-is-zero" in arg for arg in commands[0]["argv"]),
    "Zero-target assumption is forbidden")
  if expect_error:
    require(caught is not None and caught.startswith("root filesystem restore failed:"),
      "Read-only destination did not propagate the real qemu-img failure")
  else:
    require(caught is None, f"Actual restore failed: {caught}")


def normalized_map(entries):
  return [{key: value for key, value in entry.items() if key != "filename"} for entry in entries]


def inspect_source(manifest):
  source_bytes = manifest["source_bytes"]
  require(type(source_bytes) is int and 0 < source_bytes <= IMAGE_BYTES,
    "Unexpected qcow2 container size")
  require(manifest["virtual_size"] == IMAGE_BYTES, "Unexpected fixture virtual size")
  source = inspect_device(SOURCE_SERIAL, readonly=True)
  # Raw-attached container files may receive zero padding to a 512-byte boundary.
  require(source["size_bytes"] == (source_bytes + 511) // 512 * 512,
    "Source block-device size differs from the qcow2 container")
  device = Path(source["device"])
  source["container_sha256"] = digest_range(device, 0, source_bytes)
  require(source["container_sha256"] == manifest["source_sha256"], "Source container hash mismatch")
  info = json.loads(run(["qemu-img", "info", "--output=json", "-f", "qcow2", str(device)]))
  require(info["format"] == "qcow2" and info["virtual-size"] == IMAGE_BYTES,
    "Unexpected source format or virtual size")
  require(not any(info.get(key) for key in ("backing-filename", "full-backing-filename", "data-file")),
    "Source must be self-contained")
  format_data = info.get("format-specific", {}).get("data", {})
  require(not format_data.get("data-file"), "External qcow2 data files are forbidden")
  require(info.get("cluster-size") == MIB and format_data.get("compression-type") == "zstd",
    "Fixture must exercise 1 MiB zstd-compressed qcow2 clusters")
  mapping = json.loads(run(["qemu-img", "map", "--output=json", "-f", "qcow2", str(device)]))
  require(normalized_map(mapping) == normalized_map(manifest["map"]),
    "Guest source allocation map differs from the host fixture")
  source["qemu_img_info"] = info
  source["qemu_img_map"] = mapping
  return source


def test_case(restore, source, guard, sector, offload, expected, work):
  label = f"{sector}-{'on' if offload else 'off'}"
  record = {"case": label, "status": "running", "hardware": guard, "expected": expected}
  path = work / f"case-{label}.json"
  save(path, record)
  try:
    again = inspect_device(guard["serial"], size=TARGET_BYTES, sector=sector, offload=offload)
    require(again == guard, "Target identity or capabilities changed before prefill")
    device = Path(guard["device"])
    prefill(device)
    record["prefill_sha256"] = digest_range(device, 0, TARGET_BYTES)
    require(record["prefill_sha256"] == expected["prefill_sha256"],
      "Full-device sentinel prefill verification failed")
    record["prefill_bytes_verified"] = TARGET_BYTES
    save(path, record)
    require(inspect_device(guard["serial"], size=TARGET_BYTES, sector=sector, offload=offload) == guard,
      "Target identity or capabilities changed before restore")
    invoke_restore(restore, source, device, record)
    flush(device)
    record["verification"] = compare_target(device)
    require(record["verification"]["first_mismatch"] is None, "Restored target bytes differ")
    for field in ("image_sha256", "trailing_sha256"):
      require(record["verification"][field] == expected[field], f"Unexpected {field}")
    record["status"] = "passed"
  except Exception as error:
    record["status"] = "failed"
    record["error"] = f"{type(error).__name__}: {error}"
    record["traceback"] = traceback.format_exc()
  finally:
    save(path, record)
  return record


def test_readonly_failure(restore, source, successful_case, work):
  record = {"status": "running", "case": successful_case["case"],
    "target_left_read_only": False}
  path = work / "readonly-error.json"
  save(path, record)
  try:
    previous = successful_case["hardware"]
    guard = inspect_device(previous["serial"], size=TARGET_BYTES,
      sector=previous["logical_sector_bytes"],
      offload=all(value > 0 for value in previous["capabilities"].values()))
    require(guard == previous, "Completed target changed before read-only error test")
    device = Path(guard["device"])
    record["before_sha256"] = digest_range(device, 0, TARGET_BYTES)
    require(record["before_sha256"] == successful_case["verification"]["whole_device_sha256"],
      "Completed target bytes changed before read-only error test")
    run(["blockdev", "--setro", str(device)])
    record["target_left_read_only"] = True
    record["hardware"] = inspect_device(previous["serial"], size=TARGET_BYTES,
      sector=previous["logical_sector_bytes"], readonly=True)
    invoke_restore(restore, source, device, record, expect_error=True)
    record["after_sha256"] = digest_range(device, 0, TARGET_BYTES)
    require(record["after_sha256"] == record["before_sha256"],
      "Failed restore changed bytes on the read-only target")
    record["bytes_verified_unchanged"] = TARGET_BYTES
    record["status"] = "passed"
  except Exception as error:
    record["status"] = "failed"
    record["error"] = f"{type(error).__name__}: {error}"
    record["traceback"] = traceback.format_exc()
  finally:
    # Deliberately leave the completed fixture read-only; never repair failures.
    save(path, record)
  return record


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--work-dir", type=Path, required=True)
  parser.add_argument("--phases", type=Path, required=True)
  parser.add_argument("--fixture-manifest", type=Path, required=True)
  args = parser.parse_args()
  work = args.work_dir.resolve()
  require(work.is_relative_to(Path("/tmp")) and work != Path("/tmp"),
    "Test artifacts must be in a dedicated directory below /tmp")
  work.mkdir(parents=True, exist_ok=True)
  outputs = [work / "results.json", work / "readonly-error.json"] + [
    work / f"case-{sector}-{'on' if offload else 'off'}.json" for sector, offload in CASES]
  require(all(not path.exists() and not path.is_symlink() for path in outputs),
    "Refusing to overwrite prior test evidence; use a fresh work directory")
  result = {"schema_version": 1, "status": "running", "started_at": now(),
    "measurement_valid": False, "speedup_claim": False,
    "scope": "Real qemu-img byte correctness on four disposable virtual block devices",
    "limits": ["No installation-speed claim", "No physical hardware claim",
      "No power-loss or persistence-after-host-crash claim", "No mounted filesystem workload"],
    "phases_sha256": PHASES_SHA256, "cases": []}
  save(work / "results.json", result)
  manifest = None
  source_guard = None
  try:
    require(os.geteuid() == 0, "Guest test requires root")
    virtualization = run(["systemd-detect-virt", "--vm"])
    require(virtualization in ("qemu", "kvm"), "Guest must report QEMU or KVM virtualization")
    result["virtualization"] = virtualization
    result["qemu_img_executable"] = shutil.which("qemu-img")
    result["qemu_img_version"] = run(["qemu-img", "--version"])
    require(result["qemu_img_version"].splitlines()[0].startswith("qemu-img version 8.2.2 "),
      "Use the matching portable qemu-img 8.2.2 bundle")
    restore = load_restore(args.phases)
    manifest = json.loads(args.fixture_manifest.read_text())
    result["fixture_manifest_sha256"] = hashlib.sha256(args.fixture_manifest.read_bytes()).hexdigest()
    expected = expected_hashes()
    require(expected["image_sha256"] == manifest["expected_raw_sha256"],
      "Independent guest fixture generator disagrees with the host manifest")
    result["expected"] = expected
    source_guard = inspect_source(manifest)
    result["source_before"] = source_guard
    guards = []
    for sector, offload in CASES:
      serial = f"OMARCHY_DIO_{sector}_{'ON' if offload else 'OFF'}"
      guards.append(inspect_device(serial, size=TARGET_BYTES, sector=sector, offload=offload))
    identities = [entry["major_minor"] for entry in [source_guard, *guards]]
    require(len(identities) == len(set(identities)), "Source and target devices must be distinct")
    # All source and device checks above complete before the first write.
    save(work / "results.json", result)
    source = Path(source_guard["device"])
    for (sector, offload), guard in zip(CASES, guards):
      record = test_case(restore, source, guard, sector, offload, expected, work)
      result["cases"].append(record)
      save(work / "results.json", result)
    successful = [case for case in result["cases"] if case["status"] == "passed"]
    if successful:
      result["readonly_error"] = test_readonly_failure(restore, source, successful[-1], work)
    else:
      result["readonly_error"] = {"status": "skipped", "reason": "No completed verified target"}
    require(len(successful) == len(CASES), "At least one target case failed")
    require(result["readonly_error"]["status"] == "passed", "Real read-only failure case failed")
    result["status"] = "passed"
  except Exception as error:
    result["status"] = "failed"
    result["error"] = f"{type(error).__name__}: {error}"
    result["traceback"] = traceback.format_exc()
  finally:
    if source_guard is not None and manifest is not None:
      try:
        result["source_after"] = inspect_source(manifest)
        require(result["source_after"] == source_guard, "Read-only source identity or bytes changed")
        result["source_unchanged"] = True
      except Exception as error:
        result["status"] = "failed"
        result["source_unchanged"] = False
        result["source_error"] = f"{type(error).__name__}: {error}"
    result["completed_at"] = now()
    save(work / "results.json", result)
  print(json.dumps({"status": result["status"], "results": str(work / "results.json"),
    "speedup_claim": False}, sort_keys=True))
  return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
  sys.addaudithook(audit)
  sys.exit(main())
