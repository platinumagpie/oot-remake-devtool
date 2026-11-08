"""The game's main executable as something Ghidra (and this tool) can read.

Two routes:
  * offline: decompress exefs/main (Switch 2 NSOs use ZBIC, a zstd variant: `pip install zbic`),
    apply the relocations at a fixed base and write an ELF with sections and symbols;
  * live: copy the loaded (already decompressed and relocated) image out of guest RAM through the
    debug server, 64 KiB at a time.
Both can write main_strings.txt (every C string in .rodata), which names flags and debug switches.
"""
import json
import os
import re
import struct
import time

BASE = 0x7100000000      # Ghidra base; the tool always talks in main+offset, so any base works
R_AARCH64_ABS64, R_AARCH64_GLOB_DAT, R_AARCH64_JUMP_SLOT, R_AARCH64_RELATIVE = 257, 1025, 1026, 1027
DT = dict(NULL=0, PLTRELSZ=2, STRTAB=5, SYMTAB=6, RELA=7, RELASZ=8, STRSZ=10, JMPREL=23,
          RELRSZ=35, RELR=36)


class Nso:
    """A parsed NSO: segments [(name, mem_off, bytes)], bss size, build id."""

    def __init__(self, path):
        b = open(path, 'rb').read()
        if b[:4] != b'NSO0':
            raise ValueError('%s is not an NSO' % path)
        self.path = path
        self.flags = struct.unpack_from('<I', b, 0xC)[0]
        self.build_id = b[0x40:0x54].hex().upper()
        self.segments = []
        for i, name in enumerate(('text', 'rodata', 'data')):
            fo, mo, size = struct.unpack_from('<III', b, 0x10 + i * 0x10)
            csize = struct.unpack_from('<I', b, 0x60 + i * 4)[0]
            raw = b[fo:fo + csize]
            if raw[:4] == b'ZBIC':
                try:
                    import zbic
                except ImportError:
                    raise RuntimeError('main is ZBIC-compressed: run "Install missing packages" '
                                       '(it adds zbic), or use "Dump main from RAM" instead')
                data = zbic.decompress(raw)
            elif self.flags & (1 << i) and csize != size:
                import lz4.block  # Switch 1 style; not used by this game
                data = lz4.block.decompress(raw, uncompressed_size=size)
            else:
                data = raw
            if len(data) != size:
                raise ValueError('%s: %s decompressed to %d bytes, expected %d' % (path, name, len(data), size))
            self.segments.append((name, mo, data))
        self.bss = struct.unpack_from('<I', b, 0x3C)[0]

    def image(self):
        """The whole module as it sits in memory, before relocation (bss zeroed)."""
        end = self.segments[2][1] + len(self.segments[2][2]) + self.bss
        img = bytearray((end + 0xFFF) & ~0xFFF)
        for _n, mo, d in self.segments:
            img[mo:mo + len(d)] = d
        return img

    def ranges(self):
        out = {n: (mo, mo + len(d)) for n, mo, d in self.segments}
        de = out['data'][1]
        out['bss'] = (de, de + self.bss)
        return out


def header_ranges(path):
    """Segment ranges (module offsets) from the NSO header alone, no decompression needed."""
    with open(path, 'rb') as f:
        h = f.read(0x100)
    out = {}
    for i, name in enumerate(('text', 'rodata', 'data')):
        fo, mo, size = struct.unpack_from('<III', h, 0x10 + i * 0x10)
        out[name] = (mo, mo + size)
    de = out['data'][1]
    out['bss'] = (de, de + struct.unpack_from('<I', h, 0x3C)[0])
    return out


# ---------------------------------------------------------------- relocation and symbols
def _dynamic(img):
    mod0 = struct.unpack_from('<I', img, 4)[0]
    if img[mod0:mod0 + 4] != b'MOD0':
        raise ValueError('no MOD0 header')
    dyn = mod0 + struct.unpack_from('<i', img, mod0 + 4)[0]
    tags = {}
    p = dyn
    while p + 16 <= len(img):
        t, v = struct.unpack_from('<qQ', img, p)
        p += 16
        if t == 0:
            break
        tags.setdefault(t, v)
    return tags


def _symbols(img, tags):
    """[(name, value, size, shndx, info)] from .dynsym; the table ends where .dynstr starts."""
    sym, strtab = tags.get(DT['SYMTAB']), tags.get(DT['STRTAB'])
    if sym is None or strtab is None:
        return []
    out = []
    end = strtab if strtab > sym else sym + 24 * 65536
    for p in range(sym, end, 24):
        nm, info, other, shndx, val, size = struct.unpack_from('<IBBHQQ', img, p)
        s = img[strtab + nm:img.index(b'\0', strtab + nm)].decode('utf-8', 'replace')
        out.append((s, val, size, shndx, info))
    return out


def relocate(img, base=BASE):
    """Applies RELATIVE (RELA and RELR) and symbol relocations to defined symbols in place.
    Returns (stats, imports) where imports = [(got_offset, symbol_name)] for unresolved imports."""
    tags = _dynamic(img)
    syms = _symbols(img, tags)
    stats = dict(relative=0, relr=0, symbol=0, imports=0)
    imports = []

    def rela(start, size):
        for p in range(start, start + size, 24):
            off, info, addend = struct.unpack_from('<QQq', img, p)
            typ, si = info & 0xFFFFFFFF, info >> 32
            if typ == R_AARCH64_RELATIVE:
                struct.pack_into('<Q', img, off, (base + addend) & 0xFFFFFFFFFFFFFFFF)
                stats['relative'] += 1
            elif typ in (R_AARCH64_ABS64, R_AARCH64_GLOB_DAT, R_AARCH64_JUMP_SLOT) and si < len(syms):
                name, val, _sz, shndx, _i = syms[si]
                if shndx:
                    struct.pack_into('<Q', img, off, (base + val + addend) & 0xFFFFFFFFFFFFFFFF)
                    stats['symbol'] += 1
                else:
                    imports.append((off, name))
                    stats['imports'] += 1
    if DT['RELA'] in tags:
        rela(tags[DT['RELA']], tags.get(DT['RELASZ'], 0))
    if DT['JMPREL'] in tags:
        rela(tags[DT['JMPREL']], tags.get(DT['PLTRELSZ'], 0))
    if DT['RELR'] in tags:
        where = 0
        p, end = tags[DT['RELR']], tags[DT['RELR']] + tags.get(DT['RELRSZ'], 0)
        while p < end:
            w = struct.unpack_from('<Q', img, p)[0]
            p += 8
            if w & 1 == 0:
                struct.pack_into('<Q', img, w, (struct.unpack_from('<Q', img, w)[0] + base) & 0xFFFFFFFFFFFFFFFF)
                stats['relr'] += 1
                where = w + 8
            else:
                for i in range(1, 64):
                    if (w >> i) & 1:
                        a = where + (i - 1) * 8
                        struct.pack_into('<Q', img, a, (struct.unpack_from('<Q', img, a)[0] + base) & 0xFFFFFFFFFFFFFFFF)
                        stats['relr'] += 1
                where += 63 * 8
    return stats, imports, syms


# ---------------------------------------------------------------- ELF writer
def write_elf(img, ranges, out_path, base=BASE, symbols=(), imports=()):
    """ET_EXEC AArch64 ELF: 3 PT_LOADs (text R-X, rodata R, data+bss RW) at base + module offset,
    section headers and a .symtab with the module's own symbols plus "<name>@got" import slots."""
    shstr = bytearray(b'\0')

    def sname(s):
        o = len(shstr)
        shstr.extend(s.encode() + b'\0')
        return o
    strtab = bytearray(b'\0')
    symtab = bytearray(b'\0' * 24)
    nsym_local = 1
    entries = []
    for name, val, size, shndx, info in symbols:
        if shndx and name:
            entries.append((name, base + val, size, info))
    for off, name in imports:
        entries.append((name + '@got', base + off, 8, 0x11))  # GLOBAL OBJECT
    sec_of = []
    for n in ('text', 'rodata', 'data', 'bss'):
        sec_of.append(ranges[n])
    for name, addr, size, info in entries:
        o = len(strtab)
        strtab.extend(name.encode('utf-8', 'replace') + b'\0')
        off = addr - base
        shndx = next((i + 1 for i, (lo, hi) in enumerate(sec_of) if lo <= off < max(hi, lo + 1)), 0xFFF1)
        symtab.extend(struct.pack('<IBBHQQ', o, info if info else 0x12, 0, shndx, addr, size))

    ph = []
    t0, t1 = ranges['text']
    r0, r1 = ranges['rodata']
    d0, d1 = ranges['data']
    b0, b1 = ranges['bss']
    hdr_size = 64 + 56 * 3
    fo = 0x1000
    blobs = []
    for (lo, hi, flags, memsz) in ((t0, t1, 5, t1 - t0), (r0, r1, 4, r1 - r0), (d0, d1, 6, b1 - d0)):
        blobs.append((fo, bytes(img[lo:hi])))
        ph.append(struct.pack('<IIQQQQQQ', 1, flags, fo, base + lo, base + lo, hi - lo, memsz, 0x1000))
        fo = (fo + (hi - lo) + 0xFFF) & ~0xFFF
    symtab_off = fo
    strtab_off = symtab_off + len(symtab)
    names = [sname(s) for s in ('', '.text', '.rodata', '.data', '.bss', '.symtab', '.strtab', '.shstrtab')]
    shstr_off = strtab_off + len(strtab)
    sh_off = (shstr_off + len(shstr) + 7) & ~7
    sh = [b'\0' * 64]
    sh.append(struct.pack('<IIQQQQIIQQ', names[1], 1, 6, base + t0, blobs[0][0], t1 - t0, 0, 0, 16, 0))
    sh.append(struct.pack('<IIQQQQIIQQ', names[2], 1, 2, base + r0, blobs[1][0], r1 - r0, 0, 0, 16, 0))
    sh.append(struct.pack('<IIQQQQIIQQ', names[3], 1, 3, base + d0, blobs[2][0], d1 - d0, 0, 0, 16, 0))
    sh.append(struct.pack('<IIQQQQIIQQ', names[4], 8, 3, base + b0, blobs[2][0] + (d1 - d0), b1 - b0, 0, 0, 16, 0))
    sh.append(struct.pack('<IIQQQQIIQQ', names[5], 2, 0, 0, symtab_off, len(symtab), 6, nsym_local, 8, 24))
    sh.append(struct.pack('<IIQQQQIIQQ', names[6], 3, 0, 0, strtab_off, len(strtab), 0, 0, 1, 0))
    sh.append(struct.pack('<IIQQQQIIQQ', names[7], 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0))
    ehdr = (b'\x7fELF' + bytes([2, 1, 1, 0]) + b'\0' * 8 +
            struct.pack('<HHIQQQIHHHHHH', 2, 183, 1, base + t0, 64, sh_off, 0, 64, 56, 3, 64, len(sh), 7))
    tmp = out_path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(ehdr)
        for p in ph:
            f.write(p)
        for off, data in blobs:
            f.seek(off)
            f.write(data)
        f.seek(symtab_off)
        f.write(symtab)
        f.write(strtab)
        f.write(shstr)
        f.seek(sh_off)
        for s in sh:
            f.write(s)
    os.replace(tmp, out_path)
    return len(entries)


# ---------------------------------------------------------------- strings
STR_RX = re.compile(rb'[\x20-\x7e]{4,200}(?=\x00)')


def strings(img, lo, hi):
    """[(offset, text)] of the NUL-terminated printable strings in img[lo:hi]."""
    return [(lo + m.start(), m.group().decode('ascii')) for m in STR_RX.finditer(img, lo, hi)]


def write_strings(img, ranges, path):
    rows = strings(img, *ranges['rodata'])
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# main .rodata strings: offset (main+) <TAB> text\n')
        for off, s in rows:
            f.write('%08X\t%s\n' % (off, s))
    return len(rows)


# ---------------------------------------------------------------- the two routes
def dump_dir(tool_dir):
    d = os.path.join(tool_dir, 'user', 'dump')
    os.makedirs(d, exist_ok=True)
    return d


def build_offline(main_path, tool_dir, base=BASE, log=print):
    """Decompress + relocate exefs/main -> user/dump/main_<id>.elf (+ main_<id>_strings.txt)."""
    t = time.time()
    nso = Nso(main_path)
    img = nso.image()
    rng = nso.ranges()
    stats, imports, syms = relocate(img, base)
    d = dump_dir(tool_dir)
    tag = nso.build_id[:16]
    elf = os.path.join(d, 'main_%s.elf' % tag)
    nsym = write_elf(img, rng, elf, base, syms, imports)
    nstr = write_strings(img, rng, os.path.join(d, 'main_%s_strings.txt' % tag))
    info = dict(build_id=nso.build_id, base=hex(base), segments={k: [hex(a), hex(b)] for k, (a, b) in rng.items()},
                relocations=stats, symbols=nsym, strings=nstr, source=main_path)
    json.dump(info, open(os.path.join(d, 'main_%s_elf.json' % tag), 'w'), indent=1)
    log('main: %d relative + %d RELR + %d symbol relocations, %d imports, %d symbols, %d strings (%.1fs)'
        % (stats['relative'], stats['relr'], stats['symbol'], stats['imports'], nsym, nstr, time.time() - t))
    return elf, info


def dump_from_ram(dbg, base, size, out_path, progress=None, chunk=0x10000):
    """Copies the loaded module out of guest RAM. Returns the number of bytes that could not be read
    (unmapped pages are filled with zeros)."""
    missing = 0
    tmp = out_path + '.tmp'
    with open(tmp, 'wb') as f:
        for off in range(0, size, chunk):
            n = min(chunk, size - off)
            try:
                data = dbg.read(base + off, n)
            except Exception:
                data = b''
                for p in range(0, n, 0x1000):   # retry page by page around unmapped pages
                    try:
                        data += dbg.read(base + off + p, min(0x1000, n - p))
                    except Exception:
                        data += b'\0' * min(0x1000, n - p)
                        missing += min(0x1000, n - p)
            f.write(data)
            if progress and (off // chunk) % 16 == 0:
                progress(off, size)
    os.replace(tmp, out_path)
    return missing
