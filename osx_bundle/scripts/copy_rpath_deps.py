#!/usr/bin/env python3
"""Bundle @rpath dylib dependencies into the app's lib dir and fix install names.

Ada libraries compiled by gprbuild use @rpath-based install names that point
into the Alire cache or a source-tree .tools/prefix.  install.py can only
handle absolute-path deps; this companion script resolves the @rpath entries
from the *source* binaries (which still have correct LC_RPATH entries) and
then:

  1. copies the resolved dylibs into <lib_dir>/
  2. changes every "@rpath/libfoo.dylib" reference in the *bundle* binaries
     (and in the freshly bundled dylibs) to the bare name "libfoo.dylib"
  3. sets each bundled dylib's install name to its bare basename

At runtime gps_wrapper sets DYLD_FALLBACK_LIBRARY_PATH=$root/lib, so bare
names are found without needing explicit rpaths in the bundle.

Usage:
    copy_rpath_deps.py <lib_dir>
        --source <src_bin> [<src_bin> ...]   # source-tree binaries (correct rpaths)
        --patch  <bnd_bin> [<bnd_bin> ...]   # installed bundle binaries to patch

If --source is omitted, --patch binaries are used for both collection and
patching (works when rpaths are still absolute, i.e. Alire-cache paths).
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_otool_L(path):
    """Return list of install-name strings from `otool -L path` (skips first line)."""
    try:
        out = subprocess.check_output(
            ["xcrun", "otool", "-L", path],
            stderr=subprocess.DEVNULL, text=True)
        result = []
        for line in out.splitlines()[1:]:
            s = line.strip()
            if s:
                result.append(s.split()[0])
        return result
    except subprocess.CalledProcessError:
        return []


def _get_rpaths_raw(path):
    """Return the raw LC_RPATH strings stored in *path* (no resolution of
    @executable_path / @loader_path).  Used for stripping rpaths via
    install_name_tool -delete_rpath, which needs the exact stored string."""
    try:
        out = subprocess.check_output(
            ["xcrun", "otool", "-l", path],
            stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return []

    raw = []
    in_rpath = False
    for line in out.splitlines():
        s = line.strip()
        if "LC_RPATH" in s:
            in_rpath = True
            continue
        if in_rpath:
            if s.startswith("path "):
                # Preserve the exact stored string (including any leading space
                # that gprbuild embeds) so install_name_tool -delete_rpath gets
                # the byte-for-byte match it requires.
                # s = "path  @executable_path/..." (stripped line, but value
                # may have a leading space making it "path  " + value).
                # Strip only the "(offset N)" suffix, not leading whitespace.
                val = s[5:].split(" (offset")[0]
                raw.append(val)
                in_rpath = False
            elif s.startswith("cmd ") or s.startswith("Load command"):
                in_rpath = False
    return raw


def _get_rpaths(path):
    """Return the list of LC_RPATH values from *path*, resolving @executable_path
    and @loader_path relative to path's real location."""
    base = os.path.dirname(os.path.realpath(path))
    rpaths = []
    for rp in _get_rpaths_raw(path):
        rp = rp.strip()  # strip leading/trailing spaces embedded by gprbuild
        if rp.startswith("@executable_path"):
            rp = os.path.normpath(rp.replace("@executable_path", base, 1))
        elif rp.startswith("@loader_path"):
            rp = os.path.normpath(rp.replace("@loader_path", base, 1))
        if os.path.isabs(rp):
            rpaths.append(rp)
    return rpaths


def _strip_all_rpaths(path):
    """Remove every LC_RPATH entry from *path*.

    After copy_rpath_deps.py rewrites all @rpath/libfoo deps to bare names,
    the remaining rpaths only interfere with dyld on macOS 26 (duplicate-rpath
    check aborts loading).  With DYLD_FALLBACK_LIBRARY_PATH=$root/lib set by
    gps_wrapper, bare names are found without any rpaths in the binary.
    """
    for rp in _get_rpaths_raw(path):
        subprocess.call(
            ["xcrun", "install_name_tool", "-delete_rpath", rp, path],
            stderr=subprocess.DEVNULL)


def _resolve_rpath_dep(dep, rpaths):
    """If *dep* is '@rpath/libfoo.dylib', try each rpath and return the first
    existing absolute path.  Returns None if not found."""
    if not dep.startswith("@rpath/"):
        return None
    libname = dep[len("@rpath/"):]
    for rp in rpaths:
        candidate = os.path.join(rp, libname)
        if os.path.isfile(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect_rpath_deps(source_binaries):
    """Walk *source_binaries* transitively and return a dict mapping each
    '@rpath/libname' string to its resolved absolute source path."""
    to_bundle = {}   # dep_str -> abs_src_path
    pending = list(source_binaries)
    seen = set()

    while pending:
        path = pending.pop()
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)

        rpaths = _get_rpaths(path)
        for dep in _run_otool_L(path):
            if not dep.startswith("@rpath/"):
                continue
            if dep in to_bundle:
                # already known – still recurse into it
                pending.append(to_bundle[dep])
                continue
            abs_src = _resolve_rpath_dep(dep, rpaths)
            if abs_src:
                to_bundle[dep] = abs_src
                pending.append(abs_src)   # follow transitive deps
            else:
                print(f"  Warning: could not resolve {dep} from {path}")

    return to_bundle


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def _patch_binary(path, to_bundle):
    """Replace every '@rpath/libname' dep in *path* with the bare 'libname'."""
    current_deps = set(_run_otool_L(path))
    for dep_str, abs_src in to_bundle.items():
        if dep_str not in current_deps:
            continue
        libname = os.path.basename(abs_src)
        subprocess.call(
            ["xcrun", "install_name_tool", "-change", dep_str, libname, path],
            stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lib_dir",
                    help="Directory inside the bundle to place dylibs (e.g. MacOS/lib)")
    ap.add_argument("--source", nargs="+", default=[], metavar="BIN",
                    help="Source-tree binaries with correct @executable_path rpaths")
    ap.add_argument("--patch", nargs="+", default=[], metavar="BIN",
                    help="Installed bundle binaries whose @rpath refs need fixing")
    args = ap.parse_args()

    lib_dir = args.lib_dir
    os.makedirs(lib_dir, exist_ok=True)

    # Use source binaries for collection; fall back to patch targets if not given.
    collect_from = args.source if args.source else args.patch

    print("Collecting @rpath dependencies from source binaries...")
    to_bundle = collect_rpath_deps(collect_from)
    if not to_bundle:
        print("  No @rpath dependencies found.")
        return
    print(f"  Found {len(to_bundle)} @rpath dependencies")

    # 1. Copy dylibs to lib_dir
    bundled_paths = []
    for dep_str, abs_src in to_bundle.items():
        libname = os.path.basename(abs_src)
        dest = os.path.join(lib_dir, libname)
        if not os.path.exists(dest):
            print(f"  Bundling: {libname}")
            shutil.copy2(abs_src, dest)
            st = os.stat(dest)
            os.chmod(dest, st.st_mode | stat.S_IWUSR)
        bundled_paths.append(dest)

    # 2. Patch bundle binaries
    for binary in args.patch:
        if os.path.isfile(binary):
            _patch_binary(binary, to_bundle)

    # 3. Patch the bundled dylibs themselves (transitive @rpath refs inside them)
    for dest in bundled_paths:
        _patch_binary(dest, to_bundle)
        # Fix install name to bare basename
        libname = os.path.basename(dest)
        subprocess.call(
            ["xcrun", "install_name_tool", "-id", libname, dest],
            stderr=subprocess.DEVNULL)

    # 4. Strip all remaining LC_RPATH entries from bundle binaries and dylibs.
    #    After step 2-3 all @rpath/... references are gone; leftover rpaths only
    #    cause macOS 26 dyld to abort with "duplicate LC_RPATH".
    #    gps_wrapper sets DYLD_FALLBACK_LIBRARY_PATH=$root/lib so bare names
    #    are found without rpaths.
    all_targets = [b for b in args.patch if os.path.isfile(b)] + bundled_paths
    for target in all_targets:
        _strip_all_rpaths(target)

    print("  @rpath bundling complete.")


if __name__ == "__main__":
    main()
