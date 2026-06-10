"""Drive ShivyC to turn a small C stencil into machine code + holes.

A "stencil" is the position-independent machine code implementing one micro-op,
together with a list of "holes": spots that must be patched at JIT time with a
runtime value (a continuation address, an operand, a symbol address, ...).

Upstream CPython produces stencils by compiling ``Tools/jit/template.c`` once
per opcode with clang/LLVM. ShivyC cannot ingest CPython's real C (it implements
only a subset of C11 and cannot parse ``Python.h``), so instead we hand-write
tiny, self-contained C stencils that ShivyC *can* compile, and reconstruct the
holes from the ELF relocations ShivyC emits via GNU ``as``.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

from _elf import (
    ELF64,
    R_X86_64_32,
    R_X86_64_32S,
    R_X86_64_64,
    R_X86_64_PC32,
    R_X86_64_PLT32,
)

VENDORED_SHIVYC = Path(__file__).resolve().parent / "_shivyc" / "ShivyC"


@dataclasses.dataclass
class Hole:
    offset: int       # byte offset into the stencil code
    kind: str         # how jit.c should patch it (patch_32r/patch_64/...)
    symbol: str       # the symbol the hole refers to (a _JIT_* name)
    addend: int


@dataclasses.dataclass
class Stencil:
    name: str
    code: bytes
    holes: list[Hole] = dataclasses.field(default_factory=list)
    data: bytes = b""


# Map an ELF relocation type to the jit.c patch helper that applies it.
_RELOC_TO_KIND = {
    R_X86_64_64: "patch_64",
    R_X86_64_32: "patch_32",
    R_X86_64_32S: "patch_32",
    R_X86_64_PC32: "patch_32r",
    R_X86_64_PLT32: "patch_32r",
}


class ShivyCError(RuntimeError):
    pass


def compile_c(src: Path, obj: Path, *, python: str | None = None) -> None:
    """Compile a C file to an ELF object with the vendored ShivyC (no LLVM)."""
    python = python or sys.executable
    runner = (
        "import sys;"
        f"sys.argv=['shivyc','-c',{str(src)!r},'-o',{str(obj)!r}];"
        "from shivyc.main import main;"
        "rc=main();"
        "sys.exit(rc or 0)"
    )
    env = {"PYTHONPATH": str(VENDORED_SHIVYC)}
    import os

    full_env = dict(os.environ)
    full_env.update(env)
    proc = subprocess.run(
        [python, "-c", runner],
        capture_output=True,
        text=True,
        env=full_env,
    )
    if proc.returncode != 0 or not obj.exists():
        raise ShivyCError(
            f"ShivyC failed to compile {src}:\n{proc.stdout}\n{proc.stderr}"
        )


def extract(obj: Path, func: str) -> Stencil:
    """Extract the stencil for ``func`` from a compiled object file.

    Each stencil source compiles to a single function, so the whole ``.text``
    section is the stencil body. Relocations against ``.text`` become holes.
    """
    elf = ELF64.from_path(obj)
    code = elf.section_bytes(".text")
    holes: list[Hole] = []
    for reloc in elf.text_relocations():
        kind = _RELOC_TO_KIND.get(reloc.type)
        if kind is None:
            raise ShivyCError(
                f"unsupported relocation {reloc.type_name} in {func}"
            )
        holes.append(Hole(offset=reloc.offset, kind=kind,
                          symbol=reloc.symbol, addend=reloc.addend))
    holes.sort(key=lambda h: h.offset)
    return Stencil(name=func, code=code, holes=holes,
                   data=elf.section_bytes(".data"))
