"""Camera mod builder: FOV, distance and stick sensitivity for the OoT remake.

The player camera is tuned entirely by data in romfs/Pack/Actor/PlayerCamera.pack.zs:
  Camera/PlayerCamera.game__camera__PlayerCameraAIParam.bgyml   every camera mode's params
  GameParameter/PlayerCameraParam*/<Mode>.bgyml                  per-mode source params
  Component/PlayerCameraParam/PlayerCamera...bgyml               stick sensitivity levels
We scale the float values in place (same byte size, so the RESTBL entry stays valid),
recompress with the game's pack dictionary and drop the file into a LayeredFS mod folder.
RomFS mods load when you boot the loose dump (not the .nup).
"""
import json
import os
import shutil
import struct

import byml
import gamedata

PACK = 'Pack/Actor/PlayerCamera.pack.zs'
MOD_NAME = 'DevTool Camera'
CATEGORIES = [
    ('explore', 'Exploring (normal, swim, climb, indoors...)'),
    ('lockon', 'Z-targeting / lock-on'),
    ('aiming', 'Aiming (bow, slingshot, hookshot, boomerang, fishing)'),
    ('horse', 'Riding Epona'),
    ('talk', 'Talking / event cameras'),
]
FOV_SKIP = {'FovyCus', 'FovyOffsetExMinLat', 'FovyNormalRelativeLat', 'FovyHighRelativeLat'}
DIST_KEYS = {'Radius', 'RadiusMax', 'RadiusMin', 'RadiusFar', 'RadiusNear', 'RadiusFast', 'RadiusSlow'}
DIST_CATS = {'explore', 'lockon', 'horse'}
FOV_CAP = 150.0
MARK = 0xFFFF  # low 16 mantissa bits carry a scrambled per-value id (changes a value by < 0.8%)
MARKER_SCHEME = 2
AIMING = ('Bow', 'Boomerang', 'Hookshot', 'FishingRod', 'LureZoom', 'SinkLureZoom')


def default_settings():
    return {'fov': {c: 1.0 for c, _ in CATEGORIES}, 'distance': 1.0, 'sensitivity': 1.0}


def classify(mode):
    if mode.startswith(AIMING) and mode != 'HookshotHookedAndMove':
        return 'aiming'
    if 'LockOn' in mode:
        return 'lockon'
    if mode.startswith('Horse') or mode == 'RidingParam':
        return 'horse'
    if mode.startswith(('Event', 'MultiTalk', 'ParamsEvent')):
        return 'talk'
    return 'explore'


def sarc_entries(data):
    """[(name, start, end)] with absolute offsets into the SARC buffer."""
    hl = struct.unpack_from('<H', data, 4)[0]
    doff = struct.unpack_from('<I', data, 0xC)[0]
    p = hl
    n = struct.unpack_from('<H', data, p + 6)[0]
    p += 12
    nodes = [struct.unpack_from('<IIII', data, p + 16 * i) for i in range(n)]
    p += 16 * n
    names = p + 8
    out = []
    for _h, attr, s, e in nodes:
        no = (attr & 0xFFFFFF) * 4
        nm = data[names + no:data.index(b'\0', names + no)].decode('utf-8', 'replace')
        out.append((nm, doff + s, doff + e))
    return out


def _mode_and_key(fname, path):
    parts = path.split('/')
    key = parts[-1]
    if fname.endswith('PlayerCameraAIParam.bgyml'):
        if parts[0] == 'Params' and len(parts) >= 3:
            return parts[1], key
        if 'CameraVariation' in parts[0]:
            return parts[0].split('CameraVariation')[0], key
        return parts[0], key
    if fname.startswith('GameParameter/PlayerCameraParam'):
        return os.path.basename(fname).split('.')[0], key
    return None, key


def candidates(pack):
    """Every tunable float in the pack: [(file, start, end, off_in_file, value, cat, kind)].
    kind is 'fov', 'dist' or 'sens'. Layout is identical in the vanilla and the modded file."""
    out, seen = [], set()
    for name, s, e in sarc_entries(pack):
        if not name.endswith('.bgyml'):
            continue
        try:
            b = byml.Byml(pack[s:e])
        except Exception:
            continue
        for path, t, v, off in b.flat():
            if t != byml.T_FLOAT or v is None or (name, off) in seen:
                continue  # BYML shares identical containers: one stored value can have many paths
            mode, key = _mode_and_key(name, path)
            if name.startswith('Component/PlayerCameraParam/') and path.startswith('StickSensitivity/'):
                out.append((name, s, e, off, v, 'all', 'sens', path, b.e))
                seen.add((name, off))
            elif mode is not None:
                cat = classify(mode)
                if key.startswith('Fovy') and key not in FOV_SKIP and 'Ratio' not in key and v > 1.0:
                    out.append((name, s, e, off, v, cat, 'fov', path, b.e))
                    seen.add((name, off))
                elif key in DIST_KEYS and cat in DIST_CATS and v > 0.0:
                    out.append((name, s, e, off, v, cat, 'dist', path, b.e))
                    seen.add((name, off))
    return out


def scaled(value, cat, kind, settings):
    if kind == 'fov':
        return min(value * float(settings['fov'].get(cat, 1.0)), FOV_CAP)
    if kind == 'dist':
        return value * float(settings['distance'])
    return value * float(settings['sensitivity'])


def marker_code(idx):
    """A scrambled, unique 16-bit code per value index (odd multiplier = bijective)."""
    return (((idx + 1) * 0x9E3B) & 0xFFFF) ^ 0x5A5A


def marked(value, idx, en='<'):
    """The float32 bits of value with its lowest 16 mantissa bits replaced by the value's code."""
    bits = struct.unpack(en + 'I', struct.pack(en + 'f', value))[0]
    return (bits & ~MARK) | marker_code(idx)


def build(rf, settings, markers=True):
    """Returns (new_pack_bytes, changes, subfiles) where subfiles = {name: (start, old, new)}.
    With markers, every tunable value also carries its index in its lowest mantissa bits, so the
    live tool can recognise the game's own parsed copies of each value in RAM."""
    orig = rf.read(PACK)
    data = bytearray(orig)
    changes, touched = [], {}
    for idx, (name, s, e, off, v, cat, kind, path, en) in enumerate(candidates(orig)):
        new = scaled(v, cat, kind, settings)
        if markers:
            struct.pack_into(en + 'I', data, s + off, marked(new, idx, en))
            touched[name] = (s, e)
        elif abs(new - v) > 1e-6:
            struct.pack_into(en + 'f', data, s + off, new)
            touched[name] = (s, e)
        if abs(new - v) > 1e-6:
            changes.append((name, path, v, new))
    subfiles = {n: (s, orig[s:e], bytes(data[s:e])) for n, (s, e) in touched.items()}
    return bytes(data), changes, subfiles


def live_map(rf):
    """What the live camera needs, saved as JSON so it works without zstandard."""
    orig = rf.read(PACK)
    files, values, offs = {}, [], {}
    cands = candidates(orig)
    for name, s, e, off, v, cat, kind, path, en in cands:
        offs.setdefault(name, set()).update(range(off, off + 4))
    for name, s, e, off, v, cat, kind, path, en in cands:
        if name not in files:
            blob = orig[s:e]
            bad = offs[name]
            # two signature windows that never contain a value we change
            h = 0
            while any(i in bad for i in range(h, h + 48)):
                h += 4
            t = len(blob) - 32
            while t > h and any(i in bad for i in range(t, t + 32)):
                t -= 4
            mid = (len(blob) // 2) & ~3
            while mid + 32 < t and any(i in bad for i in range(mid, mid + 32)):
                mid += 4
            files[name] = {'start': s, 'size': len(blob), 'head_off': h, 'head': blob[h:h + 48].hex(),
                           'mid_off': mid, 'mid': blob[mid:mid + 32].hex(),
                           'tail_off': t, 'tail': blob[t:t + 32].hex(), 'endian': en}
        values.append([name, off, v, cat, kind, path])
    return {'version': 5, 'pack': PACK, 'pack_size': len(orig), 'files': files, 'values': values}


def live_map_path(tool_dir, build_id):
    return os.path.join(tool_dir, 'data', 'camera_live_%s.json' % build_id[:16])


def load_live_map(rf, tool_dir, build_id):
    p = live_map_path(tool_dir, build_id)
    if os.path.exists(p):
        m = json.load(open(p))
        if m.get('version') == 5 or rf is None:
            return m
    if rf is None:
        raise RuntimeError('no live camera map for this game build yet: needs the zstandard package once '
                           '(Session tab: Install missing packages)')
    m = live_map(rf)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(m, open(p, 'w'))
    return m


class LiveCamera:
    """Writes camera values straight into the param files the game keeps in RAM."""

    # Most important files first; the rest are searched after.
    PRIORITY = ('PlayerCameraAIParam', 'PlayerCameraParam/')

    def __init__(self, dbg, host, cmap):
        self.dbg, self.host, self.map = dbg, host, cmap
        self.bases = {}

    WINDOWS = (('head_off', 'head'), ('mid_off', 'mid'), ('tail_off', 'tail'))

    def _verify(self, fbase, f):
        """At least two of the three signature windows must match at this file base."""
        good = 0
        for k_off, k_pat in self.WINDOWS:
            if k_off not in f:
                continue
            pat = bytes.fromhex(f[k_pat])
            try:
                if self.dbg.read(fbase + f[k_off], len(pat)) == pat:
                    good += 1
            except Exception:
                pass
        return good >= 2

    def find(self, progress=None):
        """Finds the main camera file once (whole address space), then every other file from its
        position inside the same unpacked pack. Falls back to per-file searches."""
        files = self.map['files']
        anchor = next(n for n in files if 'PlayerCameraAIParam' in n)
        fa = files[anchor]
        self.bases = {}
        packs = set()
        wins = [w for w in self.WINDOWS if w[0] in fa]
        for i, (k_off, k_pat) in enumerate(wins):
            if progress:
                progress(i, len(wins), 'main camera file')
            hits, _ = self.dbg.search(bytes.fromhex(fa[k_pat]), regions='all', max_hits=32, total_ms=90000)
            for h in hits:
                fbase = h - fa[k_off]
                if not self._verify(fbase, fa):
                    continue
                self.bases.setdefault(anchor, [])
                if fbase not in self.bases[anchor]:
                    self.bases[anchor].append(fbase)
                if 'start' in fa:
                    pbase = fbase - fa['start']
                    try:
                        if self.dbg.read(pbase, 4) == b'SARC':
                            packs.add(pbase)
                    except Exception:
                        pass
            if self.bases:
                break
        for pbase in packs:
            for name, f in files.items():
                if name != anchor and 'start' in f and self._verify(pbase + f['start'], f):
                    lst = self.bases.setdefault(name, [])
                    if pbase + f['start'] not in lst:
                        lst.append(pbase + f['start'])
        if not packs:  # files are not inside one unpacked pack: search the sensitivity file too
            for name, f in files.items():
                if 'PlayerCameraParam/' in name:
                    hits, _ = self.dbg.search(bytes.fromhex(f['head']), regions='all', max_hits=16, total_ms=60000)
                    ok = [h - f['head_off'] for h in hits if self._verify(h - f['head_off'], f)]
                    if ok:
                        self.bases[name] = ok
        self.packs = sorted(packs)
        return sum(len(v) for v in self.bases.values())

    def _write(self, addr, raw):
        try:
            self.host.write(addr, raw)
            return True
        except OSError:
            return False

    def apply(self, settings):
        """Writes the slider values. Working copies are only written if they still hold the value
        we expect (compare-and-write), so memory that was freed and reused is never touched."""
        n = 0
        self.parsed = getattr(self, 'parsed', {})
        self.expect = getattr(self, 'expect', {})
        for idx, (name, off, v, cat, kind, *_r) in enumerate(self.map['values']):
            en = self.map['files'][name]['endian']
            bits = marked(scaled(v, cat, kind, settings), idx, en)
            raw = struct.pack(en + 'I', bits)
            for a in self.bases.get(name, ()):
                n += self._write(a + off, raw)
            keep = []
            for a in self.parsed.get(idx, ()):
                try:
                    cur = struct.unpack(en + 'I', self.host.read(a, 4))[0]
                except OSError:
                    continue
                if cur != self.expect.get(a):
                    continue  # no longer our value: drop it
                if self._write(a, raw):
                    self.expect[a] = bits
                    keep.append(a)
                    n += 1
            if idx in self.parsed:
                self.parsed[idx] = keep
        return n

    # ------------------------------------------------------------ parsed copies
    SEED_PATHS = ('Params/Normal/Fovy', 'Params/Normal/FovyLookUp', 'Params/Normal_Child/Fovy',
                  'Params/LockOn/FovyNormal', 'Params/Normal/Radius', 'Params/Horse/FovyFast',
                  'StickSensitivity/2')

    def _ranges(self):
        out = []
        size = self.map.get('pack_size', 0)
        for p in getattr(self, 'packs', []):
            out.append((p, p + size))
        for name, addrs in self.bases.items():
            for a in addrs:
                out.append((a, a + self.map['files'][name]['size']))
        return out

    def _read_window(self, center, radius=0x8000):
        start = max(0, (center - radius) & ~0xFFF)
        try:
            return start, self.dbg.read(start, 2 * radius)
        except Exception:
            pass
        page = center & ~0xFFF
        try:
            return page, self.dbg.read(page, 0x1000)
        except Exception:
            return page, b''

    def find_parsed(self, installed_settings, progress=None, report=None, exclude=()):
        """Finds the game's own (parsed) copies of the camera values by their marker bits.
        installed_settings: the settings the camera mod was built with (what the game loaded)."""
        expect = {}
        for idx, (name, off, v, cat, kind, *_r) in enumerate(self.map['values']):
            en = self.map['files'][name]['endian']
            expect[marked(scaled(v, cat, kind, installed_settings), idx, en)] = idx
        known = self._ranges() + list(exclude)
        inside = lambda a: any(lo <= a < hi for lo, hi in known)
        if installed_settings.get('markers') != MARKER_SCHEME:
            if report is not None:
                report.append('camera mod uses an old marker scheme: rebuild it (game closed)')
            self.parsed = {}
            return 0
        seeds = [i for i, row in enumerate(self.map['values']) if len(row) > 5 and row[5] in self.SEED_PATHS]
        self.parsed = {}
        self.expect = {}
        scanned = []
        for k, idx in enumerate(seeds):
            name, off, v, cat, kind, path = self.map['values'][idx]
            bits = [b for b, j in expect.items() if j == idx][0]
            pat = struct.pack('<I', bits)
            if progress:
                progress(k, len(seeds), path)
            hits, _ = self.dbg.search(pat, regions='all', max_hits=64, total_ms=60000)
            other = [h for h in hits if not inside(h)]
            if report is not None:
                report.append('seed %-28s %s value %.4f: %d hit(s), %d outside the param files: %s' % (
                    path, name.split('/')[-1][:30], struct.unpack('<f', pat)[0], len(hits), len(other),
                    ' '.join('%#x' % h for h in other[:12])))
            for h in other:
                if any(lo <= h < hi for lo, hi in scanned):
                    continue
                base, blob = self._read_window(h)
                scanned.append((base, base + len(blob)))
                local = []
                for i in range(0, len(blob) - 3, 4):
                    w = struct.unpack_from('<I', blob, i)[0]
                    j = expect.get(w)
                    if j is not None and not inside(base + i):
                        local.append((base + i, j, w))
                # a real parameter object holds several of our values close together
                good = [x for x in local if sum(1 for y in local if y is not x and abs(y[0] - x[0]) <= 0x200) >= 2]
                for a, j, w in good:
                    lst = self.parsed.setdefault(j, [])
                    if a not in lst:
                        lst.append(a)
                        self.expect[a] = w
                if report is not None:
                    report.append('    window %#x..%#x: %d marked, %d kept (clustered)' % (
                        base, base + len(blob), len(local), len(good)))
                    if good:
                        i = good[0][0] - base
                        report.append('    bytes around first kept: ' + blob[max(0, i - 32):i + 32].hex(' '))
        return sum(len(v) for v in self.parsed.values())


# ================================================================ mod folder
def mod_dir(data_dir, tid):
    return os.path.join(data_dir, 'load', tid, MOD_NAME)


def probe_signatures(rf, data_dir, tid):
    """(original, installed) 32-byte patterns around Params/Normal/Fovy in the AIParam file.
    installed is None when no camera mod is installed or it leaves that value unchanged."""
    orig = rf.read(PACK)
    target = os.path.join(mod_dir(data_dir, tid), 'romfs', *PACK.split('/'))
    mod = rf.decompress(open(target, 'rb').read(), target) if os.path.exists(target) else None
    for name, s, e in sarc_entries(orig):
        if name.endswith('PlayerCameraAIParam.bgyml'):
            for p, t, v, o in byml.Byml(orig[s:e]).flat():
                if p == 'Params/Normal/Fovy':
                    a = s + o - 12
                    ms = mod[a:a + 32] if mod is not None and len(mod) == len(orig) else None
                    return orig[a:a + 32], (ms if ms != orig[a:a + 32] else None)
    raise RuntimeError('Params/Normal/Fovy not found')


def install(rf, data_dir, tid, settings):
    """Writes the camera mod (always with markers, so the live tool can find the game's copies)."""
    pack, changes, subfiles = build(rf, settings, markers=True)
    d = mod_dir(data_dir, tid)
    target = os.path.join(d, 'romfs', *PACK.split('/'))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    comp = rf.compress(pack, PACK)
    tmp = target + '.tmp'
    open(tmp, 'wb').write(comp)
    os.replace(tmp, target)
    o, m = probe_signatures(rf, data_dir, tid)
    meta = dict(settings, probe_original=o.hex(), probe_installed=m.hex() if m else None,
                markers=MARKER_SCHEME)
    json.dump(meta, open(os.path.join(d, 'devtool_camera.json'), 'w'), indent=1)
    return changes, target


def remove(data_dir, tid):
    d = mod_dir(data_dir, tid)
    if os.path.isdir(d):
        shutil.rmtree(d)
        return True
    return False


def other_mods_touching(data_dir, tid):
    """Other mod folders that also replace PlayerCamera.pack.zs (they would conflict)."""
    root = os.path.join(data_dir, 'load', tid)
    out = []
    if os.path.isdir(root):
        for m in os.listdir(root):
            if m != MOD_NAME and os.path.exists(os.path.join(root, m, 'romfs', *PACK.split('/'))):
                out.append(m)
    return out


def check_loaded(dbg, rf, data_dir, tid):
    """Searches guest RAM for the camera params: tells whether the game loaded the mod.
    rf may be None (no zstandard): then the patterns saved at build time are used."""
    if rf is not None:
        o, m = probe_signatures(rf, data_dir, tid)
    else:
        try:
            meta = json.load(open(os.path.join(mod_dir(data_dir, tid), 'devtool_camera.json')))
            o = bytes.fromhex(meta['probe_original'])
            m = bytes.fromhex(meta['probe_installed']) if meta.get('probe_installed') else None
        except Exception:
            raise RuntimeError('needs the zstandard package (Session tab: Install missing packages)')
    ho = hm = []
    for region, ms in (('heap', 15000), ('all', 30000)):
        ho, _ = dbg.search(o, regions=region, max_hits=4, total_ms=ms)
        hm, _ = dbg.search(m, regions=region, max_hits=4, total_ms=ms) if m else ([], True)
        if ho or hm:
            break
    if hm and not ho:
        return 'LOADED: the game has the modded camera params in RAM (%d hit(s)).' % len(hm)
    if ho and not hm:
        return ('NOT LOADED: RAM holds the original camera params. Boot "OoT_dump (update)" '
                '(not the .nup) and check "%s" is ticked in Add-Ons.' % MOD_NAME)
    if ho and hm:
        return 'BOTH original and modded params are in RAM (another copy loaded?).'
    return ('Camera params not found in RAM (the game may parse and free the file). '
            'Load into the game world and try again.')
