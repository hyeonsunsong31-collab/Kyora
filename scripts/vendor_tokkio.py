#!/usr/bin/env python3
"""Verify or re-extract the vendored NVIDIA Tokkio 5.0.0-ga tree (standard library only).

Usage
-----
  python scripts/vendor_tokkio.py --verify
      Recompute the sha256 of every file under vendor/tokkio-5.0.0-ga and compare with the
      per-file list in vendor/VENDOR.md. Exit 1 on any mismatch, missing or extra file.

  python scripts/vendor_tokkio.py --from-archive ACE-main.zip [--update-manifest]
      (maintainers) Re-extract ``ACE-main/workflows/tokkio/5.0.0-ga/`` from the NVIDIA ACE
      archive byte-for-byte into vendor/tokkio-5.0.0-ga (the directory is emptied first),
      then verify. With --update-manifest the per-file list, archive sha256 and file count
      in vendor/VENDOR.md are rewritten from the extracted files.

The vendored tree must never be edited by hand; Kyora's additions live in tokkio-kyora/.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO / "vendor" / "tokkio-5.0.0-ga"
MANIFEST = REPO / "vendor" / "VENDOR.md"
ARCHIVE_SUBTREE = "workflows/tokkio/5.0.0-ga/"
HASH_LINE = re.compile(r"^([0-9a-f]{64})  \./(.+)$")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry_hash(path: Path) -> str:
    """sha256 of a vendored entry exactly as the ZIP archive stores it.

    The archive stores six ``config/ace-controller/config.yaml`` entries as
    symlinks (``../../../../src/llm-rag/configs/config.yaml``); a ZIP symlink's
    payload is the link target text. A checked-out symlink is therefore hashed by
    its ``readlink`` text, not by the file it points to. On Windows without
    symlink support git writes the link text into a plain file, which hashes the
    same way.
    """
    if path.is_symlink():
        return hashlib.sha256(os.readlink(path).replace("\\", "/").encode("utf-8")).hexdigest()
    return sha256_file(path)


def hash_tree(root: Path) -> dict[str, str]:
    """``{relative_posix_path: sha256}`` for every file and symlink under ``root`` (see :func:`entry_hash`)."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            out[rel] = entry_hash(p)
    return out


def parse_manifest(text: str) -> dict[str, str]:
    """Return the ``{path: sha256}`` list from the fenced block after the per-file heading."""
    expected: dict[str, str] = {}
    in_block = False
    seen_heading = False
    for line in text.splitlines():
        if line.startswith("## Per-file sha256"):
            seen_heading = True
            continue
        if seen_heading and line.strip().startswith("```"):
            if in_block:
                break
            in_block = True
            continue
        if in_block:
            m = HASH_LINE.match(line.rstrip("\n"))
            if m:
                expected[m.group(2)] = m.group(1)
    return expected


def compare(actual: dict[str, str], expected: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Return ``(mismatched, missing, extra)`` path lists."""
    mismatched = sorted(p for p in expected if p in actual and actual[p] != expected[p])
    missing = sorted(p for p in expected if p not in actual)
    extra = sorted(p for p in actual if p not in expected)
    return mismatched, missing, extra


def verify(vendor_dir: Path = VENDOR_DIR, manifest: Path = MANIFEST, quiet: bool = False) -> int:
    if not manifest.is_file():
        print(f"FAIL: manifest not found: {manifest}")
        return 1
    if not vendor_dir.is_dir():
        print(f"FAIL: vendored tree not found: {vendor_dir}")
        return 1
    expected = parse_manifest(manifest.read_text(encoding="utf-8"))
    if not expected:
        print(f"FAIL: no sha256 lines found in {manifest}")
        return 1
    actual = hash_tree(vendor_dir)
    mismatched, missing, extra = compare(actual, expected)
    for p in mismatched:
        print(f"MISMATCH {p}\n  expected {expected[p]}\n  actual   {actual[p]}")
    for p in missing:
        print(f"MISSING  {p}")
    for p in extra:
        print(f"EXTRA    {p}")
    ok = not (mismatched or missing or extra)
    if not quiet or not ok:
        print(f"{'OK' if ok else 'FAIL'}: {len(actual)} files under {vendor_dir.relative_to(REPO) if vendor_dir.is_relative_to(REPO) else vendor_dir}, "
              f"{len(expected)} listed in {manifest.name}, {len(mismatched)} mismatched, {len(missing)} missing, {len(extra)} extra")
    return 0 if ok else 1


def extract_subtree(archive: Path, dest: Path, subtree: str = ARCHIVE_SUBTREE) -> int:
    """Extract ``<prefix>/<subtree>`` from ``archive`` into ``dest`` byte-for-byte. Returns the file count."""
    if not archive.is_file():
        raise SystemExit(f"archive not found: {archive}")
    dest = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        prefixes = sorted({n[: n.index(subtree) + len(subtree)] for n in names if subtree in n})
        if not prefixes:
            raise SystemExit(f"{subtree} not found in {archive}")
        if len(prefixes) > 1:
            raise SystemExit(f"ambiguous subtree prefixes in archive: {prefixes}")
        prefix = prefixes[0]
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        count = 0
        for info in zf.infolist():
            if not info.filename.startswith(prefix) or info.is_dir():
                continue
            rel = info.filename[len(prefix):]
            if not rel:
                continue
            target = (dest / rel).resolve()
            if dest not in target.parents:  # zip-slip guard
                raise SystemExit(f"refusing to extract outside the vendor dir: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            is_link = ((info.external_attr >> 16) & 0o170000) == 0o120000
            if is_link:
                link_text = zf.read(info).decode("utf-8")
                link_dest = (target.parent / link_text).resolve()
                if dest not in link_dest.parents:
                    raise SystemExit(f"refusing symlink pointing outside the vendor dir: {info.filename} -> {link_text}")
                try:
                    os.symlink(link_text, target)
                except (OSError, NotImplementedError):
                    target.write_text(link_text, encoding="utf-8")  # same bytes as the archive entry
            else:
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
            count += 1
    return count


def update_manifest(archive: Path, vendor_dir: Path = VENDOR_DIR, manifest: Path = MANIFEST) -> None:
    text = manifest.read_text(encoding="utf-8")
    hashes = hash_tree(vendor_dir)
    block = "\n".join(f"{h}  ./{p}" for p, h in sorted(hashes.items()))
    new_text, n = re.subn(r"(## Per-file sha256[^\n]*\n\n```\n)(.*?)(\n```)", lambda m: m.group(1) + block + m.group(3), text,
                          count=1, flags=re.S)
    if n != 1:
        raise SystemExit("could not find the per-file sha256 block in VENDOR.md")
    new_text = re.sub(r"Archive sha256: `[0-9a-f]{64}`", f"Archive sha256: `{sha256_file(archive)}`", new_text)
    new_text = re.sub(r"File count: \d+", f"File count: {len(hashes)}", new_text)
    manifest.write_text(new_text, encoding="utf-8")
    print(f"updated {manifest} ({len(hashes)} files)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true", help="verify vendor/tokkio-5.0.0-ga against vendor/VENDOR.md")
    ap.add_argument("--from-archive", metavar="ZIP", help="re-extract the subtree from the NVIDIA ACE archive")
    ap.add_argument("--update-manifest", action="store_true", help="with --from-archive: rewrite the sha256 list in VENDOR.md")
    ap.add_argument("--vendor-dir", default=str(VENDOR_DIR), help=argparse.SUPPRESS)
    ap.add_argument("--manifest", default=str(MANIFEST), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    vendor_dir = Path(args.vendor_dir)
    manifest = Path(args.manifest)
    if not args.verify and not args.from_archive:
        ap.print_help()
        return 2
    if args.from_archive:
        archive = Path(args.from_archive)
        n = extract_subtree(archive, vendor_dir)
        print(f"extracted {n} files from {archive} into {vendor_dir}")
        if args.update_manifest:
            update_manifest(archive, vendor_dir, manifest)
    return verify(vendor_dir, manifest)


if __name__ == "__main__":
    sys.exit(main())
