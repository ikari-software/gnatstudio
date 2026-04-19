# gnatcoll-bindings/python3 patches

Snapshots of the three source files in `gnatcoll-bindings/python3/` that carry
GNAT Studio's local patches. These files live *outside* this repo in the Alire
dependency tree (typically `.tools/src/gnatcoll-bindings/python3/` after
`make -C bootstrap`), so we capture them here for posterity and so a fresh
bootstrap can re-apply them before building.

## What the patches do

### `python_support.c`

Two critical changes for Python 3.14 compatibility, both upstream-worthy:

1. **Modern init path** — replaces the legacy `Py_InitializeEx` + deprecated
   `Py_SetProgramName` / `Py_SetPythonHome` chain with the PEP 587
   `PyConfig_InitPythonConfig` → `PyConfig_SetBytesString` →
   `Py_InitializeFromConfig` chain. `Py_InitializeEx` was deprecated in 3.11
   and on 3.14 silently leaves parts of interpreter state uninitialized
   (static identifier strings in particular), which manifests as
   `EXC_BAD_ACCESS in PyDict_GetItemWithError` inside `type_new` when Ada
   code later tries to create a new class.

   Adds two stasher functions — `ada_py_set_python_home(const char*)` and
   `ada_py_set_executable(const char*)` — that Ada callers use to pass
   home / executable paths in; consumed in `ada_py_initialize_and_module`
   via `PyConfig_SetBytesString`.

2. **`ada_type_new` rewrite** — replaces the direct
   `PyType_Type.tp_new(meta, args, kwargs)` call with
   `PyObject_Call(metatype, args, NULL)`. Python 3.14's `type.__call__`
   performs setup (watcher notifications, static-identifier resolution)
   that `tp_new` assumes was already done — calling `tp_new` directly
   bypasses it and crashes. Also fixes swapped steal/incref refcount
   semantics in the original code (the comments claimed `PyTuple_SetItem`
   increments, but it steals — latent bug mostly harmless pre-3.14).

### `gnatcoll-python-lifecycle.ads` + `.adb`

- Adds `procedure Py_SetExecutable (Executable : String)` public API,
  backed by `ada_py_set_executable`.
- Changes `Py_SetPythonHome (Home : String)` body to call the new
  `ada_py_set_python_home` C stasher instead of the deprecated
  `Py_SetPythonHome` C function. Backwards-compatible at the Ada API
  level.

## Applying the patches

After `make -C bootstrap` checks out `.tools/src/gnatcoll-bindings/`:

```bash
cp osx_bundle/patches/gnatcoll-bindings-python3/python_support.c \
   .tools/src/gnatcoll-bindings/python3/python_support.c
cp osx_bundle/patches/gnatcoll-bindings-python3/gnatcoll-python-lifecycle.ads \
   .tools/src/gnatcoll-bindings/python3/gnatcoll-python-lifecycle.ads
cp osx_bundle/patches/gnatcoll-bindings-python3/gnatcoll-python-lifecycle.adb \
   .tools/src/gnatcoll-bindings/python3/gnatcoll-python-lifecycle.adb
```

Then rebuild `libgnatcoll_python3.dylib` via the gprbuild command captured
in `.tools/src/gnatcoll-bindings/python3/setup.json` (or run
`bootstrap/Makefile`'s `gnatcoll-bindings` target to redo it cleanly).

## Upstream status

These patches should be sent to AdaCore/gnatcoll-bindings as a PR. The
`ada_type_new → PyObject_Call` change fixes a silent refcount bug that
affected all Python versions but only crashed visibly on 3.14 due to
stricter internal state requirements. The PyConfig migration eliminates
deprecation warnings on 3.11+ and is required for 3.14 correctness.
