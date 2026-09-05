#!/bin/bash

set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

python3 - "$ROOT" <<'PYTEST'
import ctypes
import hashlib
import importlib.util
import mmap
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("iso_vm", Path(sys.argv[1]) / "test/benchmarks/iso-vm.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def resident_pages(path):
  # Independent residency observation around the real eviction operation.
  length = path.stat().st_size
  pages = (length + mmap.PAGESIZE - 1) // mmap.PAGESIZE
  vector = (ctypes.c_ubyte * pages)()
  with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_COPY) as mapping:
    first = ctypes.c_char.from_buffer(mapping)
    address = ctypes.addressof(first)
    del first
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(length), vector) == 0
  return sum(value & 1 for value in vector)


with tempfile.TemporaryDirectory(prefix="omarchy-source-cache-", dir="/tmp") as directory:
  sources = []
  for index, size in enumerate((2 * 1024 * 1024, 3 * 1024 * 1024 + 17)):
    path = Path(directory) / f"source-{index}.bin"
    path.write_bytes(os.urandom(size))
    path.chmod(0o400)
    sources.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert resident_pages(path) > 0, "fixture did not begin with real cached pages"

  evidence = module.evict_and_measure_sources(sources)
  can_evict = all(item["resident_pages"] == 0 for item in evidence)
  assert [item["path"] for item in evidence] == [item["path"] for item in sources]
  for source, record in zip(sources, evidence):
    path = Path(source["path"])
    assert resident_pages(path) == record["resident_pages"], "reported evidence disagrees with actual mincore"
    assert record["page_count"] == (path.stat().st_size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    assert record["file_bytes"] == path.stat().st_size
    assert record["page_size"] == mmap.PAGESIZE and record["sampled_at_monotonic_s"] > 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"], "source bytes changed"
    assert resident_pages(path) > 0, "reading the source did not restore residency"

  supervisor = object.__new__(module.Supervisor)
  supervisor.args = SimpleNamespace(source_cache="cold", kernel=None)
  supervisor.manifest = {"iso": sources[0]["path"], "iso_sha256": sources[0]["sha256"],
                         "extra_media": [sources[1]]}
  # Real warmed pages remain if the eviction request has no effect. The runner
  # must reject this, retaining the measured nonzero counts for diagnosis.
  with patch.object(module.os, "posix_fadvise", lambda *_: None):
    try:
      supervisor.prepare_source_cache()
    except RuntimeError as error:
      assert "remains resident" in str(error)
    else:
      raise AssertionError("runner accepted an ineffective eviction")
  assert any(item["resident_pages"] > 0 for item in supervisor.manifest["source_cache_evidence"])
  assert "source_cache_verified_at_monotonic_s" not in supervisor.manifest

  try:
    supervisor.prepare_source_cache()
  except RuntimeError as error:
    assert "remains resident" in str(error)
    assert any(item["resident_pages"] > 0 for item in supervisor.manifest["source_cache_evidence"])
  else:
    verified = supervisor.manifest["source_cache_verified_at_monotonic_s"]
    assert all(item["resident_pages"] == 0 and item["sampled_at_monotonic_s"] <= verified
               for item in supervisor.manifest["source_cache_evidence"])
    assert all(resident_pages(Path(source["path"])) == 0 for source in sources)
    can_evict = True

  for unsupported in (
    patch.object(module.sys, "platform", "darwin"),
    patch.object(module.os, "posix_fadvise", None),
    patch.object(module.ctypes, "CDLL", return_value=SimpleNamespace()),
  ):
    with unsupported:
      try:
        module.evict_and_measure_sources(sources)
      except RuntimeError as error:
        assert "requires Linux" in str(error)
      else:
        raise AssertionError("unsupported cache verification was accepted")
  try:
    module.evict_and_measure_sources([{"path": "/dev/null", "sha256": "0" * 64}])
  except RuntimeError as error:
    assert "regular file" in str(error)
  else:
    raise AssertionError("nonregular source was accepted")

if not can_evict:
  if os.environ.get("OMARCHY_REQUIRE_COLD_EVICTION") == "1":
    raise AssertionError("native cold-cache prerequisite failed: fresh regular source pages remain resident")
  print("ok - real ineffective eviction fails closed; positive eviction unavailable on this filesystem")
else:
  print("ok - real source pages are evicted, independently verified, and failed eviction is rejected")
PYTEST
