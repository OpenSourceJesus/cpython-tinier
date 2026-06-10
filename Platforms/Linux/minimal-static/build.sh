#!/bin/sh
# Build a stripped-down, mostly-static CPython for Linux.
#
# Usage:
#   ./Platforms/Linux/minimal-static/build.sh [--clean] [--jobs N]
#
# Output: build-linux-minimal-static/python (static musl binary by default)
#
# Text encoding: UTF-8 only (custom encodings registry, no _locale module,
# no C-locale fallback). See overlay/Lib/encodings/ and PY_MINIMAL_UTF8_ONLY.
#
# Deliberately omitted: tests, docs, pip, GPL deps (readline/gdbm), and
# rarely-used extension modules (see Setup.local). Apple/Windows code is
# not compiled when building on Linux.
#
# Threading: the core _thread module is required by CPython and remains
# enabled. Multiprocessing and the _asyncio accelerator are disabled.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../../.." && pwd)
BUILD_DIR="$ROOT/build-linux-minimal-static"
SETUP="$ROOT/Platforms/Linux/minimal-static/Setup.local"
CONFIG_SITE="$ROOT/Platforms/Linux/minimal-static/config.site"
OVERLAY="$ROOT/Platforms/Linux/minimal-static/overlay"
PATCH_STAMP="$BUILD_DIR/.utf8-overlay-applied"
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
CLEAN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1; shift ;;
        --jobs) JOBS=$2; shift 2 ;;
        -j*) JOBS=${1#-j}; shift ;;
        -h|--help)
            sed -n '2,17p' "$0"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

restore_tree() {
    if [ -f "$PATCH_STAMP" ]; then
        if [ -d "$BUILD_DIR/encodings-backup" ]; then
            rm -rf "$ROOT/Lib/encodings"
            cp -a "$BUILD_DIR/encodings-backup" "$ROOT/Lib/encodings"
        fi
        if [ -f "$BUILD_DIR/freeze_modules.py.bak" ]; then
            cp "$BUILD_DIR/freeze_modules.py.bak" "$ROOT/Tools/build/freeze_modules.py"
        fi
        rm -f "$PATCH_STAMP"
    fi
}

apply_utf8_overlay() {
    mkdir -p "$BUILD_DIR"
    if [ ! -d "$BUILD_DIR/encodings-backup" ]; then
        cp -a "$ROOT/Lib/encodings" "$BUILD_DIR/encodings-backup"
    fi
    cp "$OVERLAY/Lib/encodings/__init__.py" "$ROOT/Lib/encodings/__init__.py"
    cp "$OVERLAY/Lib/encodings/aliases.py" "$ROOT/Lib/encodings/aliases.py"

    FREEZE="$ROOT/Tools/build/freeze_modules.py"
    if [ ! -f "$BUILD_DIR/freeze_modules.py.bak" ]; then
        cp "$FREEZE" "$BUILD_DIR/freeze_modules.py.bak"
    fi
    python3 - "$FREEZE" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
old = """        '<encodings>',
        'encodings.aliases',
        'encodings.utf_8',
        'encodings._win_cp_codecs',"""
new = """        'encodings',
        'encodings.aliases',"""
if old not in text:
    sys.exit("freeze_modules.py layout changed; update minimal-static build.sh")
path.write_text(text.replace(old, new, 1))
PY
    touch "$PATCH_STAMP"
}

trap restore_tree EXIT INT TERM

if [ "$CLEAN" -eq 1 ]; then
    restore_tree
    trap - EXIT INT TERM
    if [ -d "$BUILD_DIR" ]; then
        rm -rf "$BUILD_DIR"
    fi
fi

mkdir -p "$BUILD_DIR/Modules"
cp "$SETUP" "$BUILD_DIR/Modules/Setup.local"
apply_utf8_overlay

CC=${CC:-musl-gcc}
if ! command -v "$CC" >/dev/null 2>&1; then
    echo "musl-gcc not found; install musl or set CC=..." >&2
    exit 1
fi

# GCC 16 + musl static links pass -latomic_asneeded, but musl has no static
# libatomic. An empty archive satisfies the linker on x86_64 (atomics in libgcc).
STUB_LIB="$BUILD_DIR/libatomic_asneeded.a"
if [ ! -f "$STUB_LIB" ]; then
    ar rcs "$STUB_LIB"
fi

UTF8_CFLAGS="-Os -g0 -DPY_MINIMAL_UTF8_ONLY"

cd "$BUILD_DIR"

if [ ! -f Makefile ]; then
    CONFIG_SITE="$CONFIG_SITE" \
    MODULE_BUILDTYPE=static \
    CC="$CC" \
    CFLAGS="$UTF8_CFLAGS" \
    LDFLAGS="-static -L$BUILD_DIR" \
    "$ROOT/configure" \
        --prefix=/usr/local \
        --disable-test-modules \
        --without-readline \
        --without-remote-debug \
        --with-ensurepip=no \
        --with-doc-strings=no \
        --with-dbmliborder=ndbm \
        --with-builtin-hashlib-hashes=sha2,blake2
fi

make -j"$JOBS" CFLAGS="$UTF8_CFLAGS"
strip "$BUILD_DIR/python" 2>/dev/null || true

echo ""
echo "Built: $BUILD_DIR/python"
"$BUILD_DIR/python" -c "import sys; print(sys.version)"
echo ""
echo "Encoding:"
"$BUILD_DIR/python" -c "import sys; print('default:', sys.getdefaultencoding()); print('filesystem:', sys.getfilesystemencoding())"
echo ""
echo "Builtin modules:"
"$BUILD_DIR/python" -c "import sys; print(' '.join(sorted(sys.builtin_module_names)))"
echo ""
if command -v ldd >/dev/null 2>&1; then
    echo "Dynamic deps (expect 'not a dynamic executable' for fully static):"
    ldd "$BUILD_DIR/python" 2>&1 || true
fi
echo ""
ls -lh "$BUILD_DIR/python"

# Keep overlay in place until next --clean (trap restores on exit).
trap - EXIT INT TERM
