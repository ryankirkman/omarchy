#!/usr/bin/env python3
"""Test the actual pinned image-restoration patch without mounting or disks."""

import argparse
import ast
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch as mock_patch


def metadata(path):
    value = path.lstat()
    return (stat.S_IMODE(value.st_mode), value.st_uid, value.st_gid,
            {name: os.getxattr(path, name) for name in os.listxattr(path)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso-source", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("root_image_mounts", Path(__file__).with_name("root_image_mounts.py"))
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    source = subprocess.check_output(["git", "-C", str(args.iso_source), "show",
        "dbffaa6c65344d644627a023c28661e08382b8fa:configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py"])
    prepared = patch.patch_source(source)
    try:
        patch.patch_source(source + b"\n")
    except ValueError:
        pass
    else:
        raise AssertionError("Changed upstream source accepted")
    tree = ast.parse(prepared)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "initialize_image_mount_contents")
    namespace = {"Path": Path, "subprocess": subprocess}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "actual-patched-function", "exec"), namespace)
    initialize = namespace["initialize_image_mount_contents"]
    assert prepared.count(patch.NEW_BLOCK.encode()) == 1 and patch.OLD_BLOCK.encode() not in prepared

    def mount(target, name, device="/dev/vda2", fstype="btrfs"):
        return {"target": target, "source": device + "[/" + name + "]", "fstype": fstype,
                "options": "rw,subvol=/" + name}

    with tempfile.TemporaryDirectory(prefix="omarchy-image-mounts-", dir="/tmp") as temporary:
        top = Path(temporary) / "top"
        root = top / "@"
        for relative in ("home/guest", "var/log/cups", "var/log/old", "var/log/journal", "var/cache/pacman/pkg", "boot"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        for name in ("@home", "@log", "@pkg", "@optional"):
            (top / name).mkdir()
        (root / "home/guest/config").write_text("source home data\n")
        (root / "home/guest/link").symlink_to("config")
        (root / "var/log/pacman.log").write_text("source package history\n")
        (root / "boot/stale-image-file").write_text("independent ESP must not be copied\n")
        (top / "@log/retained").write_text("existing destination data\n")
        (top / "@log/pacman.log").write_text("older destination version\n")
        for relative, mode in (("home", 0o751), ("var/log", 0o750), ("var/log/cups", 0o750),
                               ("var/log/journal", 0o2755), ("var/cache/pacman/pkg", 0o711)):
            (root / relative).chmod(mode)
        os.setxattr(root / "var/log", "user.omarchy-fixture", b"directory metadata")
        os.setxattr(root / "home/guest/config", "user.omarchy-fixture", b"file metadata")
        mounts = [mount("/mnt", "@"), mount("/mnt/home", "@home"), mount("/mnt/var/log", "@log"),
                  mount("/mnt/var/cache/pacman/pkg", "@pkg"), mount("/mnt/opt/optional", "@optional"),
                  mount("/mnt/boot", "esp", device="/dev/vda1", fstype="vfat")]
        initialize(root, top, Path("/mnt"), mounts, "/dev/vda2")
        for relative, name in (("home", "@home"), ("var/log", "@log"), ("var/cache/pacman/pkg", "@pkg")):
            assert metadata(root / relative) == metadata(top / name), (relative, name)
        for relative in ("cups", "old", "journal"):
            assert metadata(root / "var/log" / relative) == metadata(top / "@log" / relative)
        assert (top / "@home/guest/config").read_text() == "source home data\n"
        assert metadata(root / "home/guest/config") == metadata(top / "@home/guest/config")
        assert (top / "@home/guest/link").is_symlink()
        assert (top / "@home/guest/link").readlink() == Path("config")
        assert (top / "@log/pacman.log").read_text() == "source package history\n"
        assert (top / "@log/retained").read_text() == "existing destination data\n"
        assert not list((top / "@optional").iterdir())
        assert not (top / "esp").exists()
        assert (root / "var/log/cups").is_dir() and (root / "var/log/old").is_dir()
        print("ok - all image-backed child mounts retain files, empty directories, uid/gid, modes and xattrs")
        print("ok - existing destination data survives; genuinely absent source and independent ESP are skipped")

        calls = []
        namespace["subprocess"] = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
        real_lstat = Path.lstat
        for subvolume in (root / "var/log", root / "var/log/cups"):
            for inode in (2, 256):
                def btrfs_lstat(path, **kwargs):
                    value = real_lstat(path, **kwargs)
                    if path == subvolume:
                        parts = list(value)
                        parts[1] = inode
                        return os.stat_result(parts)
                    return value
                with mock_patch.object(Path, "lstat", btrfs_lstat):
                    try:
                        initialize(root, top, Path("/mnt"), [mounts[2]], "/dev/vda2")
                    except RuntimeError as error:
                        assert "Btrfs subvolume" in str(error) and not calls
                    else:
                        raise AssertionError(f"Btrfs source subvolume was flattened: {subvolume}, inode {inode}")
        print("ok - source subvolume roots, nested subvolumes and snapshot stubs fail before copying")
        # A destination-file symlink must not redirect an archive overwrite.
        sentinel = Path(temporary) / "outside-file"
        sentinel.write_text("must remain unchanged\n")
        (top / "@log/pacman.log").unlink()
        (top / "@log/pacman.log").symlink_to(sentinel)
        try:
            initialize(root, top, Path("/mnt"), [mounts[2]], "/dev/vda2")
        except RuntimeError:
            assert not calls and sentinel.read_text() == "must remain unchanged\n"
        else:
            raise AssertionError("Destination-file symlink was accepted")
        (top / "@log/pacman.log").unlink()
        (top / "@log/pacman.log").write_text("source package history\n")
        # Remove the already copied source symlink for the remaining validation
        # cases, which deliberately test new restore plans against existing data.
        (top / "@home/guest/link").unlink()
        bad_layouts = [
            [mount("/outside/home", "@home")],
            [mount("/mnt/../outside", "@home")],
            [mount("/mnt/home", "../escape")],
            [mount("/mnt/home", "@")],
            [mount("/mnt/home", "@/home")],
            [mount("/mnt/home", "@home"), mount("/mnt/var/log", "@home")],
            [{**mount("/mnt/home", "@home"), "options": "subvol=/@home,subvol=/@log"}],
            [{**mount("/mnt/home", "@home"), "options": "rw"}],
            [mount("/mnt/home", "@missing")],
        ]
        for bad in bad_layouts:
            calls.clear()
            try:
                initialize(root, top, Path("/mnt"), [mounts[2], *bad], "/dev/vda2")
            except RuntimeError:
                assert not calls, "Layout validation occurred after a copy started"
            else:
                raise AssertionError(f"Unsafe layout accepted: {bad}")
        outside = Path(temporary) / "outside"
        outside.mkdir()
        for link, mapping in ((root / "linked", mount("/mnt/linked/child", "@home")),
                              (root / "broken", mount("/mnt/broken", "@home")),
                              (top / "@linked", mount("/mnt/home", "@linked"))):
            link.symlink_to(outside if link.name != "broken" else outside / "absent")
            calls.clear()
            try:
                initialize(root, top, Path("/mnt"), [mapping], "/dev/vda2")
            except RuntimeError:
                assert not calls
            else:
                raise AssertionError(f"Symlink traversal accepted: {link}")
        print("ok - root/self-copy, unsafe paths, symlink traversal, ambiguous mappings and missing destinations fail before copying")

        def failed_copy(argv, **options):
            assert options["check"] is True and argv[:3] == ["cp", "-a", "--"]
            raise subprocess.CalledProcessError(1, argv, stderr=b"simulated archive-copy failure")
        namespace["subprocess"] = SimpleNamespace(run=failed_copy)
        try:
            initialize(root, top, Path("/mnt"), [mounts[2]], "/dev/vda2")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Failed archive copy did not abort restoration")
        print("ok - archive-copy failure propagates; source pin and actual injected function validated")


if __name__ == "__main__":
    main()
