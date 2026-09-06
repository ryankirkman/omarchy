"""Seed separate Btrfs mounts from the validated image before replaying mounts."""

import hashlib
import inspect
from pathlib import Path
import subprocess


SOURCE_SHA256 = "4088b7e930d2da7729f69c4506483d8e9c661a0488de913255c868f1154de977"
OLD_BLOCK = '''        # The image's pacman.log ends up under the @log mount; carry it over so
        # the installed system's log starts with the packages it was built from.
        image_log = installed_root / "var" / "log" / "pacman.log"
        log_subvol = top / "@log"
        if image_log.is_file() and log_subvol.is_dir():
            shutil.copy2(image_log, log_subvol / "pacman.log")
'''
NEW_BLOCK = '''        # Separate mounts must retain the image's files and directory metadata;
        # mounting empty subvolumes would hide package-owned paths such as logs.
        initialize_image_mount_contents(installed_root, top, target, mounts, device)
'''


def initialize_image_mount_contents(installed_root, top, target, mounts, device):
    """Preserve image contents hidden by separate Btrfs child mounts.

    Validate all planned copies before starting one. Missing image directories
    need no seeding; a symlink or an invalid layout must never mean "missing".
    Only the restored filesystem's child mounts are image-backed; root itself
    and independent filesystems such as the ESP remain handled by their phases.
    """
    installed_root, top, target = map(Path, (installed_root, top, target))

    def directory(base, relative=Path("."), required=True):
        path = base / relative
        if not path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"Unsafe image mount path: {path}")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise RuntimeError(f"Image mount path traverses a symlink: {current}")
            if not current.exists():
                if required:
                    raise RuntimeError(f"Image mount directory is absent: {current}")
                return None
            if not current.is_dir():
                raise RuntimeError(f"Image mount path is not a directory: {current}")
        return path

    directory(installed_root)
    directory(top)
    if not target.is_absolute() or ".." in target.parts:
        raise RuntimeError(f"Unsafe installation target: {target}")
    copies, destinations, mountpoints = [], set(), set()
    for mount in mounts:
        source_device = (mount.get("source") or "").split("[")[0]
        if mount.get("fstype") != "btrfs" or source_device != device:
            continue
        mountpoint = Path(mount["target"])
        if not mountpoint.is_absolute() or ".." in mountpoint.parts:
            raise RuntimeError(f"Unsafe image-backed mountpoint: {mountpoint}")
        try:
            relative = mountpoint.relative_to(target)
        except ValueError as error:
            raise RuntimeError(f"Image-backed mount is outside the installation target: {mountpoint}") from error
        if relative == Path("."):
            continue
        names = [value.split("=", 1)[1] for value in (mount.get("options") or "").split(",")
                 if value.startswith("subvol=")]
        if len(names) != 1:
            raise RuntimeError(f"Missing or ambiguous image-backed subvolume: {mountpoint}")
        name = Path(names[0].lstrip("/"))
        if name == Path(".") or ".." in name.parts:
            raise RuntimeError(f"Unsafe image-backed subvolume: {names[0]}")
        destination = directory(top, name)
        if (destination == installed_root or installed_root in destination.parents
                or destination in installed_root.parents):
            raise RuntimeError(f"Image-backed destination overlaps the image root: {destination}")
        if destination in destinations or mountpoint in mountpoints:
            raise RuntimeError(f"Duplicate image-backed mount mapping: {mountpoint}")
        destinations.add(destination)
        mountpoints.add(mountpoint)
        source = directory(installed_root, relative, required=False)
        if source is None:
            continue
        if (source == destination or source in destination.parents or destination in source.parents
                or source.samefile(destination)):
            raise RuntimeError(f"Image-backed copy overlaps its source: {source} -> {destination}")
        # This source is the restored Btrfs filesystem. Its subvolume roots
        # have inode 256; snapshot stubs have inode 2. Archive copying either
        # would silently flatten subvolume identity and properties.
        # https://btrfs.readthedocs.io/en/latest/Subvolumes.html
        if source.lstat().st_ino in (2, 256):
            raise RuntimeError(f"Image-backed source is a Btrfs subvolume: {source}")
        # cp may follow an existing destination file symlink when replacing
        # its contents. Reject every such collision, and all symlink parents,
        # before any archive copy starts. Source symlinks themselves are kept.
        for entry in source.rglob("*"):
            if not entry.is_symlink() and entry.is_dir() and entry.lstat().st_ino in (2, 256):
                raise RuntimeError(f"Image-backed source contains a Btrfs subvolume: {entry}")
            relative_entry = entry.relative_to(source)
            parent = directory(destination, relative_entry.parent, required=False)
            if parent is not None and (destination / relative_entry).is_symlink():
                raise RuntimeError(f"Image-backed copy would collide with a destination symlink: {destination / relative_entry}")
        copies.append((source, destination))
    for source, destination in copies:
        # Archive mode preserves empty directories, symlinks within their tree,
        # uid/gid, modes (including setgid), timestamps, and xattrs. The checked
        # exit status prevents a partial copy from becoming a successful image.
        subprocess.run(["cp", "-a", "--", str(source) + "/.", str(destination) + "/"],
                       check=True, capture_output=True)


def patch_source(source):
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("Root-image mount initialization requires the exact pinned phases_impl.py")
    text = source.decode()
    anchor = "def _findmnt_mounts(root: Path) -> list[dict]:\n"
    if text.count(OLD_BLOCK) != 1 or text.count(anchor) != 1:
        raise ValueError("Unexpected pinned image-restore source structure")
    patched = text.replace(OLD_BLOCK, NEW_BLOCK).replace(
        anchor, inspect.getsource(initialize_image_mount_contents) + "\n\n" + anchor)
    compile(patched, "patched-phases_impl.py", "exec")
    return patched.encode()
