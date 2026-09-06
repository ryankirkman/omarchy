# Limine hash command startup: source-only lead

A dedicated argv launch for `b2sum` could remove shell startup from Limine's hash calculations. This is an unimplemented lead with no measured saving. It does not justify removing either of the two independent hash calculations, their comparison, or mismatch retries. The roughly 9.1–9.4-second boot-finalization branch in [run 34005907730](results/kvm-attempts/34005907730/README.md) includes other work; its duration is not attributable to hashing.

The reviewed source is `limine-entry-tool` commit `26b1879c862a55bae4e8777d48b2f917c68ef347`, version 1.37.1. In [Utility.java:118–173](https://gitlab.com/Zesko/limine-entry-tool/-/blob/26b1879c862a55bae4e8777d48b2f917c68ef347/src/main/java/org/limine/entry/tool/processes/Utility.java#L118), each `computeBlake2` runs two asynchronous `b2sum` calculations, compares their results, and permits three attempts when results differ. Each calculation currently constructs a command string and reaches `new ProcessBuilder("bash", "-c", commandLine)` at lines 282–285. `Config.HASH_COMMAND` is the fixed string `b2sum`.

The narrow change would give only this hash call an argv-based subprocess path, preserving merged output capture, exit handling, error formatting, and hash extraction. Keep the generic shell helper for other commands. This removes two shell startup/exec stages per successful first-attempt digest, not necessarily two additional processes: Bash can replace itself with the final command. Missing executables, nonzero exits, interruption, and literal filename handling require explicit equivalence review; replacing `bash -c` is not automatically behavior-neutral.

No file hashing or copying should be skipped. `Utility.copyFileIfMissingOrDifferent` (lines 201–219) compares source and destination hashes when the destination exists; a missing destination is copied directly. `LimineManager.addUki` (lines 250–256) performs that copy decision, and `createUkiBootConfig` (lines 330–363) independently hashes the destination for the boot entry when verification is enabled. In that path, a new destination entails two shell stages for its final digest; an existing destination entails six across source, destination, and final boot-entry digests, before any retries. This is a conditional source count, not an observed count for the native samples.

## Exact runtime pin and build gap

The official 4.0.2 ISO has SHA256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`. Its `var/cache/omarchy/mirror/offline/limine-mkinitcpio-hook-1.37.1-1-x86_64.pkg.tar.zst` provides `limine-entry-tool`; the accepted native package inventory contains that package/version. The native executable was streamed from this archive without executing it and its 19,990,920 bytes independently hashed against the package MTREE:

| Package member | SHA256 |
| --- | --- |
| `usr/lib/limine/limine-entry-tool` (ELF executable) | `17280c67791044d491b73e07dba628e55e7224ba307695313e5e2a8d22a37785` |
| `usr/bin/limine-entry-tool` (wrapper) | `3a3d9a07cf36ccd2d5fe626343abb90da19850051d192251f26fb582ef632458` |
| `usr/share/libalpm/scripts/limine-mkinitcpio-install` | `5c36e62c0443456c1b72c330493a1f724e7e1413e43d13c57a3bed46ed1b4cd1` |

The source checkout's wrapper and mkinitcpio hook match those MTREE digests. That agreement does not prove which Java source/toolchain produced the native executable. Package BUILDINFO pins PKGBUILD SHA256 `2978466731b561ba0a2de1a61dedbcadb257fe6022c05acd6bb9589b83100f0c`, Gradle `9.6.1-1`, and OpenJDK `26.0.2.u10-1`, but does not identify a GraalVM package. The source README instead recommends GraalVM 25 and `gradle clean nativeCompile`; its build file selects GraalVM native plugin 1.1.3, serial GC, compatibility architecture, 32 MiB maximum heap, size optimization, and no fallback.

Before this can become a runtime candidate, resolve and pin the actual native build toolchain and source mapping, then validate baseline and modified executable behavior with both hashes retained. No Java/native build, runtime modification, benchmark, or Actions job was performed for this audit.
