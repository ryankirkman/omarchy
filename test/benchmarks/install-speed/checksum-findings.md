# Exact SHA-256 optimization: rejected

Replacing `sha256sum` with `openssl dgst -sha256` does not provide evidence of a useful speedup. Keep the existing verifier and its strict manifest handling. This experiment changes no installer behavior.

The proposed prebuilt-root installer uses `sha256sum --check --strict` in `configs/airootfs/etc/systemd/system/omarchy-root-image-verify.service`. The waiter collects its result before disk formatting and obtains progress from the hashing process's open stream. A replacement would need to preserve that failure gate, manifest validation, journal diagnostics, systemd timeouts, and progress reporting. No such integration is warranted by these measurements.

## Measured result

The complete official `omarchy-4.0.2.iso` is 6,227,752,960 bytes. Every timed command produced its expected SHA-256, `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. The ISO is a real multi-gigabyte input for this phase experiment; it is not the proposed prebuilt-root qcow2 payload, which is absent from this release.

| Implementation | Median wall seconds | Median process CPU seconds | Repetitions |
| --- | ---: | ---: | ---: |
| Arch coreutils 9.11, extracted from this ISO | 12.027 | 4.446 | 3 |
| Arch OpenSSL 3.6.3, extracted from this ISO | 11.974 | 4.773 | 3 |
| Host coreutils 9.4, context only | 14.035 | 4.627 | 3 |

The apparent OpenSSL wall speedup is **1.004×**, effectively no difference. OpenSSL consumed about 7.4% more process CPU in these medians. Concurrent QEMU and benchmark work caused substantial scheduling variation: Arch coreutils ranged from 10.365 to 20.953 wall seconds while its process CPU ranged from 4.340 to 4.634 seconds. This experiment cannot distinguish small changes reliably and provides no basis for claiming an installation speedup.

Both the actual Arch coreutils binary and the host coreutils binary depend on `libcrypto.so.3`, verified with `readelf -d`. The CPU advertises SHA-NI. OpenSSL substitution therefore does not newly introduce hardware SHA acceleration to this system; the existing verifier already uses the crypto library. Version numbers alone are insufficient to determine which hashing implementation a distribution built.

The entire input was read once before timing, methods ran sequentially, and their order reversed every other round. Every measured process reported zero input blocks, consistent with warm cache. No global cache dropping, medium write, or host installation occurred. These timings exclude USB transfer bottlenecks, boot, image deployment, package configuration, reboot, and desktop readiness. The recorded hardware is an AMD EPYC 9V74 with a shared eight-CPU cgroup quota and 20 GiB memory limit.

Raw measurements, versions, exact digests, CPU times, cache policy, and environment details are in `checksum-results.json`. The separate `hash/` chunk-manifest prototype is a different experiment: it changes the manifest format and is not evidence for an exact-digest replacement.

## Reproduce

Use the binaries and libraries from the ISO being evaluated. For this exact release, `xorriso -indev omarchy-4.0.2.iso -find /arch/x86_64/airootfs.sfs -exec report_lba --` reports contiguous extents beginning at LBA 132242. Its byte offset is 270831616. Check this anew for another image; never assume an offset or contiguous extents across releases.

```bash
unsquashfs -processors 2 -no-progress -no-xattrs \
  -d arch-root -o 270831616 omarchy-4.0.2.iso \
  usr/bin/sha256sum usr/bin/openssl \
  usr/lib/ld-linux-x86-64.so.2 usr/lib/libc.so.6 \
  usr/lib/libcrypto.so.3 usr/lib/libssl.so.3

python3 test/benchmarks/install-speed/checksum-benchmark.py \
  --image /absolute/path/omarchy-4.0.2.iso \
  --manifest /absolute/path/omarchy-4.0.2.iso.sha256 \
  --arch-root /absolute/path/arch-root \
  --output /absolute/path/checksum-results.json \
  --rounds 3 --include-host

python3 test/benchmarks/install-speed/checksum-contract-test.py \
  --arch-root /absolute/path/arch-root \
  --output /absolute/path/checksum-contract-results.json
```

The benchmark invokes the extracted Arch dynamic loader with the extracted library directory. It runs native on the host CPU and kernel; it does not emulate Arch boot or the installer. The package inventory embedded in the ISO identifies `coreutils 9.11-2`, `openssl 3.6.3-1`, and `glibc 2.44+r24+g16be1518495f-1`; direct version output agrees.

Five focused checks against the actual ISO coreutils verify that a valid manifest succeeds, while corrupt image bytes, malformed-only manifests, a valid record followed by a malformed record, and a missing image fail. Results are in `checksum-contract-results.json`. These are checks of the retained verifier's contract, not a substitute for guest validation of the complete installer.
