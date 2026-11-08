"""RESTBL (resource size table) reader/writer and the merger (F7, v0.6).

Copied from tools\\fogmod\\formats.py (Restbl) so the tool has no cross-project import.

The merger: Lichii applies RomFS mods as LayeredFS layers sorted by folder NAME ascending, vanilla last,
and a file is taken from the FIRST layer that has it (zortos293/Lichii @9fa4a71 and Frizti/Lichii
@72e5ad6, src/core/file_sys/patch_manager.cpp ApplyLayeredFS: `std::sort(patch_dirs ... l->GetName() <
r->GetName())`, then src/core/file_sys/vfs/vfs_layered.cpp LayeredVfsDirectory::GetFileRelative returns
the first hit). So "0 DevTool RESTBL" (digit first) wins over any mod whose name starts with a letter,
and the merged table is built from vanilla + every enabled mod's RESTBL (later = alphabetically later
mod folders lose for the same path, matching what the game will actually load for that file).
"""
import glob
import os
import struct
import zlib

RESTBL_GLOB = os.path.join('System', 'Resource', 'ResourceSizeTable.Product.*.rsizetable.zs')
MERGED_MOD = '0 DevTool RESTBL'      # sorts before every letter, so its table is the one the game sees


class Restbl:
    """TotK-style RESTBL (magic 'RESTBL', v1): crc32(path)->size plus a by-name table for collisions."""

    def __init__(self, data):
        assert data[:6] == b'RESTBL', data[:6]
        self.ver, self.sbs, ncrc, nname = struct.unpack_from('<IIII', data, 6)
        p = 0x16
        self.crc = {}
        for i in range(ncrc):
            c, s = struct.unpack_from('<II', data, p)
            self.crc[c] = s
            p += 8
        self.names = {}
        for i in range(nname):
            nm = data[p:p + self.sbs].split(b'\0')[0].decode()
            self.names[nm] = struct.unpack_from('<I', data, p + self.sbs)[0]
            p += self.sbs + 4

    def get(self, path):
        if path in self.names:
            return self.names[path]
        return self.crc.get(zlib.crc32(path.encode()))

    def set(self, path, size):
        if path in self.names:
            self.names[path] = size
        else:
            self.crc[zlib.crc32(path.encode())] = size

    def grow(self, path, old_len, new_len):
        """Raise the entry by the 32-byte-rounded growth. Returns (old, new) or None if no entry."""
        cur = self.get(path)
        if cur is None:
            return None
        delta = (new_len - old_len + 31) & ~31  # exact growth rounded up, so the original margin is kept
        if new_len > old_len:
            self.set(path, cur + delta)
            return cur, cur + delta
        return cur, cur

    def to_bytes(self):
        out = bytearray(b'RESTBL')
        out += struct.pack('<IIII', self.ver, self.sbs, len(self.crc), len(self.names))
        for c in sorted(self.crc):
            out += struct.pack('<II', c, self.crc[c])
        for nm in sorted(self.names):
            out += nm.encode().ljust(self.sbs, b'\0') + struct.pack('<I', self.names[nm])
        return bytes(out)


def find_restbl(romfs):
    c = sorted(glob.glob(os.path.join(romfs, RESTBL_GLOB)))
    return os.path.relpath(c[-1], romfs).replace(os.sep, '/') if c else None


def size_for_new_file(decompressed_len):
    """A generous RESTBL value for a file the game has never seen: the decompressed size plus
    the usual parse margin, rounded to 32. Unconfirmed against the game; vanilla margins vary."""
    return ((decompressed_len + decompressed_len // 4 + 0x1000) + 31) & ~31


def merge(vanilla_bytes, mod_tables, log=None):
    """mod_tables: [(mod name, Restbl bytes)] in the order Lichii layers them (folder name ascending).
    Each entry that a mod's table changes versus vanilla is applied, first mod wins on a conflict
    (that is the layer whose FILE the game loads too). Returns (Restbl, [(path/crc, mod, old, new)])."""
    base = Restbl(vanilla_bytes)
    van_crc, van_names = dict(base.crc), dict(base.names)
    owner_crc, owner_name, changes = {}, {}, []
    for name, data in mod_tables:
        t = Restbl(data)
        for c, s in t.crc.items():
            if van_crc.get(c) != s and c not in owner_crc:
                owner_crc[c] = name
                changes.append(('crc %08x' % c, name, van_crc.get(c), s))
                base.crc[c] = s
        for n, s in t.names.items():
            if van_names.get(n) != s and n not in owner_name:
                owner_name[n] = name
                changes.append((n, name, van_names.get(n), s))
                base.names[n] = s
        if log:
            log('[restbl] %s: %d entries differ from vanilla' % (name, sum(1 for ch in changes if ch[1] == name)))
    return base, changes
