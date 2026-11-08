"""BYML (v2-v7) reader/writer for the TotK-engine OoT Switch 2 remake.

Typed tree model (keeps every node's exact BYML type so a round trip is lossless):
    BDict  - dict subclass; .t is T_DICT (string keys) or T_HASH32/T_HASH64 (int keys)
    BArr   - list subclass
    Val    - scalar: Val(type, value)

    root = Byml(data).tree()
    root['Key'] = Val(T_FLOAT, 1.0)
    data = write(root, version=7)

Byml.flat() also lists (path, type, value, file_offset) for quick dumps.
"""
import struct

T_HASH32, T_HASH64 = 0x20, 0x21
T_STR, T_BIN, T_BINA = 0xA0, 0xA1, 0xA2
T_ARR, T_DICT, T_STRTAB = 0xC0, 0xC1, 0xC2
T_BOOL, T_INT, T_FLOAT, T_UINT = 0xD0, 0xD1, 0xD2, 0xD3
T_I64, T_U64, T_F64, T_NULL = 0xD4, 0xD5, 0xD6, 0xFF
CONTAINERS = (T_ARR, T_DICT, T_HASH32, T_HASH64)
WIDE = (T_I64, T_U64, T_F64)
TYPENAME = {T_STR: 'str', T_BIN: 'bin', T_BINA: 'bin', T_BOOL: 'bool', T_INT: 'int',
            T_FLOAT: 'float', T_UINT: 'uint', T_I64: 'i64', T_U64: 'u64', T_F64: 'f64',
            T_NULL: 'null', T_ARR: 'array', T_DICT: 'dict', T_HASH32: 'hash32', T_HASH64: 'hash64'}


class Val:
    __slots__ = ('t', 'v')

    def __init__(self, t, v):
        self.t, self.v = t, v

    def __eq__(self, o):
        return isinstance(o, Val) and self.t == o.t and self.v == o.v

    def __repr__(self):
        return 'Val(%s,%r)' % (TYPENAME.get(self.t, hex(self.t)), self.v)


class BDict(dict):
    def __init__(self, *a, t=T_DICT, **k):
        super().__init__(*a, **k)
        self.t = t

    def __eq__(self, o):
        return isinstance(o, BDict) and self.t == o.t and dict.__eq__(self, o)

    __hash__ = None


class BArr(list):
    t = T_ARR


def F(x):
    return Val(T_FLOAT, float(x))


def B(x):
    return Val(T_BOOL, bool(x))


class Byml:
    def __init__(self, data):
        self.data = bytearray(data)
        m = bytes(self.data[:2])
        if m == b'YB':
            self.e = '<'
        elif m == b'BY':
            self.e = '>'
        else:
            raise ValueError('not BYML: %r' % m)
        self.version, = self._u('H', 2)
        hk, st, root = self._u('III', 4)
        self.keys = self._strtab(hk) if hk else []
        self.strs = self._strtab(st) if st else []
        self.root_off = root

    def _u(self, fmt, off):
        return struct.unpack_from(self.e + fmt, self.data, off)

    def _u24(self, off):
        return int.from_bytes(self.data[off:off + 3], 'little' if self.e == '<' else 'big')

    def _strtab(self, off):
        assert self.data[off] == T_STRTAB, hex(self.data[off])
        n = self._u24(off + 1)
        offs = self._u('%dI' % (n + 1), off + 4)
        return [bytes(self.data[off + offs[i]:self.data.index(0, off + offs[i])]).decode('utf-8')
                for i in range(n)]

    def _children(self, off):
        t = self.data[off]
        n = self._u24(off + 1)
        if t == T_ARR:
            types = self.data[off + 4: off + 4 + n]
            vbase = off + 4 + ((n + 3) & ~3)
            for i in range(n):
                yield i, types[i], vbase + 4 * i
        elif t == T_DICT:
            for i in range(n):
                p = off + 4 + 8 * i
                yield self.keys[self._u24(p)], self.data[p + 3], p + 4
        elif t in (T_HASH32, T_HASH64):
            hs = 4 if t == T_HASH32 else 8
            tbase = off + 4 + (hs + 4) * n
            for i in range(n):
                p = off + 4 + (hs + 4) * i
                yield self._u('I' if hs == 4 else 'Q', p)[0], self.data[tbase + i], p + hs
        else:
            raise ValueError('unknown container type 0x%02x at 0x%x' % (t, off))

    def _scalar(self, t, slot):
        if t == T_STR:
            return self.strs[self._u('I', slot)[0]], slot
        if t == T_BOOL:
            return bool(self._u('I', slot)[0]), slot
        if t == T_INT:
            return self._u('i', slot)[0], slot
        if t == T_UINT:
            return self._u('I', slot)[0], slot
        if t == T_FLOAT:
            return self._u('f', slot)[0], slot
        if t in WIDE:
            o = self._u('I', slot)[0]
            return self._u({T_I64: 'q', T_U64: 'Q', T_F64: 'd'}[t], o)[0], o
        if t == T_NULL:
            return None, slot
        if t == T_BIN:
            o = self._u('I', slot)[0]
            n = self._u('I', o)[0]
            return bytes(self.data[o + 4:o + 4 + n]), o
        if t == T_BINA:
            o = self._u('I', slot)[0]
            n, al = self._u('II', o)
            return (bytes(self.data[o + 8:o + 8 + n]), al), o
        raise ValueError('unknown scalar type 0x%02x' % t)

    # -- tree ------------------------------------------------------------
    def tree(self, off=None):
        if off is None:
            if not self.root_off:
                return None
            off = self.root_off
        t = self.data[off]
        if t == T_ARR:
            res = BArr()
        else:
            res = BDict(t=t)
        for k, ct, slot in self._children(off):
            v = self.tree(self._u('I', slot)[0]) if ct in CONTAINERS else Val(ct, self._scalar(ct, slot)[0])
            if t == T_ARR:
                res.append(v)
            else:
                res[k] = v
        return res

    def flat(self):
        out = []
        if self.root_off:
            self._walk(self.root_off, '', out)
        return out

    def _walk(self, off, path, out):
        out.append((path, self.data[off], None, off))
        for k, t, slot in self._children(off):
            ks = ('#%08x' % k) if isinstance(k, int) and self.data[off] != T_ARR else str(k)
            p = '%s/%s' % (path, ks) if path else ks
            if t in CONTAINERS:
                self._walk(self._u('I', slot)[0], p, out)
            else:
                v, o = self._scalar(t, slot)
                out.append((p, t, v, o))


# -- writer --------------------------------------------------------------
def _key(s):
    return s.encode('utf-8')


def _collect(node, keys, strs):
    if isinstance(node, BDict):
        for k, v in node.items():
            if node.t == T_DICT:
                keys.add(k)
            _collect(v, keys, strs)
    elif isinstance(node, BArr):
        for v in node:
            _collect(v, keys, strs)
    elif isinstance(node, Val):
        if node.t == T_STR:
            strs.add(node.v)
    else:
        raise TypeError('bad node %r' % (node,))


def _strtab_bytes(strings, e):
    n = len(strings)
    hdr = 4 + 4 * (n + 1)
    offs, body = [], bytearray()
    for s in strings:
        offs.append(hdr + len(body))
        body += s.encode('utf-8') + b'\0'
    offs.append(hdr + len(body))
    out = bytearray([T_STRTAB]) + n.to_bytes(3, 'little' if e == '<' else 'big')
    out += struct.pack(e + '%dI' % (n + 1), *offs) + body
    while len(out) % 4:
        out += b'\0'
    return out


def _items(node):
    if isinstance(node, BArr):
        return list(enumerate(node))
    if node.t == T_DICT:
        return sorted(node.items(), key=lambda kv: _key(kv[0]))
    return sorted(node.items(), key=lambda kv: kv[0])


def _csize(node):
    n = len(node)
    if isinstance(node, BArr):
        return 4 + ((n + 3) & ~3) + 4 * n
    if node.t == T_DICT:
        return 4 + 8 * n
    hs = 4 if node.t == T_HASH32 else 8
    return 4 + (hs + 4) * n + ((n + 3) & ~3)


def write(root, version=7, e='<'):
    keys, strs = set(), set()
    if root is not None:
        _collect(root, keys, strs)
    keys = sorted(keys, key=_key)
    strs = sorted(strs, key=_key)
    kidx = {k: i for i, k in enumerate(keys)}
    sidx = {s: i for i, s in enumerate(strs)}
    bo = 'little' if e == '<' else 'big'

    buf = bytearray(16)
    hk = st = 0
    if keys:
        hk = len(buf)
        buf += _strtab_bytes(keys, e)
    if strs:
        st = len(buf)
        buf += _strtab_bytes(strs, e)

    # pass 1: lay out containers (DFS pre-order), then wide values / binaries
    order, offs = [], {}
    pos = len(buf)

    def lay(node):
        nonlocal pos
        offs[id(node)] = pos
        order.append(node)
        pos += _csize(node)
        for _, v in _items(node):
            if isinstance(v, (BDict, BArr)):
                lay(v)

    if root is not None:
        lay(root)
    extras = {}

    def lay_extra(node):
        nonlocal pos
        for _, v in _items(node):
            if isinstance(v, (BDict, BArr)):
                lay_extra(v)
            elif v.t in WIDE:
                pos = (pos + 7) & ~7
                extras[id(v)] = pos
                pos += 8
            elif v.t == T_BIN:
                pos = (pos + 3) & ~3
                extras[id(v)] = pos
                pos += 4 + len(v.v)
            elif v.t == T_BINA:
                data, al = v.v
                pos = (pos + 3) & ~3
                extras[id(v)] = pos
                pos += 8 + len(data)

    if root is not None:
        lay_extra(root)
    out = buf + bytearray(((pos + 3) & ~3) - len(buf))

    def slot(v):
        if isinstance(v, (BDict, BArr)):
            return v.t, offs[id(v)]
        t = v.t
        if t == T_STR:
            return t, sidx[v.v]
        if t == T_BOOL:
            return t, 1 if v.v else 0
        if t == T_INT:
            return t, struct.unpack(e + 'I', struct.pack(e + 'i', int(v.v)))[0]
        if t == T_UINT:
            return t, int(v.v) & 0xFFFFFFFF
        if t == T_FLOAT:
            return t, struct.unpack(e + 'I', struct.pack(e + 'f', float(v.v)))[0]
        if t in WIDE or t in (T_BIN, T_BINA):
            o = extras[id(v)]
            if t == T_I64:
                struct.pack_into(e + 'q', out, o, int(v.v))
            elif t == T_U64:
                struct.pack_into(e + 'Q', out, o, int(v.v))
            elif t == T_F64:
                struct.pack_into(e + 'd', out, o, float(v.v))
            elif t == T_BIN:
                struct.pack_into(e + 'I', out, o, len(v.v))
                out[o + 4:o + 4 + len(v.v)] = v.v
            else:
                data, al = v.v
                struct.pack_into(e + 'II', out, o, len(data), al)
                out[o + 8:o + 8 + len(data)] = data
            return t, o
        if t == T_NULL:
            return t, 0
        raise TypeError('cannot write type 0x%02x' % t)

    for node in order:
        o = offs[id(node)]
        items = _items(node)
        n = len(items)
        out[o] = node.t
        out[o + 1:o + 4] = n.to_bytes(3, bo)
        if isinstance(node, BArr):
            vb = o + 4 + ((n + 3) & ~3)
            for i, (_, v) in enumerate(items):
                t, x = slot(v)
                out[o + 4 + i] = t
                struct.pack_into(e + 'I', out, vb + 4 * i, x)
        elif node.t == T_DICT:
            for i, (k, v) in enumerate(items):
                t, x = slot(v)
                p = o + 4 + 8 * i
                out[p:p + 3] = kidx[k].to_bytes(3, bo)
                out[p + 3] = t
                struct.pack_into(e + 'I', out, p + 4, x)
        else:
            hs = 4 if node.t == T_HASH32 else 8
            tb = o + 4 + (hs + 4) * n
            for i, (k, v) in enumerate(items):
                t, x = slot(v)
                p = o + 4 + (hs + 4) * i
                struct.pack_into(e + ('I' if hs == 4 else 'Q'), out, p, k)
                struct.pack_into(e + 'I', out, p + hs, x)
                out[tb + i] = t

    out[0:2] = b'YB' if e == '<' else b'BY'
    struct.pack_into(e + 'HIII', out, 2, version, hk, st, offs[id(root)] if root is not None else 0)
    return bytes(out)


def dump(data, show_offsets=False):
    b = Byml(data)
    lines = []
    for p, t, v, o in b.flat():
        if t in CONTAINERS:
            continue
        if isinstance(v, float):
            v = round(v, 6)
        if isinstance(v, (bytes, tuple)):
            v = '<bin %d>' % len(v if isinstance(v, bytes) else v[0])
        lines.append(('%-70s %-6s %r' % (p, TYPENAME.get(t, hex(t)), v)) + (' @0x%x' % o if show_offsets else ''))
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    for fn in sys.argv[1:]:
        print('==', fn)
        print(dump(open(fn, 'rb').read()))
