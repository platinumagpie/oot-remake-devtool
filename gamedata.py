"""GameData definitions, flag names and hashing for the OoT remake (TotK GameData model).

The game keeps every persistent value (flags, items, counters, time, position...) as a
GameData entry keyed by murmur3_32(name, seed 0). The definitions live in
romfs/GameData/GameDataList.Product.*.byml.zs; the names are not shipped, so we recover them
by hashing strings harvested from the game files (names_build.py) plus your own labels.
"""
import os
import glob
import pickle
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(HERE, 'user')
CACHE_DIR = os.path.join(HERE, 'cache')

# Order of the type sections inside progress.sav (index = the section marker value).
SAVE_TYPES = ['Bool', 'BoolArray', 'Int', 'IntArray', 'Float', 'FloatArray', 'Enum', 'EnumArray',
              'Vector2', 'Vector2Array', 'Vector3', 'Vector3Array', 'String16', 'String16Array',
              'String32', 'String32Array', 'String64', 'String64Array', 'Binary', 'BinaryArray',
              'UInt', 'UIntArray', 'Int64', 'Int64Array', 'UInt64', 'UInt64Array',
              'WString16', 'WString16Array', 'WString32', 'WString32Array', 'WString64',
              'WString64Array', 'Bool64bitKey']

# ---------------------------------------------------------------- murmur3
try:
    import mmh3 as _mmh3

    def mmh(s):
        if isinstance(s, str):
            s = s.encode('utf-8')
        return _mmh3.hash(s, 0, signed=False)
except ImportError:  # pure-Python fallback
    def mmh(s):
        data = s.encode('utf-8') if isinstance(s, str) else bytes(s)
        M = 0xFFFFFFFF
        c1, c2 = 0xcc9e2d51, 0x1b873593
        h, n = 0, len(data)
        r = n & ~3
        for i in range(0, r, 4):
            k = int.from_bytes(data[i:i + 4], 'little')
            k = (k * c1) & M
            k = ((k << 15) | (k >> 17)) & M
            k = (k * c2) & M
            h ^= k
            h = ((h << 13) | (h >> 19)) & M
            h = (h * 5 + 0xe6546b64) & M
        k, t = 0, n & 3
        if t >= 3:
            k ^= data[r + 2] << 16
        if t >= 2:
            k ^= data[r + 1] << 8
        if t >= 1:
            k ^= data[r]
            k = (k * c1) & M
            k = ((k << 15) | (k >> 17)) & M
            k = (k * c2) & M
            h ^= k
        h ^= n
        h ^= h >> 16
        h = (h * 0x85ebca6b) & M
        h ^= h >> 13
        h = (h * 0xc2b2ae35) & M
        h ^= h >> 16
        return h


# ---------------------------------------------------------------- romfs access
def _sarc(data):
    assert data[:4] == b'SARC', data[:4]
    hl = struct.unpack_from('<H', data, 4)[0]
    doff = struct.unpack_from('<I', data, 0xC)[0]
    p = hl
    n = struct.unpack_from('<H', data, p + 6)[0]
    p += 12
    nodes = [struct.unpack_from('<IIII', data, p + 16 * i) for i in range(n)]
    p += 16 * n
    names = p + 8
    out = {}
    for _h, attr, s, e in nodes:
        no = (attr & 0xFFFFFF) * 4
        nm = data[names + no:data.index(b'\0', names + no)].decode('utf-8', 'replace')
        out[nm] = data[doff + s:doff + e]
    return out


class RomFS:
    """Reads (and zstd-decompresses) files from a loose romfs folder."""

    def __init__(self, root):
        import zstandard
        self.zstd = zstandard
        self.root = root
        raw = zstandard.ZstdDecompressor().decompress(
            open(os.path.join(root, 'Pack', 'ZsDic.pack.zs'), 'rb').read(), max_output_size=1 << 26)
        self.dicts = {k: zstandard.ZstdCompressionDict(v) for k, v in _sarc(raw).items()}

    # Which zstd dictionary a file uses is decided by the game, not by us. Surveyed Nov 7 on the dump:
    # dict 1 = zs.zsdic (most byml, bfevfl, ainb), 3 = bcett.byml.zsdic (Banc), 4 = pack.zsdic (Pack), and
    # NO dictionary (dict_id 0) for RSDB\\*.rstbl.byml.zs, the RESTBL (*.rsizetable.zs), UI\\*.bntx.zs and
    # ZsDic.pack.zs itself. A RESTBL written with zs.zsdic crashed the game at boot (null size table at
    # main+0x348C540, Nov 7): compress() copies the vanilla file's frame when the path exists in the dump,
    # and falls back to the extension rule below.
    NO_DICT = ('.rsizetable.zs', '.rstbl.byml.zs', '.bntx.zs', 'ZsDic.pack.zs')

    def _key(self, name):
        n = name.replace('\\', '/')
        if n.endswith(self.NO_DICT):
            return None
        return ('bcett.byml.zsdic' if n.endswith('.bcett.byml.zs') else
                'pack.zsdic' if n.endswith('.pack.zs') else 'zs.zsdic')

    def _vanilla_key(self, name):
        """The dictionary key the vanilla copy of this file uses (None = dict-less); '' if unknown."""
        n = name.replace('\\', '/')
        rel = n[n.index('romfs/') + 6:] if 'romfs/' in n else n
        p = os.path.join(self.root, rel)
        if not os.path.isfile(p):
            return ''
        try:
            did = self.zstd.get_frame_parameters(open(p, 'rb').read(64)).dict_id
        except Exception:
            return ''
        if did == 0:
            return None
        for k, d in self.dicts.items():
            if d.dict_id() == did:
                return k
        return ''

    def decompress(self, b, name=''):
        key = self._key(name)
        for k in [key] + [k for k in self.dicts if k != key] + ([None] if key else []):
            try:
                dz = (self.zstd.ZstdDecompressor(dict_data=self.dicts[k]) if k
                      else self.zstd.ZstdDecompressor())
                return dz.decompress(b, max_output_size=1 << 29)
            except Exception:
                pass
        raise RuntimeError('cannot decompress ' + name)

    def compress(self, data, name, level=19):
        key = self._vanilla_key(name)
        if key == '':
            key = self._key(name)
        c = self.zstd.ZstdCompressor(dict_data=self.dicts[key] if key else None, level=level,
                                     write_content_size=True, write_checksum=False, write_dict_id=bool(key))
        out = c.compress(data)
        assert self.decompress(out, name) == data
        return out

    def read(self, rel):
        p = os.path.join(self.root, rel)
        b = open(p, 'rb').read()
        return self.decompress(b, p) if p.endswith('.zs') else b


def find_gamedatalist(romfs):
    c = sorted(glob.glob(os.path.join(romfs, 'GameData', 'GameDataList.Product.*.byml.zs')))
    if not c:
        raise FileNotFoundError('GameDataList not found under ' + romfs)
    return c[-1]


# ---------------------------------------------------------------- definitions
class Def:
    __slots__ = ('type', 'hash', 'default', 'save_index', 'reset', 'raw_values', 'values', 'size', 'members')

    def __init__(self, type_, h):
        self.type, self.hash = type_, h
        self.default = None
        self.save_index = -1
        self.reset = 0
        self.raw_values = None  # enum value names
        self.values = None      # enum value hashes
        self.size = None
        self.members = None     # Struct: [(member name hash, full entry hash)]


def _v(x):
    return x.v if hasattr(x, 'v') else x


def load_definitions(romfs, path=None):
    """Returns (defs: {hash: Def}, meta: dict). Cached per GameDataList file.
    path: an alternative GameDataList (v0.6: the DevTool Core mod's copy with the DevTool.* entries)."""
    path = path or find_gamedatalist(romfs)
    st = os.stat(path)
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, 'gdl2_%d_%d.pkl' % (st.st_size, int(st.st_mtime)))
    if os.path.exists(cache):
        try:
            return pickle.load(open(cache, 'rb'))
        except Exception:
            pass
    import byml
    if os.path.abspath(path).startswith(os.path.abspath(romfs)):
        raw = RomFS(romfs).read(os.path.relpath(path, romfs))
    else:
        raw = RomFS(romfs).decompress(open(path, 'rb').read(), path)
    tree = byml.Byml(raw).tree()
    defs = {}
    for type_, entries in tree['Data'].items():
        if type_ == 'Bool64bitKey':
            continue
        for e in entries:
            d = Def(type_, _v(e['Hash']))
            dv = e.get('DefaultValue')
            if type_ == 'Struct' and isinstance(dv, list):
                d.members = [(_v(m['Hash']) & 0xFFFFFFFF, _v(m['Value']) & 0xFFFFFFFF) for m in dv
                             if isinstance(m, dict) and 'Hash' in m and 'Value' in m]
            if isinstance(dv, (list, dict)):
                d.default = None
            else:
                d.default = _v(dv)
            d.save_index = _v(e.get('SaveFileIndex', -1))
            d.reset = _v(e.get('ResetTypeValue', 0))
            if 'RawValues' in e:
                d.raw_values = [_v(x) for x in e['RawValues']]
                d.values = [_v(x) & 0xFFFFFFFF for x in e['Values']]
            for k in ('TotalSize', 'Size', 'ArraySize'):
                if k in e:
                    d.size = _v(e[k])
                    break
            defs[d.hash] = d
    meta = {k: (_v(v) if not isinstance(v, list) else [_v(x) for x in v])
            for k, v in tree['MetaData'].items()}
    meta['file'] = os.path.basename(path)
    out = (defs, meta)
    try:
        pickle.dump(out, open(cache, 'wb'))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- names
class Names:
    """hash -> name, from names.tsv (generated), enum value names, and user/names_user.tsv."""

    def __init__(self, defs=None):
        self.map = {}
        self.user = {}
        self._load(os.path.join(HERE, 'names.tsv'), self.map)
        if defs:
            for d in defs.values():
                if d.raw_values:
                    for h, n in zip(d.values, d.raw_values):
                        self.map.setdefault(h, n)
        self.user_path = os.path.join(USER_DIR, 'names_user.tsv')
        self._load(self.user_path, self.user)

    @staticmethod
    def _load(path, into):
        if not os.path.exists(path):
            return
        for line in open(path, encoding='utf-8'):
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            try:
                into[int(parts[0], 16)] = parts[1]
            except (ValueError, IndexError):
                pass

    def get(self, h):
        return self.user.get(h) or self.map.get(h)

    def label(self, h):
        n = self.get(h)
        return n if n else '#%08X' % h

    def add_user(self, name, h=None):
        """Label a hash yourself (any text) or add a real name (h=None -> hashed)."""
        h = mmh(name) if h is None else h
        self.user[h] = name
        os.makedirs(USER_DIR, exist_ok=True)
        with open(self.user_path, 'a', encoding='utf-8') as f:
            f.write('%08X\t%s\n' % (h, name))
        return h

    def try_names(self, candidates, known_hashes):
        """Hash candidate strings; keep the ones that hit a known GameData hash."""
        hits = {}
        for c in candidates:
            c = c.strip()
            if c:
                h = mmh(c)
                if h in known_hashes and h not in self.map and h not in self.user:
                    hits[h] = c
        for h, c in hits.items():
            self.add_user(c, h)
        return hits

    def count(self):
        return len(set(self.map) | set(self.user))
