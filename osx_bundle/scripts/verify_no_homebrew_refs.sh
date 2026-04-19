#!/bin/bash
# Build-time guard: fail the build if any bundled Mach-O file still references
# /opt/homebrew via its LC_LOAD_DYLIB entries.  Duplicate-library bugs are
# silent at bundle time and catastrophic at runtime — any Homebrew-absolute
# reference will pull a second copy of libglib/libgobject/libgtk into the
# process on top of the bundled one, causing ObjC duplicate-class warnings,
# GObject type-system corruption, and eventual aborts in GTK icon lookup.
#
# Usage:
#   verify_no_homebrew_refs.sh <bundle_root>
#
# where <bundle_root> is typically Contents/MacOS/ inside the .app.

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <bundle_root>" >&2
    exit 2
fi

ROOT="$1"
if [ ! -d "$ROOT" ]; then
    echo "ERROR: bundle root '$ROOT' does not exist" >&2
    exit 2
fi

echo "Scanning $ROOT for Homebrew-absolute Mach-O deps..."

# We tolerate *one* remaining absolute reference: the Homebrew Python
# framework itself.  gnatstudio_exe and libgnatcoll_python3.dylib link
# against /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/
# 3.14/Python by install name.  That framework is a runtime requirement
# (we don't bundle Python.framework — we bundle the stdlib, but the
# interpreter proper comes from the user's Python install).  Everything
# else must be clean.
ALLOWED_RE='/opt/homebrew/opt/python@[0-9.]+/Frameworks/Python.framework/.*/Python'

leaks=0
while IFS= read -r f; do
    # otool -L lists all LC_LOAD_DYLIB deps, first line is the file header.
    deps=$(xcrun otool -L "$f" 2>/dev/null | tail -n +2 | awk '{print $1}')
    # Any dep starting with /opt/homebrew that isn't the allowed Python framework.
    bad=$(echo "$deps" | grep '^/opt/homebrew/' | grep -vE "^${ALLOWED_RE}\$" || true)
    if [ -n "$bad" ]; then
        echo "LEAK: $f"
        echo "$bad" | sed 's/^/    /'
        leaks=$((leaks + 1))
    fi
done < <(find "$ROOT" -type f \( -name '*.dylib' -o -name '*.so' \))

if [ "$leaks" -gt 0 ]; then
    echo ""
    echo "FAIL: $leaks files still reference /opt/homebrew." >&2
    echo "      These would cause duplicate libraries at runtime." >&2
    echo "      Extend install.py's file list in Makefile or verify that" >&2
    echo "      fixup_bundle_deps.py copied their deps into $ROOT/lib/." >&2
    exit 1
fi

echo "OK: no Homebrew-absolute Mach-O refs (other than Python.framework)."
