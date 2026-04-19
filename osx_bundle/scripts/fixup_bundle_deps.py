#!/usr/bin/env python3
"""Rewrite remaining absolute-path dylib references to bare names.

After install.py and copy_rpath_deps.py have run, some bundled Mach-O files
still reference dependencies via absolute Homebrew paths (e.g.
/opt/homebrew/opt/glib/lib/libglib-2.0.0.dylib).  At runtime, dyld loads
those from the absolute path — pulling in the *system* copy alongside the
bundled copy, causing duplicate ObjC class crashes and GObject type confusion.

This script walks every Mach-O file under <root>/bin and <root>/lib and
rewrites any absolute-path dependency whose basename already exists in
<root>/lib to the bare basename.  The gps_wrapper sets
DYLD_FALLBACK_LIBRARY_PATH=$root/lib, so bare names are found there.

Usage:
    fixup_bundle_deps.py <root>
"""

import os
import subprocess
import sys


def otool_deps(path):
    """Return list of install-name strings from otool -L (skip first line)."""
    try:
        out = subprocess.check_output(
            ["xcrun", "otool", "-L", path],
            stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return []
    deps = []
    for line in out.splitlines()[1:]:
        s = line.strip()
        if s:
            deps.append(s.split()[0])
    return deps


def fixup(path, lib_dir):
    """Rewrite absolute deps in *path* to bare names when present in lib_dir."""
    changed = False
    for dep in otool_deps(path):
        if not os.path.isabs(dep):
            continue
        basename = os.path.basename(dep)
        if os.path.exists(os.path.join(lib_dir, basename)):
            subprocess.call(
                ["xcrun", "install_name_tool", "-change", dep, basename, path],
                stderr=subprocess.DEVNULL)
            changed = True
    # Also fix the install id to bare basename for dylibs
    if changed and path.endswith(".dylib"):
        subprocess.call(
            ["xcrun", "install_name_tool", "-id", os.path.basename(path), path],
            stderr=subprocess.DEVNULL)
    return changed


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <root>", file=sys.stderr)
        sys.exit(1)

    root = sys.argv[1]
    lib_dir = os.path.join(root, "lib")
    bin_dir = os.path.join(root, "bin")

    targets = []
    # Collect all Mach-O files in bin/ and lib/
    for d in [bin_dir, lib_dir]:
        for dirpath, _dirs, filenames in os.walk(d):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                if fn.endswith((".dylib", ".so")) or (
                    os.path.isfile(fp) and os.access(fp, os.X_OK)
                    and dirpath == bin_dir
                ):
                    targets.append(fp)

    count = 0
    for t in targets:
        if fixup(t, lib_dir):
            count += 1
    print(f"  fixup_bundle_deps: rewrote absolute deps in {count} files")


if __name__ == "__main__":
    main()
