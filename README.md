# GNAT Studio

> **This is a fork of [AdaCore/gnatstudio](https://github.com/AdaCore/gnatstudio).**
> Upstream's `master` is the canonical source; this fork carries macOS-bundle
> and toolchain-bootstrap work that upstream hasn't picked up. See
> [§ Fork divergence](#fork-divergence-as-of-april-2026) below.

- [What is GNAT Studio?](#what-is-gps)
- [Fork divergence (as of April 2026)](#fork-divergence-as-of-april-2026)
- [Building](#building)

## What is GNAT Studio?

GNAT Studio is a lightweight, extensible IDE, intended to develop high-integrity software in **Ada** and **SPARK**, with support for **C** and **C++** as well.

![GPS - Screenshot](/docs/users_guide/gps-main-window.png?raw=true)

## Fork divergence (as of April 2026)

### Why this fork exists

Upstream's macOS build pipeline (`osx_bundle/`) was last meaningfully
updated in the GPS-era and is structurally stuck on **Python 2.7**, the
**`.pkg` / `productbuild`** distribution format, the pre-rename **`gps_exe`**
binary name, and **`$(GTK_PREFIX)`** (rather than Homebrew). It does not
build a working bundle on a modern Mac. AdaCore's primary development
happens on internal GitLab and reaches GitHub as occasional snapshots, so
the public repo is not where macOS-bundle work currently lands.

This fork exists because we needed a working GNAT Studio bundle on
**macOS 26 / Apple Silicon / Python 3.14**, and "modernize the macOS
bundle" turned out to require a ground-up rewrite of `osx_bundle/`,
patches into a sibling repo (`AdaCore/gnatcoll-bindings`), and an
Alire-based toolchain bootstrap — well outside the shape of a single
upstream PR.

### What's different here

| Area | Upstream | This fork |
|---|---|---|
| macOS bundle Python | 2.7 | 3.14 |
| macOS bundle output | `.pkg` (productbuild) | `.dmg` (with codesign + entitlements) |
| macOS bundle binary name | `gps_exe` | `gnatstudio_exe` |
| macOS prefix variable | `$(GTK_PREFIX)` only | `$(HOMEBREW_PREFIX)` + `$(GTK_PREFIX)` |
| Bundle dep walking | `install.py` over a small static list | `install.py` recurses Python C extensions, `gi/`, `gio/modules/`, plus explicit `librsvg` + `py3cairo` feeds; `copy_rpath_deps.py` handles Ada `@rpath` deps; `fixup_bundle_deps.py` final sweep |
| Build-time invariant check | none | `verify_no_homebrew_refs.sh` fails the build if any bundled Mach-O still has a `/opt/homebrew/...` `LC_LOAD_DYLIB` (single-library-stack guarantee) |
| Hardened-runtime entitlements | none | `osx_bundle/srcs/entitlements.plist` (allow-dyld-environment-variables, disable-library-validation, allow-jit) |
| Python init in `gps-python_core.adb` | deprecated `Py_InitializeEx`/`Py_SetProgramName` | `PyConfig_InitPythonConfig` + `Py_InitializeFromConfig` (Python 3.14-compatible; patches captured under `osx_bundle/patches/gnatcoll-bindings-python3/`) |
| Toolchain bootstrap | system-installed expected | `make -C bootstrap` provisions alr + gnat_native + gprbuild + xmlada + gnatcoll + gnatcoll-bindings + gtkada into `.tools/` |
| `lsp_client` ALS API | older | adapted to ALS 26.0.0 (Location.hidden field gone, etc.) |
| libadalang Python binding | external | vendored snapshot at `osx_bundle/vendored/libadalang-python/` for reproducible builds when the Alire dep tree isn't live |

### Commits to read for context

- [`a61e6385`](../../commit/a61e6385) — `macOS 26 bundle: modernize Python init, fix duplicate-library root cause` (the bulk of the rewrite)
- [`ad96540c`](../../commit/ad96540c) — `osx_bundle: bundle librsvg explicitly so its dep walk runs`
- [`f3b098a0`](../../commit/f3b098a0) — `osx_bundle: bundle py3cairo to prevent duplicate libcairo stack`

### Upstreaming status

None of the above is currently in flight as a PR against
`AdaCore/gnatstudio`. The macOS bundle work has cross-repo dependencies
(patches required in `AdaCore/gnatcoll-bindings`) and is large enough
that opening it cold without prior maintainer engagement would not
serve anyone. If you would like to drive any of this upstream, please
open an issue first to gauge interest before doing the rebase work.

## Building

### Requirements

GNAT Studio requires:

- A recent version of [Gtk+](http://www.gtk.org/) (currently using version 3.24)
- An install of Python which includes [PyGObject](https://wiki.gnome.org/action/show/Projects/PyGObject) and [Pycairo](https://cairographics.org/pycairo/)
- An install of [GtkAda](https://github.com/AdaCore/gtkada)
- An install of [GNATcoll](https://github.com/AdaCore/gnatcoll), configured with support for projects and Python scripting (`--enable-project`, `--with-python=...`)

See the `INSTALL` file for details.
