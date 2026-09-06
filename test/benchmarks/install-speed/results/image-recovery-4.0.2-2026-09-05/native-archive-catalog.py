#!/usr/bin/env python3
"""Read package metadata directly from the pinned ISO, without unpacking payloads."""
import argparse
import concurrent.futures
import ctypes as C
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

HERE = Path(__file__).resolve().parent
ISO = Path('/tmp/omarchy-bench/downloads/omarchy-4.0.2.iso')
ISO_SHA256 = '2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8'
OFFSET = 132242 * 2048
UNSQUASHFS = '/tmp/omarchy-bench/toolchain/usr/bin/unsquashfs'
BASELINE = Path('/tmp/omarchy-bench/baseline-03/package-manifest.txt')
ENV = dict(os.environ, LD_LIBRARY_PATH='/tmp/omarchy-bench/toolchain/lib/x86_64-linux-gnu:/tmp/omarchy-bench/toolchain/usr/lib/x86_64-linux-gnu')
LIB = C.CDLL('libarchive.so.13')

def api(name, result, *args):
    f = getattr(LIB, name)
    f.restype = result
    f.argtypes = list(args)
    return f

new = api('archive_read_new', C.c_void_p)
filters = api('archive_read_support_filter_all', C.c_int, C.c_void_p)
tar = api('archive_read_support_format_tar', C.c_int, C.c_void_p)
mtree = api('archive_read_support_format_mtree', C.c_int, C.c_void_p)
open_fd = api('archive_read_open_fd', C.c_int, C.c_void_p, C.c_int, C.c_size_t)
open_memory = api('archive_read_open_memory', C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)
next_header = api('archive_read_next_header', C.c_int, C.c_void_p, C.POINTER(C.c_void_p))
read_data = api('archive_read_data', C.c_ssize_t, C.c_void_p, C.c_void_p, C.c_size_t)
free = api('archive_read_free', C.c_int, C.c_void_p)
error = api('archive_error_string', C.c_char_p, C.c_void_p)
pathname = api('archive_entry_pathname', C.c_char_p, C.c_void_p)
filetype = api('archive_entry_filetype', C.c_uint, C.c_void_p)
size = api('archive_entry_size', C.c_int64, C.c_void_p)
version_string = api('archive_version_string', C.c_char_p)

def check(status, reader):
    if status != 0:
        raise RuntimeError(f'libarchive status {status}: {error(reader)!r}')

def read_member(reader, entry):
    length = size(entry)
    if length < 0 or length > 64 * 1024 * 1024:
        raise ValueError(f'unexpected metadata length {length}')
    chunks = []
    buf = C.create_string_buffer(65536)
    while True:
        n = read_data(reader, buf, len(buf))
        if n < 0:
            raise RuntimeError(f'metadata read failed: {error(reader)!r}')
        if n == 0:
            break
        chunks.append(buf.raw[:n])
    value = b''.join(chunks)
    if len(value) != length:
        raise ValueError('metadata member length mismatch')
    return value

def read_leading_metadata(archive_path):
    cmd = [UNSQUASHFS, '-offset', str(OFFSET), '-processors', '1',
           '-data-queue', '1', '-frag-queue', '1', '-no-wildcards', '-cat',
           str(ISO), archive_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    reader = new()
    entry = C.c_void_p()
    metadata = {}
    stopped_at_payload = False
    try:
        check(filters(reader), reader)
        check(tar(reader), reader)
        check(open_fd(reader, proc.stdout.fileno(), 65536), reader)
        while True:
            status = next_header(reader, C.byref(entry))
            if status == 1:
                break
            check(status, reader)
            name = pathname(entry).decode('utf-8')
            normalized = name[2:] if name.startswith('./') else name
            if normalized in {'.PKGINFO', '.BUILDINFO', '.MTREE', '.INSTALL'}:
                if normalized in metadata:
                    raise ValueError('duplicate metadata member')
                metadata[normalized] = read_member(reader, entry)
            elif normalized not in {'.', ''}:
                stopped_at_payload = True
                break
        if not {'.PKGINFO', '.MTREE'} <= metadata.keys():
            raise ValueError(f'archive lacks required leading metadata: {archive_path}')
    finally:
        free(reader)
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
    if not stopped_at_payload and proc.returncode != 0:
        raise RuntimeError(f'unsquashfs failed: {stderr.decode(errors="replace")}')
    return metadata

def package_info(raw):
    result = {}
    for line in raw.decode('utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition(' = ')
        if not separator:
            raise ValueError(f'malformed PKGINFO line: {line!r}')
        result.setdefault(key, []).append(value)
    return result

def mtree_files(raw):
    reader = new()
    buffer = C.create_string_buffer(raw)
    entry = C.c_void_p()
    result = []
    try:
        check(filters(reader), reader)
        check(mtree(reader), reader)
        check(open_memory(reader, buffer, len(raw)), reader)
        while True:
            status = next_header(reader, C.byref(entry))
            if status == 1:
                break
            check(status, reader)
            name = pathname(entry).decode('utf-8')
            name = name[2:] if name.startswith('./') else name
            if name in {'.', '', '.PKGINFO', '.BUILDINFO', '.MTREE', '.INSTALL'}:
                continue
            if name.startswith('/') or '..' in name.split('/'):
                raise ValueError(f'unsafe archive path {name!r}')
            if filetype(entry) == 0o040000 and not name.endswith('/'):
                name += '/'
            result.append(name)
    finally:
        free(reader)
    if len(result) != len(set(result)):
        raise ValueError('duplicate mtree package paths')
    return sorted(result)

def extract_one(task):
    name, wanted_version, archive_path = task
    started = time.monotonic()
    metadata = read_leading_metadata(archive_path)
    info = package_info(metadata['.PKGINFO'])
    if info.get('pkgname') != [name] or info.get('pkgver') != [wanted_version]:
        raise ValueError(f'wrong archive identity {name}: {info.get("pkgname")} {info.get("pkgver")}')
    files = mtree_files(metadata['.MTREE'])
    out = HERE / 'packages' / name
    out.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for label, value in metadata.items():
        filename = label[1:]
        (out / filename).write_bytes(value)
        hashes[filename] = {'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
    mtree_text = gzip.decompress(metadata['.MTREE']) if metadata['.MTREE'].startswith(b'\x1f\x8b') else metadata['.MTREE']
    (out / 'MTREE.txt').write_bytes(mtree_text)
    (out / 'files.txt').write_text(''.join(path + '\n' for path in files))
    result = {'name': name, 'version': wanted_version, 'source_archive_path': archive_path,
              'pkginfo': info, 'metadata': hashes, 'file_count': len(files),
              'file_list_sha256': hashlib.sha256((out / 'files.txt').read_bytes()).hexdigest(),
              'install_scriptlet_present': '.INSTALL' in metadata,
              'elapsed_seconds': time.monotonic() - started}
    (out / 'metadata.json').write_text(json.dumps(result, indent=2) + '\n')
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    inventories = [line.split(' ', 1) for line in BASELINE.read_text().splitlines() if line]
    if len(inventories) != 941 or len({name for name, _ in inventories}) != 941:
        raise ValueError('expected exact 941 baseline packages')
    sources = [line.removeprefix('squashfs-root/') for line in (HERE / 'squashfs-package-list.txt').read_text().splitlines()]
    tasks = []
    for name, wanted_version in inventories:
        version_no_epoch = wanted_version.split(':', 1)[-1]
        prefixes = tuple(f'var/cache/omarchy/mirror/offline/{name}-{version}-' for version in {version_no_epoch, wanted_version})
        matches = [source for source in sources if source.startswith(prefixes) and re.search(r'\.pkg\.tar\.(zst|xz|gz)$', source)]
        if len(matches) != 1:
            raise ValueError(f'expected unique archive for {name} {wanted_version}: {matches}')
        tasks.append((name, wanted_version, matches[0]))
    if args.limit:
        tasks = tasks[:args.limit]
    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(extract_one, task): task for task in tasks}
        for future in concurrent.futures.as_completed(pending):
            result = future.result()
            results.append(result)
            if len(results) % 25 == 0 or len(results) == len(tasks):
                progress = {'complete': len(results), 'total': len(tasks), 'elapsed_seconds': time.monotonic() - started, 'last_package': result['name']}
                (HERE / 'progress.json').write_text(json.dumps(progress, indent=2) + '\n')
                print(json.dumps(progress), flush=True)
    manifest = {'schema_version': 1, 'complete': len(results) == 941,
                'source_iso': str(ISO), 'source_iso_sha256': ISO_SHA256,
                'source_iso_hash_provenance': 'Existing independently verified official release ISO pin; not rehashed by this metadata-only cataloger',
                'squashfs_extent_bytes': OFFSET, 'squashfs_bytes': 5915328512,
                'squashfs_extents_contiguous': True,
                'squashfs_extent_lba': [[132242, 2097151], [2229393, 791193]],
                'squashfs_tools_deb_sha256': '87fae263846bab255d4a51ad9fc623685497ad830db60758dde39589c9fdadcb',
                'liblzo2_deb_sha256': 'e0d13be155013138b8db4cfe68212b866080af661c78302c2eab0d2f9d0d454e',
                'libarchive_version': version_string().decode(),
                'baseline_manifest_sha256': hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
                'catalog_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'method': 'unsquashfs -cat at contiguous ISO offset; read leading archive metadata with libarchive; file lists from package MTREE, not full payload scan; payload bodies not validated by catalog',
                'elapsed_seconds': time.monotonic() - started,
                'packages': sorted(results, key=lambda item: item['name'])}
    (HERE / ('catalog.json' if manifest['complete'] else 'catalog-smoke.json')).write_text(json.dumps(manifest, indent=2) + '\n')

if __name__ == '__main__':
    main()
