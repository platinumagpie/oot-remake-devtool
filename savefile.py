"""Reader/writer for the OoT remake's GameData save files (progress.sav, option.sav, ...).

Layout (same as Tears of the Kingdom):
  0x00 u32 magic 0x01020304   0x04 u32 GameDataList revision   0x08 u32 data offset
  0x20 .. data offset: (u32 hash, u32 value) pairs. A pair with hash 0 starts a type section;
       its value is the type index (gamedata.SAVE_TYPES). Small scalars (Bool, Int, Float,
       Enum, UInt) are stored inline in `value`; every other type stores an offset to its data.
  Arrays at their offset: u32 count, then elements (BoolArray: count, then bit words).
Edits here never change the file size or layout: values are overwritten in place.
"""
import os
import struct
import shutil
import time

from gamedata import SAVE_TYPES

MAGIC = 0x01020304
INLINE = {'Bool', 'Int', 'Float', 'Enum', 'UInt'}
ELEM = {  # element struct per base type
    'Bool': None, 'Int': '<i', 'Float': '<f', 'Enum': '<I', 'UInt': '<I',
    'Vector2': '<2f', 'Vector3': '<3f', 'Int64': '<q', 'UInt64': '<Q',
    'String16': 16, 'String32': 32, 'String64': 64,
    'WString16': 32, 'WString32': 64, 'WString64': 128,
}
READ_ONLY = {'Binary', 'BinaryArray', 'Bool64bitKey'}


class Entry:
    __slots__ = ('type', 'hash', 'pos')

    def __init__(self, type_, h, pos):
        self.type, self.hash, self.pos = type_, h, pos

    @property
    def base(self):
        return self.type[:-5] if self.type.endswith('Array') else self.type

    @property
    def is_array(self):
        return self.type.endswith('Array')


class SaveFile:
    def __init__(self, path, data=None):
        self.path = path
        self.data = bytearray(open(path, 'rb').read() if data is None else data)
        magic, self.revision, self.data_offset = struct.unpack_from('<III', self.data, 0)
        if magic != MAGIC:
            raise ValueError('%s: not a GameData save (magic %08X)' % (path, magic))
        self.entries = []
        self.by_hash = {}
        cur = None
        p = 0x20
        while p + 8 <= self.data_offset:
            h, v = struct.unpack_from('<II', self.data, p)
            if h == 0:
                cur = SAVE_TYPES[v] if v < len(SAVE_TYPES) else 'Type%d' % v
            elif cur:
                e = Entry(cur, h, p)
                self.entries.append(e)
                self.by_hash[h] = e
            p += 8
        self.dirty = False

    # ---------------------------------------------------------------- read
    def raw(self, e):
        return struct.unpack_from('<I', self.data, e.pos + 4)[0]

    def _scalar(self, base, off):
        f = ELEM[base]
        if isinstance(f, int):
            b = bytes(self.data[off:off + f])
            if base.startswith('W'):
                return b.decode('utf-16-le', 'replace').split('\0')[0]
            return b.split(b'\0')[0].decode('utf-8', 'replace')
        v = struct.unpack_from(f, self.data, off)
        return v if len(v) > 1 else v[0]

    def _size(self, base):
        f = ELEM[base]
        return f if isinstance(f, int) else struct.calcsize(f)

    def get(self, e):
        t, base = e.type, e.base
        if t in INLINE:
            raw = self.raw(e)
            if t == 'Bool':
                return bool(raw)
            if t == 'Int':
                return struct.unpack('<i', struct.pack('<I', raw))[0]
            if t == 'Float':
                return struct.unpack('<f', struct.pack('<I', raw))[0]
            return raw
        off = self.raw(e)
        if t == 'Binary':
            n = struct.unpack_from('<I', self.data, off)[0]
            return bytes(self.data[off + 4:off + 4 + min(n, 4096)])
        if t == 'BinaryArray':
            return bytes(self.data[off:off + 64])
        if t == 'Bool64bitKey':
            n = (len(self.data) - off) // 8
            return list(struct.unpack_from('<%dQ' % n, self.data, off))
        if not e.is_array:
            return self._scalar(base, off)
        n = struct.unpack_from('<I', self.data, off)[0]
        if base == 'Bool':
            words = struct.unpack_from('<%dI' % ((n + 31) // 32), self.data, off + 4)
            return [bool(words[i // 32] >> (i % 32) & 1) for i in range(n)]
        sz = self._size(base)
        return [self._scalar(base, off + 4 + i * sz) for i in range(n)]

    # ---------------------------------------------------------------- write
    def _put_scalar(self, base, off, v):
        f = ELEM[base]
        if isinstance(f, int):
            wide = base.startswith('W')
            term = b'\0\0' if wide else b'\0'
            b = str(v).encode('utf-16-le' if wide else 'utf-8')[:f - len(term)]
            if self._scalar(base, off) != str(v):  # like the game: write text + terminator only
                self.data[off:off + len(b) + len(term)] = b + term
            return
        if isinstance(v, (tuple, list)):
            struct.pack_into(f, self.data, off, *v)
        else:
            struct.pack_into(f, self.data, off, v)

    def set(self, e, value, index=None):
        """Set a value. For arrays pass index= to set one element, or a full list."""
        t, base = e.type, e.base
        if t in READ_ONLY:
            raise ValueError('%s values are read-only in this version' % t)
        if t in INLINE:
            if t == 'Bool':
                raw = 1 if value else 0
            elif t == 'Int':
                raw = struct.unpack('<I', struct.pack('<i', int(value)))[0]
            elif t == 'Float':
                raw = struct.unpack('<I', struct.pack('<f', float(value)))[0]
            else:
                raw = int(value) & 0xFFFFFFFF
            struct.pack_into('<I', self.data, e.pos + 4, raw)
        elif not e.is_array:
            self._put_scalar(base, self.raw(e), value)
        else:
            off = self.raw(e)
            n = struct.unpack_from('<I', self.data, off)[0]
            items = {index: value} if index is not None else dict(enumerate(value))
            for i, v in items.items():
                if not 0 <= i < n:
                    raise IndexError('index %d out of range (size %d)' % (i, n))
                if base == 'Bool':
                    wo = off + 4 + (i // 32) * 4
                    w = struct.unpack_from('<I', self.data, wo)[0]
                    w = (w | (1 << (i % 32))) if v else (w & ~(1 << (i % 32)))
                    struct.pack_into('<I', self.data, wo, w & 0xFFFFFFFF)
                else:
                    self._put_scalar(base, off + 4 + i * self._size(base), v)
        self.dirty = True

    def save(self, backup_root=None):
        """Write back in place. Copies the original to backup_root/<timestamp>/ first."""
        if backup_root:
            stamp = time.strftime('%Y-%m-%d_%H%M%S')
            parent = os.path.basename(os.path.dirname(self.path))
            dst = os.path.join(backup_root, stamp, parent)
            os.makedirs(dst, exist_ok=True)
            shutil.copy2(self.path, os.path.join(dst, os.path.basename(self.path)))
        tmp = self.path + '.devtool.tmp'
        with open(tmp, 'wb') as f:
            f.write(self.data)
        os.replace(tmp, self.path)
        self.dirty = False


def diff(a, b):
    """Entries whose value differs between two loaded saves: [(hash, type, old, new)]."""
    out = []
    for e in b.entries:
        ea = a.by_hash.get(e.hash)
        if ea is None or ea.type != e.type:
            continue
        va, vb = a.get(ea), b.get(e)
        if va != vb:
            out.append((e.hash, e.type, va, vb))
    return out
