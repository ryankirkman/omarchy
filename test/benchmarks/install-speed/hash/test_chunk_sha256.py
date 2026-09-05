import json
from pathlib import Path
import tempfile
import unittest

from chunk_sha256 import CHUNK_SIZE, build_manifest, load_manifest, verify


class ChunkManifestTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.image = Path(self.temp.name) / "image"
    self.manifest = Path(self.temp.name) / "manifest.json"
    self.original = b"A" * CHUNK_SIZE + b"B" * CHUNK_SIZE + b"last partial chunk"
    self.image.write_bytes(self.original)
    build_manifest(self.image, self.manifest)

  def test_every_worker_count_accepts_complete_valid_image(self):
    for workers in (1, 2, 3, 4):
      verify(self.image, self.manifest, workers)

  def test_corruption_at_start_boundaries_and_partial_tail_is_rejected(self):
    for offset in (0, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE * 2 - 1, CHUNK_SIZE * 2, len(self.original) - 1):
      with self.subTest(offset=offset):
        self.image.write_bytes(self.original)
        with self.image.open("r+b") as image:
          image.seek(offset)
          image.write(b"!")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
          verify(self.image, self.manifest, 4)

  def test_truncated_and_appended_images_are_rejected(self):
    for contents in (self.original[:-1], self.original + b"extra", b""):
      self.image.write_bytes(contents)
      with self.assertRaisesRegex(ValueError, "size differs"):
        verify(self.image, self.manifest)

  def test_reordered_chunks_are_rejected(self):
    self.image.write_bytes(b"B" * CHUNK_SIZE + b"A" * CHUNK_SIZE + b"last partial chunk")
    with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
      verify(self.image, self.manifest)

  def test_missing_or_extra_chunk_is_rejected(self):
    original = load_manifest(self.manifest)
    for digests in (original["sha256"][:-1], original["sha256"] + [original["sha256"][-1]]):
      self.manifest.write_text(json.dumps({**original, "sha256": digests}))
      with self.assertRaisesRegex(ValueError, "exactly every"):
        verify(self.image, self.manifest)

  def test_invalid_metadata_is_rejected(self):
    original = load_manifest(self.manifest)
    variants = [
      {**original, "chunk_size": 1},
      {**original, "size": True},
      {**original, "size": -1},
      {**original, "unexpected": "field"},
      {**original, "format": "unknown"},
      {**original, "sha256": ["z" * 64] * 3},
    ]
    for metadata in variants:
      self.manifest.write_text(json.dumps(metadata))
      with self.assertRaises(ValueError):
        verify(self.image, self.manifest)

  def test_duplicate_json_keys_are_rejected(self):
    value = self.manifest.read_text().rstrip()
    self.manifest.write_text(value[:-1] + ',"size":17}')
    with self.assertRaisesRegex(ValueError, "duplicate"):
      verify(self.image, self.manifest)

  def test_manifest_digest_order_is_binding(self):
    value = load_manifest(self.manifest)
    value["sha256"][0], value["sha256"][1] = value["sha256"][1], value["sha256"][0]
    self.manifest.write_text(json.dumps(value))
    with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
      verify(self.image, self.manifest)


if __name__ == "__main__":
  unittest.main()
