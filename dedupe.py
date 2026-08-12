
import argparse
import hashlib
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime


DEFAULT_SKIP_DIRS = {
    "venv", ".venv", "env", "node_modules", ".git", ".gradle", ".godot",
    "__pycache__", ".idea", ".vs", ".vscode", "dist", "build", ".next",
    ".cache", ".mypy_cache", ".pytest_cache",
}


def human_readable_size(num_bytes):
    """Convert a byte count into something like '4.2 MB' for display."""
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def hash_file(filepath, chunk_size=65536):
    """
    Compute a SHA-256 hash of a file's contents.
    Reads in chunks so this works fine even on very large files
    without loading the whole thing into memory.
    """
    hasher = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hasher.update(chunk)
    except (OSError, PermissionError) as e:
        print(f"  ! Skipping (couldn't read): {filepath} ({e})")
        return None
    return hasher.hexdigest()


def find_duplicates(root_path, min_size=0, skip_dirs=None):
    """
    Walk the directory tree under root_path and group files by content.

    Returns a dict: { hash: [list of file paths with that hash] }
    Only hashes files that share a size with at least one other file,
    since a unique file size guarantees a unique file.

    skip_dirs: a set of folder names (not full paths) to skip entirely,
    e.g. {"venv", ".git", "node_modules"} — anything inside a folder
    with one of these names is never scanned.
    """
    skip_dirs = skip_dirs or set()
    print(f"Scanning: {root_path}")
    if skip_dirs:
        print(f"Skipping folders named: {', '.join(sorted(skip_dirs))}")
    print("Step 1/2: grouping files by size...")

    size_groups = defaultdict(list)
    total_files_seen = 0
    skipped_dir_count = 0

    for dirpath, dirnames, filenames in os.walk(root_path):
        # prune skipped folders in place so os.walk never descends into them
        before = len(dirnames)
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        skipped_dir_count += before - len(dirnames)

        for name in filenames:
            filepath = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(filepath)
            except OSError:
                continue
            if size < min_size:
                continue
            total_files_seen += 1
            size_groups[size].append(filepath)

    # Only files sharing a size with at least one other file could be duplicates
    candidates = [group for group in size_groups.values() if len(group) > 1]
    candidate_count = sum(len(g) for g in candidates)

    print(f"  Found {total_files_seen} files total.")
    if skipped_dir_count:
        print(f"  Skipped {skipped_dir_count} folder(s) matching the skip list.")
    print(f"  {candidate_count} share a size with another file and need checking.")
    print("Step 2/2: comparing file contents (hashing)...")

    hash_groups = defaultdict(list)
    checked = 0
    for group in candidates:
        for filepath in group:
            checked += 1
            if checked % 50 == 0 or checked == candidate_count:
                print(f"  ...checked {checked}/{candidate_count}", end="\r")
            file_hash = hash_file(filepath)
            if file_hash:
                hash_groups[file_hash].append(filepath)
    print()  # move past the progress line

    duplicate_groups = {h: paths for h, paths in hash_groups.items() if len(paths) > 1}
    return duplicate_groups


def report(duplicate_groups):
    """Print a summary of duplicate groups found and total reclaimable space."""
    if not duplicate_groups:
        print("\nNo duplicates found. Your files are already unique.")
        return 0

    total_reclaimable = 0
    group_num = 0

    print("\n" + "=" * 60)
    print("DUPLICATES FOUND")
    print("=" * 60)

    for file_hash, paths in duplicate_groups.items():
        group_num += 1
        file_size = os.path.getsize(paths[0])
        # every file after the first one in a group is "extra" / reclaimable
        reclaimable = file_size * (len(paths) - 1)
        total_reclaimable += reclaimable

        print(f"\nGroup {group_num} — {len(paths)} copies, {human_readable_size(file_size)} each:")
        for i, p in enumerate(paths):
            tag = "(keep)" if i == 0 else "(extra)"
            print(f"  {tag} {p}")

    print("\n" + "-" * 60)
    print(f"Total duplicate groups: {len(duplicate_groups)}")
    print(f"Space you could reclaim: {human_readable_size(total_reclaimable)}")
    print("-" * 60)
    return total_reclaimable


def quarantine_duplicates(duplicate_groups, quarantine_dir):
    """
    Move all but the first file in each duplicate group into quarantine_dir.
    The first file found in each group is always kept in place.
    """
    os.makedirs(quarantine_dir, exist_ok=True)
    moved_count = 0

    for paths in duplicate_groups.values():
        # keep the first, quarantine the rest
        for extra_path in paths[1:]:
            filename = os.path.basename(extra_path)
            dest = os.path.join(quarantine_dir, filename)

            # avoid overwriting if two duplicates happen to share a filename
            if os.path.exists(dest):
                base, ext = os.path.splitext(filename)
                dest = os.path.join(quarantine_dir, f"{base}_{moved_count}{ext}")

            try:
                shutil.move(extra_path, dest)
                moved_count += 1
            except OSError as e:
                print(f"  ! Couldn't move {extra_path}: {e}")

    print(f"\nMoved {moved_count} duplicate file(s) into: {quarantine_dir}")
    print("Nothing was permanently deleted. Review the folder and delete manually when ready.")


def main():
    parser = argparse.ArgumentParser(
        description="Find duplicate files by content and optionally quarantine them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--path", required=True, help="Folder to scan (scans subfolders too)")
    parser.add_argument(
        "--quarantine",
        action="store_true",
        help="Move duplicate files into a quarantine folder instead of just reporting them",
    )
    parser.add_argument(
        "--quarantine-dir",
        default="./duplicates_found",
        help="Where to move duplicates if --quarantine is used (default: ./duplicates_found)",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=0,
        help="Ignore files smaller than this many bytes (default: 0, no minimum)",
    )
    parser.add_argument(
        "--skip",
        default="",
        help=(
            "Comma-separated folder names to skip entirely, on top of the "
            "built-in defaults (venv, node_modules, .git, .gradle, .godot, "
            "build, dist, and similar). Example: --skip cache,temp,old_backups"
        ),
    )
    parser.add_argument(
        "--no-default-skips",
        action="store_true",
        help="Disable the built-in default skip list — only skip what you pass to --skip",
    )

    args = parser.parse_args()

    if not os.path.isdir(args.path):
        print(f"Error: '{args.path}' is not a valid folder.")
        sys.exit(1)

    skip_dirs = set() if args.no_default_skips else set(DEFAULT_SKIP_DIRS)
    if args.skip:
        skip_dirs |= {name.strip().lower() for name in args.skip.split(",") if name.strip()}

    start = datetime.now()
    duplicate_groups = find_duplicates(args.path, min_size=args.min_size, skip_dirs=skip_dirs)
    reclaimable = report(duplicate_groups)

    if duplicate_groups and args.quarantine:
        confirm = input(
            f"\nMove {sum(len(p) - 1 for p in duplicate_groups.values())} duplicate file(s) "
            f"to '{args.quarantine_dir}'? [y/N]: "
        )
        if confirm.strip().lower() == "y":
            quarantine_duplicates(duplicate_groups, args.quarantine_dir)
        else:
            print("Cancelled — no files were moved.")
    elif duplicate_groups and not args.quarantine:
        print("\n(This was a dry run. Add --quarantine to actually move duplicates.)")

    elapsed = (datetime.now() - start).total_seconds()
    print(f"\nDone in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()
