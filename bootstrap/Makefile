# bootstrap/Makefile — project-local Ada toolchain + library bootstrap
#
# Builds everything GNAT Studio needs to compile, installing into .tools/
# at the repo root (gitignored).  Nothing is written outside this repo.
#
# Usage:
#   make -C bootstrap          # full bootstrap (all deps)
#   make -C bootstrap toolchain  # just alr + gnat_native + gprbuild
#   make -C bootstrap gtkada     # incrementally add gtkada
#   make -C bootstrap env        # (re)write .tools/env
#   make -C bootstrap clean      # wipe .tools/ entirely
#
# After bootstrap completes:
#   source .tools/env
#   ./configure --with-gtk-prefix=$(GTK_PREFIX)
#   make -j$(NPROC)
#   make -C osx_bundle dmg
#
# VERSION PINS
#   All source repos currently track 'master'.  Once you find a working
#   combination, pin the *_REF variables to specific tags/SHAs so the
#   bootstrap is reproducible.  See comments on each block below.

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT := $(realpath $(CURDIR)/..)
TOOLS_DIR := $(REPO_ROOT)/.tools
BIN_DIR   := $(TOOLS_DIR)/bin
SRC_DIR   := $(TOOLS_DIR)/src
PREFIX    := $(TOOLS_DIR)/prefix

# ── System GTK+3 (Homebrew) ────────────────────────────────────────────────
GTK_PREFIX ?= $(shell brew --prefix gtk+3 2>/dev/null)
PYTHON3    ?= $(shell brew --prefix python3 2>/dev/null)/bin/python3
ifeq ($(GTK_PREFIX),)
$(error "gtk+3 not found via Homebrew. Run: brew install gtk+3")
endif

# ── Parallelism ────────────────────────────────────────────────────────────
NPROC ?= $(shell sysctl -n hw.logicalcpu 2>/dev/null || echo 4)

# ── macOS SDK (needed when GNAT's built-in sysroot spec is stale) ──────────
# GNAT 15 was compiled targeting MacOSX14.sdk; on macOS 26 beta that path
# no longer exists.  We patch the gcc 'specs' file (auto-loaded from the
# toolchain's libgcc dir) to point at whatever xcrun reports.
SDK_PATH := $(shell xcrun --show-sdk-path 2>/dev/null)

# ── Alire ──────────────────────────────────────────────────────────────────
ALR_VERSION := 2.1.0
# Map macOS 'uname -m' output to Alire's naming: arm64 → aarch64
ALR_ARCH    := $(shell uname -m | sed 's/arm64/aarch64/')
ALR_URL     := https://github.com/alire-project/alire/releases/download/v$(ALR_VERSION)/alr-$(ALR_VERSION)-bin-$(ALR_ARCH)-macos.zip
ALR_CONFIG  := $(TOOLS_DIR)/alire
# alr 2.x: use -s flag for settings dir (ALIRE_CONFIG is deprecated)
ALR         := $(BIN_DIR)/alr -s $(ALR_CONFIG)

# alr 2.x stores toolchain binaries in the XDG data dir, not the settings dir.
ALIRE_DATA  := $(HOME)/.local/share/alire
# Helper: locate toolchain bin dirs after alr has installed them
toolchain_bin = $(shell ls -d $(ALIRE_DATA)/toolchains/$(1)*/bin 2>/dev/null | head -1)
GNAT_BIN    = $(call toolchain_bin,gnat_native)
GPR_BIN     = $(call toolchain_bin,gprbuild)
# Use real toolchain bin dirs so gprbuild can find its share/gprconfig/ kb.
# .tools/bin/ symlinks are for human convenience only.
CHAIN_PATH  = $(GNAT_BIN):$(GPR_BIN):$(PATH)

# ── GPR search path shared by all sub-builds ───────────────────────────────
GPR_PATH = $(PREFIX)/share/gpr:$(PREFIX)/lib/gnat

# ── Source repo refs ───────────────────────────────────────────────────────
# Start with master; tighten to a tag once the build works end-to-end.
# AdaCore tags are typically YYYY.minor.patch (e.g. 25.0.0).
XMLADA_REF   ?= master
GNATCOLL_REF ?= master
GNATBIND_REF ?= master
GTKADA_REF   ?= master

# ── Stamp files ────────────────────────────────────────────────────────────
S_ALR      := $(TOOLS_DIR)/.stamp-alr
S_CHAIN    := $(TOOLS_DIR)/.stamp-toolchain
S_XMLADA   := $(TOOLS_DIR)/.stamp-xmlada
S_GNATCOLL := $(TOOLS_DIR)/.stamp-gnatcoll
S_GNATBIND := $(TOOLS_DIR)/.stamp-gnatcoll-bindings
S_GTKADA   := $(TOOLS_DIR)/.stamp-gtkada
S_SPECS    := $(TOOLS_DIR)/.stamp-sdk-specs

# ── Top-level targets ──────────────────────────────────────────────────────
.PHONY: all alr toolchain sdk-specs xmlada gnatcoll gnatcoll-bindings gtkada \
        env clean clean-stamps help

all: $(S_GTKADA) env
	@echo ""
	@echo "Bootstrap complete.  Next:"
	@echo "  source .tools/env"
	@echo "  ./configure --with-gtk-prefix=$(GTK_PREFIX)"
	@echo "  make -j$(NPROC)"

help:
	@echo "Targets (each builds on the previous):"
	@echo "  alr                 download alr $(ALR_VERSION) -> .tools/bin/alr"
	@echo "  toolchain           install gnat_native + gprbuild via alr"
	@echo "  xmlada              build xmlada into .tools/prefix/"
	@echo "  gnatcoll            build gnatcoll-core (no gnatcoll_projects)"
	@echo "  gnatcoll-bindings   build gnatcoll python3 + iconv bindings"
	@echo "  gtkada              build gtkada against system GTK+3"
	@echo "  env                 write .tools/env  (source before configure)"
	@echo "  clean               remove all of .tools/"
	@echo "  clean-stamps        remove stamp files (forces rebuild, keeps sources)"

# ── Directory scaffolding ──────────────────────────────────────────────────
$(BIN_DIR) $(SRC_DIR) $(PREFIX) $(ALR_CONFIG):
	mkdir -p $@

# ── 1. alr binary ──────────────────────────────────────────────────────────
$(S_ALR): | $(BIN_DIR) $(TOOLS_DIR)
	@echo "==> Downloading alr v$(ALR_VERSION) ($(ALR_ARCH))"
	curl -fL -o /tmp/alr-$(ALR_VERSION).zip "$(ALR_URL)"
	unzip -o /tmp/alr-$(ALR_VERSION).zip -d /tmp/alr-$(ALR_VERSION)/
	install -m 755 /tmp/alr-$(ALR_VERSION)/bin/alr $(BIN_DIR)/alr
	rm -rf /tmp/alr-$(ALR_VERSION).zip /tmp/alr-$(ALR_VERSION)/
	@touch $@

alr: $(S_ALR)

$(TOOLS_DIR):
	mkdir -p $@

# ── 2. GNAT native toolchain + gprbuild ────────────────────────────────────
# --batch lets alr pick the default (latest stable) non-interactively.
$(S_CHAIN): $(S_ALR) | $(ALR_CONFIG)
	@echo "==> Installing gnat_native and gprbuild via alr"
	$(ALR) -n toolchain --select gnat_native
	$(ALR) -n toolchain --select gprbuild
	@echo "==> Symlinking toolchain bins into .tools/bin/"
	@for f in $(call toolchain_bin,gnat_native)/gnat \
	           $(call toolchain_bin,gnat_native)/gnatmake \
	           $(call toolchain_bin,gnat_native)/gnatlink \
	           $(call toolchain_bin,gnat_native)/gnatbind \
	           $(call toolchain_bin,gnat_native)/gcc \
	           $(call toolchain_bin,gnat_native)/g++ \
	           $(call toolchain_bin,gprbuild)/gprbuild \
	           $(call toolchain_bin,gprbuild)/gprclean; do \
	    [ -f "$$f" ] && ln -sf "$$f" $(BIN_DIR)/; \
	done
	@echo "    GNAT: $$($(BIN_DIR)/gnat --version 2>&1 | head -1)"
	@echo "    gprbuild: $$($(BIN_DIR)/gprbuild --version 2>&1 | head -1)"
	@touch $@

toolchain: $(S_CHAIN)

# ── 2b. Wrap GNAT gcc binaries for current macOS SDK ──────────────────────
# GNAT 15 was compiled against MacOSX14.sdk which no longer exists on
# macOS 26 beta.  The --with-specs compiled into gcc hardcodes the stale
# sysroot; we cannot override it via a specs file.  Instead we wrap every
# gcc binary to prepend -isysroot with the real SDK path:
#
#   gcc                          – used by gprlib for shared-lib linking
#   aarch64-apple-darwin*-gcc*   – used by gprconfig as the C Driver
#
# Each binary is renamed to <name>.real and replaced by a tiny sh wrapper.
$(S_SPECS): $(S_CHAIN)
	@echo "==> Wrapping GNAT gcc binaries with macOS SDK isysroot fix"
	@echo "    SDK: $(SDK_PATH)"
	@gnatbin="$(GNAT_BIN)"; \
	wrap() { \
	    f="$$1"; \
	    if [ -f "$$f.real" ]; then \
	        echo "    already wrapped — skipping: $$f"; \
	        return; \
	    fi; \
	    mv "$$f" "$$f.real"; \
	    printf '#!/bin/sh\n# macOS SDK shim injected by bootstrap/Makefile\nexec "%s.real" -isysroot "%s" "$$@"\n' \
	        "$$f" "$(SDK_PATH)" > "$$f"; \
	    chmod +x "$$f"; \
	    echo "    Wrapped: $$f"; \
	}; \
	wrap "$$gnatbin/gcc"; \
	for f in "$$gnatbin"/aarch64-apple-darwin*-gcc \
	          "$$gnatbin"/aarch64-apple-darwin*-gcc-[0-9]*; do \
	    [ -f "$$f" ] && wrap "$$f"; \
	done
	@echo "==> Disabling GNAT include-fixed headers (macOS 14 SDK frozen, conflicts with macOS 26)"
	@fixinc="$(GNAT_BIN)/../lib/gcc/aarch64-apple-darwin23.6.0/15.0.1/include-fixed"; \
	fixinc=$$(cd "$$fixinc" 2>/dev/null && pwd) || true; \
	if [ -d "$$fixinc" ]; then \
	    for h in "$$fixinc"/stdio.h "$$fixinc"/math.h; do \
	        [ -f "$$h" ] && mv "$$h" "$$h.disabled" && echo "    Disabled: $$h"; \
	    done; \
	fi
	@echo "==> Generating gtkada gprbuild configuration (disables C dep tracking)"
	@PATH="$(CHAIN_PATH)" \
	$(GPR_BIN)/gprconfig \
	    --batch \
	    --config=Ada \
	    --config=C \
	    -o "$(TOOLS_DIR)/gtkada_config.cgpr" 2>&1 | grep -v "unknown language" || true
	@python3 -c "\
import re; \
txt = open('$(TOOLS_DIR)/gtkada_config.cgpr').read(); \
txt = re.sub(r'for Dependency_Switches \(\"C\"\) use [^;]+;', \
    'for Dependency_Switches (\"C\") use ();\n      for Dependency_Kind (\"C\") use \"None\";', txt); \
open('$(TOOLS_DIR)/gtkada_config.cgpr', 'w').write(txt); \
print('    C dependency tracking disabled')"
	@touch $@

sdk-specs: $(S_SPECS)

# ── 3. XML/Ada ─────────────────────────────────────────────────────────────
$(SRC_DIR)/xmlada: | $(SRC_DIR)
	git clone --depth=1 -b $(XMLADA_REF) \
	    git@github.com:AdaCore/xmlada.git $@

$(S_XMLADA): $(S_SPECS) $(SRC_DIR)/xmlada | $(PREFIX)
	@echo "==> Building xmlada"
	cd $(SRC_DIR)/xmlada && \
	    PATH="$(CHAIN_PATH)" \
	    ./configure --prefix=$(PREFIX) --enable-shared && \
	    PATH="$(CHAIN_PATH)" \
	    make -j$(NPROC) all && \
	    PATH="$(CHAIN_PATH)" \
	    make install
	@touch $@

xmlada: $(S_XMLADA)

# ── 4. GNATcoll core ───────────────────────────────────────────────────────
# Note: GNATCOLL_PROJECTS=no skips the gnatcoll_projects component (which
# needs AdaCore/gpr) since GNAT Studio does not require project-file parsing
# from gnatcoll directly.  The build uses Python .gpr.py scripts internally.
$(SRC_DIR)/gnatcoll-core: | $(SRC_DIR)
	git clone --depth=1 -b $(GNATCOLL_REF) \
	    git@github.com:AdaCore/gnatcoll-core.git $@

$(S_GNATCOLL): $(S_XMLADA) $(SRC_DIR)/gnatcoll-core
	@echo "==> Building gnatcoll-core"
	cd $(SRC_DIR)/gnatcoll-core && \
	    PATH="$(CHAIN_PATH)" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    PYTHON=$(PYTHON3) \
	    make PROCESSORS=$(NPROC) \
	        ENABLE_SHARED=yes \
	        GNATCOLL_PROJECTS=no \
	        build && \
	    PATH="$(CHAIN_PATH)" \
	    make prefix=$(PREFIX) install
	@touch $@

gnatcoll: $(S_GNATCOLL)

# ── 6. GNATcoll bindings (python3 + iconv) ────────────────────────────────
# Each binding lives in its own subdirectory with an independent Makefile.
# The python3 binding requires PyGObject headers; iconv is pure C.
$(SRC_DIR)/gnatcoll-bindings: | $(SRC_DIR)
	git clone --depth=1 -b $(GNATBIND_REF) \
	    git@github.com:AdaCore/gnatcoll-bindings.git $@

$(S_GNATBIND): $(S_GNATCOLL) $(SRC_DIR)/gnatcoll-bindings
	@echo "==> Building gnatcoll-bindings (iconv)"
	cd $(SRC_DIR)/gnatcoll-bindings/iconv && \
	    PATH="$(CHAIN_PATH)" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    $(PYTHON3) setup.py build \
	        --jobs=$(NPROC) \
	        --prefix=$(PREFIX) \
	        --library-types=relocatable && \
	    PATH="$(CHAIN_PATH)" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    $(PYTHON3) setup.py install \
	        --prefix=$(PREFIX)
	@echo "==> Building gnatcoll-bindings (python3)"
	cd $(SRC_DIR)/gnatcoll-bindings/python3 && \
	    PATH="$(CHAIN_PATH)" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    $(PYTHON3) setup.py build \
	        --jobs=$(NPROC) \
	        --prefix=$(PREFIX) \
	        --library-types=relocatable && \
	    PATH="$(CHAIN_PATH)" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    $(PYTHON3) setup.py install \
	        --prefix=$(PREFIX)
	@touch $@

gnatcoll-bindings: $(S_GNATBIND)

# ── 7. GtkAda ──────────────────────────────────────────────────────────────
# Must be built against the exact GTK+3 installation configure.ac will use.
# --with-GL=no avoids the mesa/GL rabbit hole on macOS.
$(SRC_DIR)/gtkada: | $(SRC_DIR)
	git clone --depth=1 -b $(GTKADA_REF) \
	    git@github.com:AdaCore/gtkada.git $@

$(S_GTKADA): $(S_GNATBIND) $(SRC_DIR)/gtkada
	@echo "==> Building gtkada (GTK_PREFIX=$(GTK_PREFIX))"
	cd $(SRC_DIR)/gtkada && \
	    PATH="$(CHAIN_PATH)" \
	    PKG_CONFIG_PATH="$(GTK_PREFIX)/lib/pkgconfig" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    ./configure \
	        --prefix=$(PREFIX) \
	        --with-GL=no && \
	    PATH="$(CHAIN_PATH)" \
	    GPR_PROJECT_PATH="$(GPR_PATH)" \
	    make -j1 \
	        "GPRBUILD_FULL=$(GPR_BIN)/gprbuild -j$(NPROC) -p --config=$(TOOLS_DIR)/gtkada_config.cgpr" \
	        "GPRINSTALL_OPTIONS=--config=$(TOOLS_DIR)/gtkada_config.cgpr" && \
	    make install \
	        "GPRINSTALL_OPTIONS=--config=$(TOOLS_DIR)/gtkada_config.cgpr"
	@touch $@

gtkada: $(S_GTKADA)

# ── 8. Dev environment file ────────────────────────────────────────────────
# Source this before running ./configure + make.
# It is regenerated by 'make -C bootstrap env'; the file itself is gitignored.
env: $(TOOLS_DIR)/env

$(TOOLS_DIR)/env: | $(TOOLS_DIR)
	@echo "==> Writing .tools/env"
	@( \
	  echo '# Auto-generated by bootstrap/Makefile — re-run "make -C bootstrap env" to refresh.'; \
	  echo '# Usage: source .tools/env'; \
	  echo ''; \
	  echo 'TOOLS_DIR="$(TOOLS_DIR)"'; \
	  echo '# alr 2.x stores toolchains in XDG data dir, not the settings dir'; \
	  echo 'ALIRE_DATA="$$HOME/.local/share/alire"'; \
	  echo 'GNAT_BIN="$$(ls -d "$$ALIRE_DATA"/toolchains/gnat_native*/bin 2>/dev/null | head -1)"'; \
	  echo 'GPR_BIN="$$(ls -d "$$ALIRE_DATA"/toolchains/gprbuild*/bin 2>/dev/null | head -1)"'; \
	  echo ''; \
	  echo 'export PATH="$$GNAT_BIN:$$GPR_BIN:$$TOOLS_DIR/prefix/bin:$$PATH"'; \
	  echo 'export GPR_PROJECT_PATH="$$TOOLS_DIR/prefix/share/gpr:$$TOOLS_DIR/prefix/lib/gnat"'; \
	  echo 'export ADA_PROJECT_PATH="$$GPR_PROJECT_PATH"'; \
	  echo 'export PKG_CONFIG_PATH="$(GTK_PREFIX)/lib/pkgconfig:$$PKG_CONFIG_PATH"'; \
	  echo 'export DYLD_FALLBACK_LIBRARY_PATH="$$TOOLS_DIR/prefix/lib:$(GTK_PREFIX)/lib:$$DYLD_FALLBACK_LIBRARY_PATH"'; \
	  echo ''; \
	  echo '# For ./configure'; \
	  echo 'export GTK_PREFIX="$(GTK_PREFIX)"'; \
	  echo ''; \
	  echo 'echo "GNAT Studio dev env: $$(gnat --version 2>&1 | head -1)"'; \
	) > $@

# ── Clean ──────────────────────────────────────────────────────────────────
clean:
	@echo "Removing $(TOOLS_DIR) ..."
	rm -rf $(TOOLS_DIR)

clean-stamps:
	@echo "Removing stamp files (sources and prefix kept)"
	rm -f $(TOOLS_DIR)/.stamp-*
