"""Experimental full-file SHA-256 chunk verifier; not wired into the installer.

The ISO builder must ship the ordered manifest as trusted ISO metadata, just as
it currently ships the expected whole-file SHA-256. This does not verify an
existing whole-file checksum: it is a different, fully covering hash format.
"""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat


CHUNK_SIZE = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
FORMAT = "omarchy-ordered-sha256-chunks-v1"


def unique_object(pairs):
  result = {}
  for key, value in pairs:
    if key in result:
      raise ValueError(f"duplicate manifest key: {key}")
    result[key] = value
  return result


def load_manifest(path):
  with open(path, "rb") as source:
    raw = source.read(MAX_MANIFEST_BYTES + 1)
  if len(raw) > MAX_MANIFEST_BYTES:
    raise ValueError("manifest exceeds size limit")
  value = json.loads(raw, object_pairs_hook=unique_object)
  if not isinstance(value, dict) or set(value) != {"format", "size", "chunk_size", "sha256"}:
    raise ValueError("unsupported manifest fields")
  if value["format"] != FORMAT or type(value["chunk_size"]) is not int or value["chunk_size"] != CHUNK_SIZE:
    raise ValueError("unsupported manifest format or chunk size")
  if type(value["size"]) is not int or value["size"] <= 0:
    raise ValueError("invalid image size")
  expected_count = (value["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE
  digests = value["sha256"]
  if not isinstance(digests, list) or len(digests) != expected_count:
    raise ValueError("manifest must cover exactly every image chunk")
  if any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in digests):
    raise ValueError("invalid SHA-256 digest")
  return value


def identity(info):
  return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_exact(source, buffer, count):
  view = memoryview(buffer)
  done = 0
  while done < count:
    size = source.readinto(view[done:count])
    if not size:
      raise ValueError("image ended before its declared size")
    done += size
  return view[:count]


def build_manifest(image, manifest):
  """Build-time only; use the returned manifest as trusted ISO metadata."""
  values = []
  with open(image, "rb", buffering=0) as source:
    before = os.fstat(source.fileno())
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
      raise ValueError("image must be a nonempty regular file")
    buffer = bytearray(CHUNK_SIZE)
    for offset in range(0, before.st_size, CHUNK_SIZE):
      block = read_exact(source, buffer, min(CHUNK_SIZE, before.st_size - offset))
      values.append(hashlib.sha256(block).hexdigest())
    if source.read(1) or identity(before) != identity(os.fstat(source.fileno())):
      raise ValueError("image changed while building the manifest")
  result = {"format": FORMAT, "size": before.st_size, "chunk_size": CHUNK_SIZE, "sha256": values}
  Path(manifest).write_text(json.dumps(result, separators=(",", ":")) + "\n")


def check_chunk(block, expected, index):
  if hashlib.sha256(block).hexdigest() != expected:
    raise ValueError(f"SHA-256 mismatch in image chunk {index}")


def verify(image, manifest, workers=4):
  """Read once, strictly sequentially; hash at most eight 4-MiB buffers."""
  if type(workers) is not int or not 1 <= workers <= 4:
    raise ValueError("worker count must be between one and four")
  expected = load_manifest(manifest)
  with open(image, "rb", buffering=0) as source:
    before = os.fstat(source.fileno())
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected["size"]:
      raise ValueError("image size differs from the manifest")
    if workers == 1:
      buffer = bytearray(CHUNK_SIZE)
      for index, digest in enumerate(expected["sha256"]):
        count = min(CHUNK_SIZE, before.st_size - index * CHUNK_SIZE)
        check_chunk(read_exact(source, buffer, count), digest, index)
    else:
      buffers = deque(bytearray(CHUNK_SIZE) for _ in range(workers * 2))
      pending = deque()
      with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, digest in enumerate(expected["sha256"]):
          buffer = buffers.popleft()
          count = min(CHUNK_SIZE, before.st_size - index * CHUNK_SIZE)
          block = read_exact(source, buffer, count)
          pending.append((pool.submit(check_chunk, block, digest, index), buffer))
          if len(pending) == workers * 2:
            future, buffer = pending.popleft()
            future.result()
            buffers.append(buffer)
        for future, _buffer in pending:
          future.result()
    if source.read(1) or identity(before) != identity(os.fstat(source.fileno())):
      raise ValueError("image changed while being verified")


def default_workers():
  return min(4, len(os.sched_getaffinity(0)))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("operation", choices=("build", "verify"))
  parser.add_argument("image", type=Path)
  parser.add_argument("manifest", type=Path)
  parser.add_argument("--workers", type=int, default=default_workers())
  args = parser.parse_args()
  try:
    if args.operation == "build":
      build_manifest(args.image, args.manifest)
    else:
      verify(args.image, args.manifest, args.workers)
  except (ValueError, OSError) as error:
    parser.exit(1, f"root image verification failed: {error}\n")


if __name__ == "__main__":
  main()
