"""SARC archive reader/writer (copied from tools\\fogmod\\formats.py, v0.8)."""
import struct


# -- SARC ------------------------------------------------------------------
def sarc_read(data):
    """Return list of (name, bytes) in node order."""
    assert data[:4] == b'SARC', data[:4]
    hl, bom = struct.unpack_from('<HH', data, 4)
    assert bom == 0xFEFF, 'big-endian SARC not supported'
    doff = struct.unpack_from('<I', data, 0xC)[0]
    p = hl
    assert data[p:p + 4] == b'SFAT'
    n = struct.unpack_from('<H', data, p + 6)[0]
    p += 12
    nodes = [struct.unpack_from('<IIII', data, p + 16 * i) for i in range(n)]
    p += 16 * n
    assert data[p:p + 4] == b'SFNT'
    names = p + 8
    out = []
    for h, attr, s, e in nodes:
        no = (attr & 0xFFFFFF) * 4
        nm = data[names + no:data.index(b'\0', names + no)].decode()
        out.append((nm, bytes(data[doff + s:doff + e])))
    return out


def sarc_hash(name, key=0x65):
    h = 0
    for c in name.encode():
        if c >= 0x80:
            c -= 0x100
        h = (h * key + c) & 0xFFFFFFFF
    return h


def sarc_write(files, align=8):
    """files: list of (name, bytes). Nodes are sorted by name hash."""
    files = sorted(files, key=lambda f: sarc_hash(f[0]))
    sfnt = bytearray()
    name_offs = []
    for nm, _ in files:
        name_offs.append(len(sfnt))
        sfnt += nm.encode() + b'\0'
        while len(sfnt) % 4:
            sfnt += b'\0'
    hdr_len = 0x14 + 0xC + 16 * len(files) + 8 + len(sfnt)
    doff = (hdr_len + align - 1) // align * align
    data = bytearray()
    nodes = bytearray()
    for (nm, b), no in zip(files, name_offs):
        while len(data) % align:
            data += b'\0'
        s = len(data)
        data += b
        nodes += struct.pack('<IIII', sarc_hash(nm), 0x01000000 | (no // 4), s, len(data))
    out = bytearray()
    out += b'SARC' + struct.pack('<HHIIHH', 0x14, 0xFEFF, doff + len(data), doff, 0x100, 0)
    out += b'SFAT' + struct.pack('<HHI', 0xC, len(files), 0x65) + nodes
    out += b'SFNT' + struct.pack('<HH', 8, 0) + sfnt
    out += b'\0' * (doff - len(out))
    out += data
    return bytes(out)
