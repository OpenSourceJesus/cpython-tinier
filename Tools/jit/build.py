#!/usr/bin/env python3
"""LLVM-free JIT stencil generator for CPython, powered by ShivyC.

This is a drop-in replacement for CPython's upstream ``Tools/jit/build.py``,
which shells out to clang/LLVM to produce copy-and-patch stencils. Instead, we
drive the vendored pure-Python C compiler ShivyC (``Tools/jit/_shivyc``) so the
JIT can be built with *no* LLVM/clang dependency at all -- a direct answer to
the Steering Council's concern that the JIT is too tightly coupled to a single
backend and burdens redistributors with an LLVM build-time requirement.

Because ShivyC implements only a subset of C11 and cannot parse CPython's real
``Python.h``/``executor_cases.c.h``, this is a proof of concept: every micro-op
is compiled to the same small, self-contained "deopt" stencil that safely
returns control to the tier-1 interpreter. The interpreter then does the real
work, so execution stays correct while the ShivyC-generated machine code is
genuinely produced, laid out and executed by the JIT at runtime.

Invoked by the build as::

    python Tools/jit/build.py <host-triple> --output-dir . --pyconfig-dir . \
        --cflags=... --llvm-version=... --llvm-tools-install-dir=...

The ``--llvm-*`` flags are accepted and ignored so ``configure`` needs no edit.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _stencils import Stencil, compile_c, extract  # noqa: E402

HERE = Path(__file__).resolve().parent          # <srcdir>/Tools/jit
SRCDIR = HERE.parent.parent                       # <srcdir>


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="+", help="host triple(s)")
    parser.add_argument("-o", "--output-dir", default=".",
                        help="where to write the generated headers/objects")
    parser.add_argument("-p", "--pyconfig-dir", default=".",
                        help="directory containing pyconfig.h")
    parser.add_argument("-c", "--cflags", default="",
                        help="extra cflags (forwarded to the offsets/shim probe)")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="(accepted for compatibility; ignored)")
    # Accepted purely for compatibility with the stock configure invocation.
    parser.add_argument("--llvm-version", default="", help=argparse.SUPPRESS)
    parser.add_argument("--llvm-tools-install-dir", default="",
                        help=argparse.SUPPRESS)
    # Be lenient about any other future flags configure might pass.
    args, _unknown = parser.parse_known_args(argv)
    return args


def canonical_triple(triple: str) -> str:
    """Map a host triple to the label configure uses for JIT artifacts.

    configure.ac canonicalizes ``$host`` (e.g. ``x86_64-pc-linux-gnu``) into the
    LLVM-style triple used to name ``jit_stencils-<triple>.h`` and
    ``jit_shim-<triple>.o`` (e.g. ``x86_64-unknown-linux-gnu``). We must emit
    files under the exact same name or the Makefile targets go unsatisfied.
    """
    parts = triple.split("-")
    arch = parts[0]
    if "linux" in triple:
        return f"{arch}-unknown-linux-gnu"
    if "darwin" in triple or "apple" in triple:
        return f"{arch}-apple-darwin"
    if "windows" in triple or "msvc" in triple:
        return f"{arch}-pc-windows-msvc"
    return triple


def read_max_uop_regs_id() -> int:
    header = SRCDIR / "Include" / "internal" / "pycore_uop_ids.h"
    text = header.read_text()
    match = re.search(r"#define\s+MAX_UOP_REGS_ID\s+(\d+)", text)
    if not match:
        raise RuntimeError("could not find MAX_UOP_REGS_ID in pycore_uop_ids.h")
    return int(match.group(1))


def core_include_flags(pyconfig_dir: str) -> list[str]:
    return [
        f"-I{pyconfig_dir}",
        f"-I{SRCDIR}",
        f"-I{SRCDIR / 'Include'}",
        f"-I{SRCDIR / 'Include' / 'internal'}",
        f"-I{SRCDIR / 'Include' / 'internal' / 'mimalloc'}",
        "-DPy_BUILD_CORE",
    ]


def host_cc() -> list[str]:
    cc = os.environ.get("CC", "").strip()
    if cc:
        return cc.split()
    return ["cc"]


# ---------------------------------------------------------------------------
# Step 1: discover the few struct field offsets the stencil needs.
# ---------------------------------------------------------------------------

PROBE_C = r"""
#include "Python.h"
#include "pycore_frame.h"
#include "pycore_interpframe_structs.h"
#include <stddef.h>
#include <stdio.h>

int main(void) {
    printf("instr_ptr %zu\n", offsetof(struct _PyInterpreterFrame, instr_ptr));
    printf("stackpointer %zu\n", offsetof(struct _PyInterpreterFrame, stackpointer));
    printf("current_executor %zu\n", offsetof(PyThreadState, current_executor));
    return 0;
}
"""


def discover_offsets(pyconfig_dir: str, cflags: list[str]) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "probe.c"
        exe = tmp / "probe"
        src.write_text(PROBE_C)
        cmd = host_cc() + core_include_flags(pyconfig_dir) + cflags + [
            str(src), "-o", str(exe)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "offsets probe failed to compile:\n"
                + " ".join(cmd) + "\n" + proc.stdout + proc.stderr)
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        if run.returncode != 0:
            raise RuntimeError("offsets probe failed to run:\n" + run.stderr)
        offsets: dict[str, int] = {}
        for line in run.stdout.splitlines():
            key, _, value = line.partition(" ")
            offsets[key] = int(value)
        return offsets


# ---------------------------------------------------------------------------
# Step 2: build the "deopt" stencil with ShivyC.
# ---------------------------------------------------------------------------

def build_deopt_stencil(offsets: dict[str, int], workdir: Path,
                        python: str) -> Stencil:
    template = (HERE / "template.c").read_text()
    src = workdir / "stencil_deopt.c"
    obj = workdir / "stencil_deopt.o"
    src.write_text(
        f"#define _JIT_INSTR_PTR_OFFSET {offsets['instr_ptr']}\n" + template)
    compile_c(src, obj, python=python)
    stencil = extract(obj, "_jit_deopt")
    if stencil.holes:
        raise RuntimeError(
            f"deopt stencil unexpectedly has holes: {stencil.holes}")
    if not stencil.code:
        raise RuntimeError("deopt stencil has empty .text")
    return stencil


# ---------------------------------------------------------------------------
# Step 3: emit the generated headers + the C shim object.
# ---------------------------------------------------------------------------

def c_byte_array(name: str, data: bytes) -> str:
    body = ", ".join(f"0x{b:02x}" for b in data)
    return f"static const unsigned char {name}[] = {{{body}}};"


def emit_stencils_header(stencil: Stencil, max_id: int, triple: str) -> str:
    lines: list[str] = []
    add = lines.append
    add("// Auto-generated by Tools/jit/build.py (ShivyC backend). Do not edit!")
    add(f"// Target: {triple}")
    add("")
    add((HERE / "abi.h").read_text().rstrip())
    add("")
    add("// Resolved at JIT time only for symbols referenced via the GOT or a")
    add("// trampoline. The deopt stencil references none, so this stays empty.")
    add("static void *const symbols_map[1] = { 0 };")
    add("")
    add(c_byte_array("deopt_code_body", stencil.code))
    add("")
    add("static void")
    add("emit_deopt(unsigned char *code, unsigned char *data,")
    add("           _PyExecutorObject *executor,")
    add("           const _PyUOpInstruction *instruction, jit_state *state)")
    add("{")
    add("    (void)data; (void)executor; (void)instruction; (void)state;")
    add("    memcpy(code, deopt_code_body, sizeof(deopt_code_body));")
    add("}")
    add("")
    add("// Every micro-op maps to the same safe deopt stencil in this PoC, so")
    add("// any trace the optimizer produces is compilable and runnable.")
    add(f"static const StencilGroup stencil_groups[{max_id + 1}] = {{")
    add(f"    [0 ... {max_id}] = {{")
    add("        .code_size = sizeof(deopt_code_body),")
    add("        .data_size = 0,")
    add("        .emit = emit_deopt,")
    add("        .trampoline_mask = {0},")
    add("        .got_mask = {0},")
    add("    },")
    add("};")
    add("")
    return "\n".join(lines)


UNWIND_INFO_H = """\
// Auto-generated by Tools/jit/build.py (ShivyC backend). Do not edit!
// Minimal DWARF CFI description for the rbp-based frame ShivyC emits.
#ifndef JIT_UNWIND_INFO_H
#define JIT_UNWIND_INFO_H

#define JIT_UNWIND_INFO_SUPPORTED 1

// Standard System V x86-64 frame-pointer based unwind rules.
#define JIT_UNWIND_CODE_ALIGNMENT_FACTOR 1
#define JIT_UNWIND_DATA_ALIGNMENT_FACTOR (-8)
#define JIT_UNWIND_RA_REG  16   /* return address column */
#define JIT_UNWIND_CFA_REG 6    /* rbp */
#define JIT_UNWIND_CFA_OFFSET 16
#define JIT_UNWIND_FP_REG  6    /* rbp */
#define JIT_UNWIND_FP_OFFSET 2  /* saved rbp at CFA-16 (2 * 8) */
#define JIT_UNWIND_RA_OFFSET 1  /* return addr at CFA-8 (1 * 8) */

#endif  // JIT_UNWIND_INFO_H
"""


def build_shim_object(triple: str, output_dir: Path, pyconfig_dir: str,
                      cflags: list[str]) -> Path:
    obj = output_dir / f"jit_shim-{triple}.o"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "jit_shim.c"
        src.write_text((HERE / "shim.c").read_text())
        cmd = host_cc() + core_include_flags(pyconfig_dir) + [
            "-D_Py_JIT", "-D_Py_TIER2=1"] + cflags + [
            "-fPIC", "-c", str(src), "-o", str(obj)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "jit shim failed to compile:\n" + " ".join(cmd) + "\n"
                + proc.stdout + proc.stderr)
    return obj


# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    triple = canonical_triple(args.target[0])
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cflags = args.cflags.split()
    python = sys.executable

    max_id = read_max_uop_regs_id()
    offsets = discover_offsets(args.pyconfig_dir, cflags)

    with tempfile.TemporaryDirectory() as tmp:
        stencil = build_deopt_stencil(offsets, Path(tmp), python)

    per_triple = output_dir / f"jit_stencils-{triple}.h"
    per_triple.write_text(emit_stencils_header(stencil, max_id, triple))

    # jit.c #includes "jit_stencils.h"; point it at the per-triple file.
    (output_dir / "jit_stencils.h").write_text(
        "// Auto-generated by Tools/jit/build.py (ShivyC backend). Do not edit!\n"
        f'#include "jit_stencils-{triple}.h"\n')

    (output_dir / "jit_unwind_info.h").write_text(UNWIND_INFO_H)
    (output_dir / f"jit_unwind_info-{triple}.h").write_text(UNWIND_INFO_H)

    build_shim_object(triple, output_dir, args.pyconfig_dir, cflags)

    print(f"[jit] ShivyC stencils generated for {triple}: "
          f"{len(stencil.code)} bytes/op, {max_id + 1} ops, no LLVM.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
