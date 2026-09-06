# Parallel source verification experiment

This is an isolated prototype, not an installer change. It investigates verifying an ordered manifest of SHA-256 chunk digests using a single sequential reader and a bounded worker pool. The existing ISO PR #145 verifier hashes the complete source image before permitting destructive install steps. This experiment retains complete pre-install content verification, but requires a different build-generated manifest format.

## Measured outcome

Five randomized-order, separate-process trials on a warmed 512-MiB slice of the official Omarchy 4.0.2 ISO produced the following medians. Raw observations and runtime versions are in `results.json`.

| Method | Median seconds | Speedup versus host sha256sum |
| --- | ---: | ---: |
| Host GNU sha256sum | 0.8490 | 1.000× |
| Host OpenSSL SHA-256 | 0.8869 | 0.957× |
| Chunk manifest, one worker | 1.1389 | 0.745× |
| Chunk manifest, two workers | 0.7534 | 1.127× |
| Chunk manifest, four workers | 0.6291 | 1.350× |

These figures are exploratory. Other agents ran work in the same container, and a three-command diagnostic of the extracted Arch binary overlapped the last benchmark round; the raw samples show substantial timing variation. Do not use these measurements to claim a shipping improvement. The actual ISO uses Arch coreutils 9.11 and OpenSSL 3.6.3; this host uses coreutils 9.4 and OpenSSL 3.0.13. Both coreutils binaries link OpenSSL, so this is not a comparison of an unaccelerated baseline with an accelerated candidate. The candidate Python runtime is also the host runtime. A faster comparison against an unmatched host baseline does not establish a faster ISO installation.

An earlier in-process experiment suggested 1.86×; the fuller separate-process comparison reduced this to 1.35×. The earlier figure is deliberately not a result to optimize against. No complete boot or installation was timed for this experiment, and no source-integrity checks were skipped.

Recommendation: keep this prototype out of the default installer until it shows repeatable improvement against the exact ISO binaries in a controlled full-image test and then a real boot-to-install test. A same-format acceleration would have substantially lower integration cost. The `checksum*` experiment beside this directory investigates that route separately.

## Coverage and resource limits

- The manifest fixes the format identifier, exact file size, chunk size of 4 MiB, exact number of chunks, and an ordered SHA-256 digest for every byte range. It accepts no unknown or duplicate fields and caps manifest input at 8 MiB.
- The verifier reads the source file once in increasing file-offset order. Worker threads receive already-read memory buffers, so parallel hashing introduces no random source reads.
- At most four workers and eight 4-MiB buffers are used. This bounds data buffers at 32 MiB, in addition to interpreter and manifest memory. One-worker mode uses one 4-MiB buffer.
- Every digest must match; truncated or extended files, altered chunk order, corrupt boundaries, malformed manifests, and source metadata changes cause failure. Before/after descriptor metadata checks supplement the expectation that the ISO is read-only; they are not a guarantee against malicious concurrent same-metadata file replacement.
- A manifest is trusted only to the same extent as the ISO distribution that supplies it. As with an adjacent `.sha256` file, it does not authenticate an attacker-controlled image if the attacker can also replace all expected metadata. Keep existing ISO signature validation and publication of the whole-file checksum.
- The chunk manifest is not the existing whole-file SHA-256, and cannot be derived from that digest. It must be generated from the source image at build time. It provides complete SHA-256 collision-resistant coverage with bound ordering and size, but changes the distribution metadata contract.

Eight focused tests exercise correct acceptance at each worker count, corrupted chunk boundaries and the last partial chunk, truncated/appended content, reordered image chunks, absent/extra digest entries, malformed metadata, duplicate JSON keys, and digest-order binding. They passed on this host.

## Reproduction

Use a representative, non-sparse source file that fits in page cache for the CPU-bound experiment. The recorded fixture is the first 536870912 bytes of the official 4.0.2 ISO, with SHA-256 `7a7e6ef32e35d272c6923d6ece01105f8e546660485ee36c8f4a4f88ce1cea67`. The large fixture is intentionally not checked into the repository.

```bash
python3 -m unittest -v test_chunk_sha256.py
python3 benchmark_hash.py /absolute/path/to/fixture.bin new-results.json --repeats 5
python3 benchmark_hash.py /absolute/path/to/fixture.bin matched-results.json --repeats 5 --arch-root /absolute/path/to/extracted/arch-root
```

The optional Arch comparison uses the extracted ISO dynamic loader and library path. It still uses host Python for the candidate, so also repeat the candidate under the live ISO runtime before making a decision. Benchmarking includes process startup and manifest validation, excludes build-time manifest generation, and randomizes method order deterministically.

For a prototype-only manual verification:

```bash
python3 chunk_sha256.py build image.qcow2 image.qcow2.chunks.json
python3 chunk_sha256.py verify image.qcow2 image.qcow2.chunks.json --workers 4
```

## Integration gates if this route survives measurement

Generate the manifest beside the image in the ISO builder; require it before starting the existing verification service; retain the old checksum for compatibility and a possible single-CPU fallback; keep the service's idle I/O class, low CPU priority, and size-based timeout. Adapt failure logging currently filtered to `_COMM=sha256sum`. Continue waiting for successful completion before both full-disk formatting and free-space partitioning. Do not rely on bytes-read progress as a successful verification verdict.

Measure cold slow-USB, cold fast-USB/NVMe, single-CPU, constrained-memory, and multicore cases. Slow media still require a complete read and may gain essentially nothing. Single-worker results already regress here. Avoid promising a universal speedup or enabling a multicore default purely from warmed-host results.
