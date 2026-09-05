# Reproduce the prebuilt root installer using one official package snapshot

These tools prepare an experimental live overlay of [omacom/omarchy-iso PR #145](https://github.com/omacom/omarchy-iso/pull/145), pinned to `dbffaa6c65344d644627a023c28661e08382b8fa`. The upstream contributors created the prebuilt Btrfs/qcow2 installer, parallel finalization and LUKS mapper reuse. These scripts adapt its build to a disposable VM and constrained disk space; they do not establish a speedup without the paired installation runs.

The baseline and candidate both boot the verified official Omarchy 4.0.2 ISO. Every target package comes from that ISO's offline repository. The candidate restores the common filesystem from a supplementary read-only ISO and uses the pinned upstream orchestration. This is a recorded experimental fixture, not a production ISO release. Keep the secondary CD drive, cache policy, storage backend and source-media accounting visible in the run manifest. For a strict media comparison, attach the corresponding supplementary drive to the baseline too. A later production comparison must embed the image and installer into a complete ISO and measure slow physical install media.

## Build inputs

Capture a successfully booted baseline's `pacman -Q` as `package-manifest.txt` and `pacman -Qqe` as `package-explicit.txt`. Package reasons matter: a one-transaction image build can otherwise turn some dependencies into explicit packages. The builder requires all image packages to be the same versions as the baseline and normalizes their reasons to the baseline. Final candidate validation must still require full inventory and reason equality, because hardware packages are added after restoration.

Prepare the small reproducible bundles on the host:

```bash
python3 test/benchmarks/install-speed/image/prepare-bundles.py \
  /tmp/omarchy-iso-fast /tmp/omarchy-bench/image-bundles
```

The source checkout must be clean and exactly the pin above. The builder bundle contains the pinned `archinstall.packages` and `image.packages` lists. The larger base list and runtime package names are read inside the guest from the official ISO, so a newer Omarchy checkout cannot change the experiment's package snapshot. Package archives are not copied into the image cache, and no online repository is configured.

The installer bundle applies one recorded correction to the pinned `phases_impl.py`. The original restore path creates empty Btrfs child subvolumes and copies only `pacman.log` into `@log`; mounting them hides the image's other files and directory metadata. An actual fresh candidate boot exposed missing package-owned `/var/log/cups/` and `/var/log/old/`, despite their presence in the validated source image. The correction seeds every image-backed Btrfs child mount from its corresponding image subtree before replaying the mount table. This preserves home defaults, log directories, cache directory metadata, existing files, owners, modes and xattrs without hardcoding package names. The root mount and independent filesystems such as the ESP retain their existing handling. Image cleanup and the sealed root image remain unchanged.

`root_image_mounts.py` checks the exact upstream source hash and expected replacement site before producing the patched module. The installer bundle manifest records both upstream and patched hashes and describes the correction. All copy paths are validated before the first write; genuinely absent image directories remain empty, while symlink traversal, overlapping/self-copies, ambiguous layouts and copy failures abort restoration. Source subvolume roots, nested Btrfs subvolumes and snapshot stubs also abort: archive copying cannot preserve their subvolume identity and properties. These are detected through Btrfs's reserved directory inode numbers, [256 and 2](https://btrfs.readthedocs.io/en/latest/Subvolumes.html). The standard home, log and package-cache source directories are ordinary directories. The focused test executes the actual injected function and real archive copies:

```bash
python3 test/benchmarks/install-speed/image/root-image-mounts-test.py \
  --iso-source /tmp/omarchy-iso-fast
```

The failed installed system must remain a failed sample. Validate this correction on a new target with complete package-file checks and both boot gates; repairing directories after installation cannot establish success.

## Guest filesystem build

Create a fresh sparse raw disk outside the synced workspace and attach it to a disposable official-ISO live guest:

```bash
qemu-img create -f raw /tmp/omarchy-bench/root-build.raw 24G
```

Relevant QEMU drive options are `format=raw,cache=none,discard=unmap,detect-zeroes=unmap`; the virtio device must have `serial=OMARCHY_IMAGE_BUILD`. The builder refuses a missing serial, existing filesystem signatures, a mounted device, an undersized disk, or a non-QEMU environment. Do not attach any real user disks. The guest must be in the official live environment with its offline mirror; the installed baseline lacks some live build tools.

Transfer and extract the small builder bundle and baseline manifests, verify the bundle's `.sha256`, then run inside the guest:

```bash
bash /run/image-builder/build-root-from-iso.sh \
  /run/image-builder /run/baseline-manifests /run/image-build-output
```

The default Btrfs compression is upstream's `compress-force=zstd:15`. An experimental override uses `OMARCHY_IMAGE_COMPRESSION`; record it and compare image size and restore performance. Build time is outside install time because this work belongs to producing the reusable installation medium, just as producing the official ISO does.

The builder runs pacstrap and its normal non-boot package hooks. It masks only upstream's five deferred boot hooks, restores the masks afterward, strips machine ID, pacman keyring, SSH host keys and build resolver state, checks package files, seals the source subvolume read-only, shrinks Btrfs and checks every data checksum. Required per-machine identities, keyring population, UKI creation and configuration remain the installer's responsibility.

When the builder guest is managed by `iso-vm.py --mode builder`, the host driver transfers baseline manifests, launches the build as a systemd unit, preserves progress and failure logs, and retrieves the small completed evidence automatically:

```bash
python3 test/benchmarks/install-speed/image/drive-guest-build.py \
  /tmp/omarchy-bench/builder-01 /tmp/omarchy-bench/baseline-03 \
  /tmp/omarchy-bench/image-build-output
```

Its default timeout is four hours; set the supervisor's timeout at least as long. A timeout or failed guest unit is a failed build, and must not lead to native compression. This driver expects the builder scripts at `/usr/local/lib/omarchy-benchmark/image-builder`, as supplied by the builder initramfs payload. It leaves the VM running so failures can be inspected.

Copy `/run/image-build-output` to the host, then shut the builder VM down completely. Compress with the native host binary, avoiding expensive software-emulated compression:

```bash
python3 test/benchmarks/install-speed/image/compress-root-image.py \
  /tmp/omarchy-bench/root-build.raw /tmp/omarchy-bench/image-build-output \
  /tmp/omarchy-bench/image-media/arch/x86_64/omarchy-root.btrfs.qcow2
```

`qemu-img resize` checks QEMU's image write lock before shrinking the raw backing file; it must fail if the build VM still owns the image. Compression uses upstream's 1 MiB zstd qcow2 clusters. The tool checks qcow2 structure, compares its entire logical content against the validated raw filesystem and writes a SHA-256 companion and provenance JSON. Do not delete the raw file until this succeeds.

## Read-only candidate media and activation

Put `installer-overlay.tar`, its `.sha256` and `.manifest.json` at the image-media root. Include the completed image and companion checksum at `arch/x86_64/`. Build a supplementary ISO without copying the large files to another staging tree:

```bash
xorriso -as mkisofs -r -J -V OMARCHY_FAST_IMAGE \
  -o /tmp/omarchy-bench/fast-image.iso /tmp/omarchy-bench/image-media
```

Before reclaiming the loose image's disk space, verify the actual bytes embedded in the ISO against the validated root-image manifest:

```bash
python3 test/benchmarks/install-speed/image/verify-image-media.py \
  /tmp/omarchy-bench/fast-image.iso \
  /tmp/omarchy-bench/image-media/arch/x86_64/omarchy-root.btrfs.qcow2.json \
  /tmp/omarchy-bench/image-media-verification.json
```

The tool uses xorriso's reported file extents, hashes every embedded byte and also records the whole-ISO digest. It supports multiple extents without extracting a second large copy. A failed comparison must leave the loose validated image available for rebuilding the ISO.

Record the ISO digest. If the original live ISO lacks `qemu-img`, `bundle-qemu-img.py` prepares a live-only binary with its exact ELF loader and linked libraries. It records every input digest and tests a truly zstd-compressed qcow2, including conversion, integrity checking and content comparison using only its bundled loader/libraries. Example for the extracted Ubuntu host toolchain:

```bash
python3 test/benchmarks/install-speed/image/bundle-qemu-img.py \
  /tmp/omarchy-bench/toolchain/usr/bin/qemu-img \
  /tmp/omarchy-bench/image-media/qemu-img-live.tar \
  --library-path /tmp/omarchy-bench/toolchain/usr/lib/x86_64-linux-gnu
```

Include that tar and its checksum/manifest on the supplementary ISO. Activation verifies and extracts it only if `qemu-img` is missing. No package is added to the target to satisfy this live tooling dependency.

Before the candidate's configurator or installer starts, mount the supplementary ISO read-only and run:

```bash
mount -o ro /dev/disk/by-label/OMARCHY_FAST_IMAGE /run/fast-image
bash /run/activate-installer-overlay.sh /run/fast-image
```

Activation verifies the small source archive, applies pinned installer files to the live root overlay and bind-mounts the read-only image directory at upstream's original path. It starts the upstream image-verification service asynchronously. The upstream preparation phase waits for this full SHA-256 verification before formatting a disk. The script does not select a disk or start the installer; the benchmark supervisor supplies the same fresh target/configuration as the baseline. Start the primary host clock at VM boot, not after overlay activation or image verification. Preserve installation-only and boot-to-success timing separately.

For unattended runs, use the benchmark's appended-initramfs hook and `candidate-preflight.sh`. The payload tree must place `activate-installer-overlay.sh` at `/usr/local/lib/omarchy-benchmark/activate-installer-overlay.sh`. The preflight mounts `OMARCHY_FAST_IMAGE` read-only and runs activation before the original `.automated_script.sh`. Apply the same initramfs mechanism with an empty preflight to the baseline so boot plumbing is matched. Builder mode suppresses automatic installation and can enable temporary root SSH to invoke the builder on its separate empty disk.

The fixture retains the official ISO's complete offline mirror. Candidate image assembly and live overlay activation are additional recorded inputs; there is no claim that this reduces ISO download size. Do not infer normal hardware performance from software-emulated QEMU. Validate installed-system boot, every phase's success, package versions and reasons, package-file checks, Btrfs subvolume layout, UKI, unique machine identity and SSH/pacman keys before accepting a timing sample.
