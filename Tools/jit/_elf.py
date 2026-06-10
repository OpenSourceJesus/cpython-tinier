"""Minimal pure-Python ELF64 reader for the ShivyC-driven JIT stencil generator.

This is deliberately tiny: it understands just enough of the ELF64 relocatable
object format (as emitted by GNU ``as`` on behalf of ShivyC) to pull out the
machine code of a named function plus the relocations ("holes") that apply to
it. It exists so the JIT build has *no* dependency on LLVM, ``llvm-readobj`` or
any external object-parsing library -- the whole point of the ShivyC backend.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


# ELF constants we care about.
ELFCLASS64 = 2
ELFDATA2LSB = 1
SHT_SYMTAB = 2
SHT_RELA = 4

# x86-64 relocation types (subset). See the x86-64 psABI.
R_X86_64_NONE = 0
R_X86_64_64 = 1
R_X86_64_PC32 = 2
R_X86_64_PLT32 = 4
R_X86_64_32 = 10
R_X86_64_32S = 11

RELOC_NAMES = {
    R_X86_64_NONE: "R_X86_64_NONE",
    R_X86_64_64: "R_X86_64_64",
    R_X86_64_PC32: "R_X86_64_PC32",
    R_X86_64_PLT32: "R_X86_64_PLT32",
    R_X86_64_32: "R_X86_64_32",
    R_X86_64_32S: "R_X86_64_32S",
}


@dataclass
class Section:
    name: str
    sh_type: int
    flags: int
    offset: int
    size: int
    link: int
    info: int
    entsize: int
    addralign: int
    index: int


@dataclass
class Symbol:
    name: str
    value: int  # offset into its section
    size: int
    shndx: int
    info: int


@dataclass
class Relocation:
    offset: int  # offset into the relocated section
    symbol: str
    type: int
    addend: int

    @property
    def type_name(self) -> str:
        return RELOC_NAMES.get(self.type, f"R_X86_64_{self.type}")


@dataclass
class ELF64:
    data: bytes
    sections: list[Section] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    # section index -> list of relocations targeting it
    relocations: dict[int, list[Relocation]] = field(default_factory=dict)
    _by_name: dict[str, Section] = field(default_factory=dict)

    # -- parsing ---------------------------------------------------------

    @classmethod
    def from_path(cls, path) -> "ELF64":
        with open(path, "rb") as fh:
            return cls.from_bytes(fh.read())

    @classmethod
    def from_bytes(cls, data: bytes) -> "ELF64":
        self = cls(data=data)
        self._parse()
        return self

    def _parse(self) -> None:
        data = self.data
        if data[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        if data[4] != ELFCLASS64 or data[5] != ELFDATA2LSB:
            raise ValueError("only little-endian ELF64 is supported")
        # ELF64 header: e_shoff at 0x28 (8 bytes), e_shentsize 0x3a (2),
        # e_shnum 0x3c (2), e_shstrndx 0x3e (2).
        (e_shoff,) = struct.unpack_from("<Q", data, 0x28)
        (e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<HHH", data, 0x3A)

        raw_sections = []
        for i in range(e_shnum):
            base = e_shoff + i * e_shentsize
            (sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size,
             sh_link, sh_info, sh_addralign, sh_entsize) = struct.unpack_from(
                "<IIQQQQIIQQ", data, base)
            raw_sections.append((sh_name, sh_type, sh_flags, sh_offset, sh_size,
                                 sh_link, sh_info, sh_entsize, sh_addralign))

        # Section header string table.
        shstr_off = raw_sections[e_shstrndx][3]

        def cstr(table_off: int, rel: int) -> str:
            start = table_off + rel
            end = data.index(b"\x00", start)
            return data[start:end].decode("utf-8", "replace")

        for i, raw in enumerate(raw_sections):
            (sh_name, sh_type, sh_flags, sh_offset, sh_size,
             sh_link, sh_info, sh_entsize, sh_addralign) = raw
            sec = Section(
                name=cstr(shstr_off, sh_name),
                sh_type=sh_type,
                flags=sh_flags,
                offset=sh_offset,
                size=sh_size,
                link=sh_link,
                info=sh_info,
                entsize=sh_entsize,
                addralign=sh_addralign,
                index=i,
            )
            self.sections.append(sec)
            self._by_name[sec.name] = sec

        self._parse_symbols()
        self._parse_relocations(cstr)

    def _parse_symbols(self) -> None:
        symtab = next((s for s in self.sections if s.sh_type == SHT_SYMTAB), None)
        if symtab is None:
            return
        strtab = self.sections[symtab.link]
        data = self.data
        count = symtab.size // 24  # sizeof(Elf64_Sym)
        for i in range(count):
            base = symtab.offset + i * 24
            (st_name, st_info, st_other, st_shndx, st_value, st_size) = \
                struct.unpack_from("<IBBHQQ", data, base)
            start = strtab.offset + st_name
            end = data.index(b"\x00", start)
            name = data[start:end].decode("utf-8", "replace")
            self.symbols.append(Symbol(name=name, value=st_value, size=st_size,
                                       shndx=st_shndx, info=st_info))

    def _parse_relocations(self, cstr) -> None:
        data = self.data
        for sec in self.sections:
            if sec.sh_type != SHT_RELA:
                continue
            target = sec.info  # section the relocations apply to
            symtab = self.sections[sec.link]
            strtab = self.sections[symtab.link]
            relocs: list[Relocation] = []
            count = sec.size // 24  # sizeof(Elf64_Rela)
            for i in range(count):
                base = sec.offset + i * 24
                (r_offset, r_info, r_addend) = struct.unpack_from("<QQq", data, base)
                r_sym = r_info >> 32
                r_type = r_info & 0xFFFFFFFF
                # Resolve the symbol name.
                sym_base = symtab.offset + r_sym * 24
                (st_name,) = struct.unpack_from("<I", data, sym_base)
                nstart = strtab.offset + st_name
                nend = data.index(b"\x00", nstart)
                sym_name = data[nstart:nend].decode("utf-8", "replace")
                relocs.append(Relocation(offset=r_offset, symbol=sym_name,
                                         type=r_type, addend=r_addend))
            self.relocations.setdefault(target, []).extend(relocs)

    # -- queries ---------------------------------------------------------

    def section(self, name: str) -> Section | None:
        return self._by_name.get(name)

    def section_bytes(self, name: str) -> bytes:
        sec = self.section(name)
        if sec is None:
            return b""
        return self.data[sec.offset:sec.offset + sec.size]

    def symbol(self, name: str) -> Symbol | None:
        for sym in self.symbols:
            if sym.name == name:
                return sym
        return None

    def text_relocations(self) -> list[Relocation]:
        """Relocations that apply to the .text section."""
        text = self.section(".text")
        if text is None:
            return []
        return self.relocations.get(text.index, [])
