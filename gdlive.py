"""Live GameData: the game's own flag tables in RAM, read and written while you play.

How the game keeps them (found Nov 2026 from the v0.4.1 probe and the code of main):
  main .bss holds the gmd::GameDataMgr pointer (sInstance).
  The manager holds one 0x60-byte table per GameData type, at mgr + 8 + 0x60 * k, in this order
  (the same as TotK): Bool, BoolArray, Int, IntArray, Float, FloatArray, Enum, ...
  Each table: +0x08 u32 count, +0x10 pointer to the records, +0x18 hash -> index map.
  Records are in GameDataList definition order (index i = i-th entry of that type in the list).
  A record is a small object: +0x00 vtable, +0x08 reset type, then default value, live value and
  a second copy (the value as of the last load/save). Writing the live value is what the game sees.
"""
import struct
import threading
import time

TYPE_ORDER = ['Bool', 'BoolArray', 'Int', 'IntArray', 'Float', 'FloatArray', 'Enum', 'EnumArray',
              'Vector2', 'Vector2Array', 'Vector3', 'Vector3Array', 'String16', 'String16Array',
              'String32', 'String32Array', 'String64', 'String64Array', 'Binary', 'BinaryArray',
              'UInt', 'UIntArray', 'Int64', 'Int64Array', 'UInt64', 'UInt64Array',
              'WString16', 'WString16Array', 'WString32', 'WString32Array', 'WString64', 'WString64Array']
TABLE0, TABLE_STRIDE = 8, 0x60          # table k at mgr + TABLE0 + TABLE_STRIDE * k
T_COUNT, T_PTR = 0x08, 0x10

# Per type: record size, (default, value, copy) offsets, value codec, value size.
# Read off the record initialisers in main (each type's allocator zeroes exactly these fields).
LAYOUT = {
    'Bool':      (0x18, (0x15, 0x16, 0x17), '<B', 1),
    'Int':       (0x28, (0x18, 0x1C, 0x20), '<i', 4),
    'UInt':      (0x28, (0x18, 0x1C, 0x20), '<I', 4),
    'Float':     (0x28, (0x18, 0x1C, 0x20), '<f', 4),
    'Enum':      (0x38, (0x18, 0x1C, 0x20), '<I', 4),     # checked against GameDataList defaults on attach
    'Vector3':   (0x40, (0x18, 0x24, 0x30), '<3f', 12),
    'Int64':     (0x30, (0x18, 0x20, 0x28), '<q', 8),
    'UInt64':    (0x30, (0x18, 0x20, 0x28), '<Q', 8),
    'String16':  (0x78, (0x24, 0x44, 0x64), 's', 16),     # {char *p; u32 size; char buf[N]} x3
    'String32':  (0xA8, (0x24, 0x54, 0x84), 's', 32),
    'String64':  (0x108, (0x24, 0x74, 0xC4), 's', 64),
    'WString16': (0xA8, (0x24, 0x54, 0x84), 'w', 16),
}
# Other offsets tried when the expected default offset does not match the GameDataList defaults.
ALT_DEFAULT = {'Bool': (0x14, 0x15, 0x16), 'Enum': (0x18, 0x1C, 0x20, 0x28)}

# Per game build (first 16 hex digits of the build ID): main offsets.
BUILDS = {
    'E0F3CCFAF50065AE': {
        'version': '1.1.1',
        'sinstance': 0x4E842A8,        # .bss: gmd::GameDataMgr *  (GOT 0x4C7E180)
        'vtables': {'Bool': 0x4C20658, 'Int': 0x4C20718, 'Float': 0x4C207D8, 'Enum': 0x4C20898,
                    'Vector3': 0x4C20A18, 'String16': 0x4C20AD8, 'String32': 0x4C20B98,
                    'String64': 0x4C20C58, 'UInt': 0x4C20DD8, 'Int64': 0x4C20E98, 'UInt64': 0x4C20F58,
                    'WString16': 0x4C21068},
    },
}
LIVE_TYPES = [t for t in TYPE_ORDER if t in LAYOUT]


class LiveError(Exception):
    pass


class LiveEntry:
    """Looks like savefile.Entry, so the World state tab can show live values with the same code."""
    __slots__ = ('type', 'hash', 'index')
    is_array = False

    def __init__(self, type_, h, index):
        self.type, self.hash, self.index = type_, h, index

    @property
    def base(self):
        return self.type


def decode(typ, raw):
    size, _offs, fmt, n = LAYOUT[typ]
    if fmt == 's':
        return raw[:n].split(b'\0', 1)[0].decode('utf-8', 'replace')
    if fmt == 'w':
        b = raw[:n * 2]
        for i in range(0, len(b) - 1, 2):
            if b[i:i + 2] == b'\0\0':
                b = b[:i]
                break
        return b.decode('utf-16-le', 'replace')
    v = struct.unpack(fmt, raw[:n])
    if typ == 'Bool':
        return bool(v[0])
    if typ == 'Vector3':
        return tuple(v)
    return v[0]


def encode(typ, value):
    size, _offs, fmt, n = LAYOUT[typ]
    if fmt == 's':
        b = str(value).encode('utf-8')
        if len(b) >= n:
            raise ValueError('%s holds at most %d bytes' % (typ, n - 1))
        return b + b'\0'
    if fmt == 'w':
        b = str(value).encode('utf-16-le')
        if len(b) >= n * 2:
            raise ValueError('%s holds at most %d characters' % (typ, n - 1))
        return b + b'\0\0'
    if typ == 'Bool':
        return struct.pack('<B', 1 if value else 0)
    if typ == 'Vector3':
        return struct.pack(fmt, *[float(x) for x in value])
    if typ == 'Float':
        return struct.pack(fmt, float(value))
    return struct.pack(fmt, int(value))


def _default_raw(typ, d):
    """The GameDataList default as raw bytes, or None when it can't be compared."""
    dv = d.default
    try:
        if typ == 'Bool':
            return encode(typ, bool(dv))
        if typ in ('Int', 'UInt', 'Enum', 'Int64', 'UInt64'):
            v = int(dv or 0)
            fmt = LAYOUT[typ][2]
            mask = (1 << (8 * LAYOUT[typ][3])) - 1
            return struct.pack(fmt.replace('i', 'I').replace('q', 'Q'), v & mask)
        if typ == 'Float':
            return encode(typ, float(dv or 0))
        if typ.startswith('String') or typ.startswith('WString'):
            return encode(typ, dv or '') if isinstance(dv, (str, type(None))) else None
    except (ValueError, struct.error, TypeError):
        return None
    return None


class LiveGameData:
    """attach() once per boot (cheap: a few reads), then snapshot()/read()/write() as often as needed."""

    def __init__(self, dbg, host, main_base, build_id, defs):
        self.dbg, self.host, self.main_base = dbg, host, main_base
        self.build = BUILDS.get((build_id or '')[:16].upper())
        self.defs = defs
        self.tables = {}       # type -> dict(ptr, count, size, offs, fmt, n)
        self.problems = {}     # type -> why it is not live
        self.entries = []      # LiveEntry for every live record
        self.by_hash = {}
        self.mgr = None
        self.vals = {}         # hash -> last decoded value
        self.raw = {}          # type -> last raw table bytes
        self.of_type = {}      # type -> [LiveEntry]
        self.lock = threading.Lock()
        self.wrote_at = {}     # type -> time of the tool's last write (a poll that overlaps it is redone)

    # ------------------------------------------------------------ memory
    def _read(self, addr, n):
        if self.host is not None:
            try:
                return self.host.read(addr, n)
            except OSError:
                pass
        return self.dbg.read(addr, n)

    def _q(self, addr):
        return struct.unpack('<Q', self._read(addr, 8))[0]

    # ------------------------------------------------------------ attach
    def attach(self):
        if not self.build:
            raise LiveError('this game build is not mapped yet (known: %s). Run the v0.4 probes on it and '
                            'send me the report.' % ', '.join(BUILDS))
        self.mgr = self._q(self.main_base + self.build['sinstance'])
        if not self.mgr or self.mgr & 7:
            raise LiveError('GameData manager pointer is %#x: the game has not set it up yet. Load a save, '
                            'then try again.' % self.mgr)
        by_type = {}
        for h, d in self.defs.items():
            by_type.setdefault(d.type, []).append(h)
        self.tables, self.problems, self.entries, self.by_hash, self.of_type = {}, {}, [], {}, {}
        for typ in LIVE_TYPES:
            hs = by_type.get(typ, [])
            if not hs:
                continue
            k = TYPE_ORDER.index(typ)
            t = self.mgr + TABLE0 + TABLE_STRIDE * k
            hdr = self._read(t + T_COUNT, 16)
            count, ptr = struct.unpack_from('<I', hdr, 0)[0], struct.unpack_from('<Q', hdr, 8)[0]
            size, offs, fmt, n = LAYOUT[typ]
            if count != len(hs):
                self.problems[typ] = 'table has %d records, GameDataList has %d' % (count, len(hs))
                continue
            if not ptr or ptr & 3:
                self.problems[typ] = 'record pointer is %#x' % ptr
                continue
            vt = self.build['vtables'].get(typ)
            if vt is not None:
                got = self._q(ptr)
                if got != self.main_base + vt:
                    self.problems[typ] = 'first record vtable %#x, expected main+%#x' % (got, vt)
                    continue
            blob = self._read(ptr, count * size)
            offs = self._check_defaults(typ, hs, blob, size, offs, n)
            if offs is None:
                continue
            self.tables[typ] = {'ptr': ptr, 'count': count, 'size': size, 'offs': offs, 'fmt': fmt,
                                'n': n * (2 if fmt == 'w' else 1), 'table': t}
            self.raw[typ] = blob
            lst = self.of_type[typ] = []
            for i, h in enumerate(hs):
                e = LiveEntry(typ, h, i)
                self.entries.append(e)
                lst.append(e)
                self.by_hash[h] = e
        if not self.tables:
            raise LiveError('no GameData table checked out: %s' % '; '.join(
                '%s: %s' % kv for kv in self.problems.items()))
        self._decode_all()
        return self

    def _check_defaults(self, typ, hs, blob, size, offs, n):
        """The default field must hold the GameDataList defaults. Returns the (default, value, copy) offsets."""
        n_bytes = n * (2 if LAYOUT[typ][2] == 'w' else 1)
        wants = [(i, _default_raw(typ, self.defs[h])) for i, h in enumerate(hs)]
        wants = [(i, w) for i, w in wants if w is not None]
        if not wants:
            return offs
        step = offs[1] - offs[0]

        def score(o):
            ok = 0
            for i, w in wants:
                got = blob[i * size + o:i * size + o + len(w)]
                if typ.startswith('String') or typ.startswith('WString'):
                    got = blob[i * size + o:i * size + o + n_bytes]
                    ok += got.startswith(w)
                else:
                    ok += got == w
            return ok / len(wants)
        best = (score(offs[0]), offs[0])
        if best[0] < 0.95:
            for o in ALT_DEFAULT.get(typ, ()):
                if o != offs[0]:
                    best = max(best, (score(o), o))
        if best[0] < 0.95:
            self.problems[typ] = 'default values do not line up (best %.0f%% at +%#x)' % (best[0] * 100, best[1])
            return None
        o = best[1]
        return (o, o + step, o + 2 * step)

    # ------------------------------------------------------------ reading
    def _field(self, typ, blob, i, which=1):
        t = self.tables[typ]
        o = i * t['size'] + t['offs'][which]
        return blob[o:o + t['n']]

    def _decode_all(self):
        for e in self.entries:
            self.vals[e.hash] = decode(e.type, self._field(e.type, self.raw[e.type], e.index))

    def snapshot(self, types=None):
        """Re-reads the tables. Returns [(entry, old, new)] for every live value that changed."""
        changes = []
        for typ, t in self.tables.items():
            if types and typ not in types:
                continue
            t0 = time.time()
            blob = self._read(t['ptr'], t['count'] * t['size'])
            with self.lock:
                if self.wrote_at.get(typ, 0) >= t0:
                    continue      # the tool wrote while this was being read: take the next poll instead
                old = self.raw.get(typ)
                self.raw[typ] = blob
                if old == blob:
                    continue
                size, o, n = t['size'], t['offs'][1], t['n']
                for e in (self.of_type[typ] if old is not None else ()):
                    a = e.index * size + o
                    if blob[a:a + n] != old[a:a + n]:
                        nv = decode(typ, blob[a:a + n])
                        ov = self.vals.get(e.hash)
                        self.vals[e.hash] = nv
                        if nv != ov:      # text: bytes after the terminator can change without the value changing
                            changes.append((e, ov, nv))
        return changes

    def get(self, e):
        if isinstance(e, int):
            e = self.by_hash[e]
        return self.vals.get(e.hash)

    def read(self, h):
        """Fresh read of one value straight from the game."""
        e = self.by_hash[h]
        t = self.tables[e.type]
        nv = decode(e.type, self._read(self.addr(h), t['n']))
        self.vals[h] = nv
        return nv

    def addr(self, h, which=1):
        e = self.by_hash[h]
        t = self.tables[e.type]
        return t['ptr'] + e.index * t['size'] + t['offs'][which]

    def default(self, h):
        e = self.by_hash[h]
        return decode(e.type, self._field(e.type, self.raw[e.type], e.index, 0))

    # ------------------------------------------------------------ writing
    dirty = False       # the World state tab asks; live edits are never pending

    def set(self, e, value):
        """Same call as savefile.SaveFile.set, but goes straight into the game."""
        return self.write(e.hash, value)

    def write(self, h, value, also_copy=False):
        """Writes the live value (and optionally the copy). Returns the value read back."""
        if self.host is None:
            raise LiveError('writes need host memory access (Connect found no fastmem arena)')
        e = self.by_hash[h]
        t = self.tables[e.type]
        data = encode(e.type, value)
        with self.lock:
            self.host.write(self.addr(h), data)
            if also_copy:
                self.host.write(self.addr(h, 2), data)
            got = self._read(self.addr(h), t['n'])
            # keep the shadow copy in step, so the flag log shows the game's changes and not ours
            o = e.index * t['size'] + t['offs'][1]
            raw = bytearray(self.raw[e.type])
            raw[o:o + t['n']] = got
            self.raw[e.type] = bytes(raw)
            self.wrote_at[e.type] = time.time()
            nv = decode(e.type, got)
            self.vals[h] = nv
        return nv


def fmt_datetime(v):
    """World_CurrentDateTime: day count in the high 32 bits, seconds of the day in the low 32."""
    if not isinstance(v, int):
        return '?'
    day, sec = v >> 32, v & 0xFFFFFFFF
    return 'day %d %02d:%02d' % (day, (sec // 3600) % 24, (sec // 60) % 60)
