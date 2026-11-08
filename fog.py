"""DevTool Fog (v0.8.4): the fog mod, built into the tool.

Ported from tools\\fogmod\\make_fogmod.py (+ gamefiles.py, formats.py) with no cross-project import: it
uses the tool's own byml.py (the same file as the fog mod's), sarc.py, restbl.py and gamedata.RomFS
(every .zs is written through RomFS.compress, which copies the vanilla file's zstd dictionary: the game is
strict about zstd dictionaries per file type). One mod folder, "DevTool Fog", replaces the four
old "Fog - *" folders, and it ships NO RESTBL of its own: the size changes go into devtool_fog.json and
mods.rebuild_restbl() puts them into the merged table ("0 DevTool RESTBL"), like the Core and Player mods.

Decoded facts (from "Desktop\\ZELDAOOT\\Fog mod - README.md", built and tested Nov 5 on Lichii Frizti v2):

* Pack\\EnvPalette.pack.zs -> WorldMgr\\ResEnvPalette\\*.bgyml: 191 time-of-day palettes, the real controls:
    VolumeFogParams1/2 (NearDensity / FarDensity / fades / EHF height falloff)   volumetric fog
    VolumeFogIsEnabled                                                           volumetric fog on/off
    VolumeFog*LightScale (Main, ProbeSky, ProbeBake, ProbeBounce, Dynamic)       the fog's glow
    SfParam_* (density, far, attenuation, horizontal)                            scatter fog = distance haze
    FogStart / FogEnd / FogColor.A                                               classic linear fog
    SkyRParam_mie_amplifier                                                      milky sky glow (mie)
  Palettes inherit from $parent palettes (Parent_Field ...); WorldMgr\\WorldParam picks the time tables per
  area. In the same pack: ResFixedEnvPalette (indoor overrides, VF*Density*/VF*LightScale),
  ResWeatherInfluenceParam (rain fog multiplier), ResEnvPaletteTimeTable.
* Pack\\DemoPalette.pack.zs: 1,347 cutscene palettes: FogDensity, YFogDensity, VolumeFogIntensity.
* agl\\SkyParam\\*.bgyml: global ScatterFog/AdhocFog defaults (ScatterFogDensity; 0.85 is the value we assume
  for the 57 palettes that never set SfParam_density: unverified, only matters when distance haze != 1).
* System\\GameGraphics\\VolumeFogAreaParam\\*.bgyml: 188 local fog volumes (Fog1Params/Fog2Params densities,
  MainLightScale, EnableFog1/2).
* Left alone on purpose (unless --all / include_all): scene-transition fades (SceneConnect*, BlackFade),
  lava and damage volumes, underwater fog (Banc\\GameBancParam OverwriteWaterFogParam; never touched), and the
  Haunted Wasteland sandstorm (a gameplay mechanic; palettes named HountedWasteland, the game's spelling).
* Tested in-game (Nov 5): the white haze and blown-out sky are the volumetric fog drawn 3-4x too bright by
  Lichii (Lichii PR #87); dimming only its light (glow x0.3, the "Lichii fix") removes most of the milky glow.
  Use glow 1.0 once Lichii fixes volumetric brightness. Untested: Hyrule Field, night, rain.
* "None" keeps scatter/cutscene fog at x0.01, not 0: vanilla never uses 0 there (min 0.85 / 0.24) and an
  exact 0 can divide by zero in the shader -> NaN pixels -> white flashes through bloom.
* RomFS mods load only when booting the loose dump (OoT_dump (update)), not the .nup.

Live fog (v0.8.4, unconfirmed): with markers on, every density / haze / glow / mie float in the EnvPalette
pack gets a 16-bit id in its lowest mantissa bits (camera.py's method, changes a value by < 0.8%). After
Connect, LiveFog finds the unpacked pack in RAM (by its SARC header) and any parsed copies (clusters of
marked values), and writes slider values there with compare-and-write. Whether the game re-reads either
copy after load is exactly what is unconfirmed; the UI says what was found and nothing more.
"""
import glob
import json
import os
import re
import shutil
import struct
import time

import byml
from byml import Byml, BDict, BArr, Val, CONTAINERS, T_FLOAT, T_BOOL, T_INT, T_UINT, T_F64
import gamedata
import restbl
import sarc

MOD_NAME = 'DevTool Fog'
OLD_PREFIX = 'Fog - '
REPORT = 'devtool_fog.json'
PACKS = ['Pack/EnvPalette.pack.zs', 'Pack/DemoPalette.pack.zs']
LOOSE = ['agl/SkyParam/*.bgyml', 'System/GameGraphics/VolumeFogAreaParam/*.bgyml']
LIVE_PACK = 'Pack/EnvPalette.pack.zs'

# Strength relative to the console look (1.0 = vanilla). Copied from make_fogmod.py PRESETS.
PRESETS = {
    'lichii-fix': dict(vol=1, scatter=1, linear=1, area=1, demo=1, mie=1, off=False),
    'light': dict(vol=0.5, scatter=0.75, linear=0.6, area=0.5, demo=0.6, mie=0.85, off=False),
    'half': dict(vol=0.2, scatter=0.5, linear=0.4, area=0.25, demo=0.35, mie=0.7, off=False),
    'none': dict(vol=0.0, scatter=0.01, linear=0.0, area=0.0, demo=0.01, mie=0.5, off=True),
    # diagnostics from the fog mod: switch off one fog system at a time (not in the UI)
    'diag-novol': dict(vol=1, scatter=1, linear=1, area=1, demo=1, mie=1, off=True),
    'diag-noscatter': dict(vol=1, scatter=0.0, linear=1, area=1, demo=1, mie=1, off=False),
    'diag-sfdefault': dict(vol=1, scatter=1, linear=1, area=1, demo=1, mie=1, off=False, force_sf=True),
}
UI_PRESETS = [('lichii-fix', 'Lichii fix'), ('light', 'Light'), ('half', 'Half'), ('none', 'None')]
OLD_FOLDER = {'lichii-fix': 'Fog - Lichii fix', 'light': 'Fog - Light', 'half': 'Fog - Half', 'none': 'Fog - None'}
EMU_LIGHT = 0.3          # the Lichii glow fix (make_fogmod.py --emu-light default)
SF_DEFAULT = 0.85        # agl SkyParam ScatterFogDensity, assumed where a palette never sets SfParam_density
SLIDERS = [('vol', 'Volumetric density'), ('scatter', 'Distance haze'), ('glow', 'Glow (fog light)'),
           ('mie', 'Mie (milky sky glow)')]

# volumetric-fog light keys (how bright the fog glows); scaled by the glow factor
LIGHT_KEYS = ['VolumeFogMainLightScale', 'VolumeFogProbeSkyLightScale', 'VolumeFogProbeBakeLightScale',
              'VolumeFogProbeBounceLightScale', 'VolumeFogDynamicLightScale']
FIXED_LIGHT_KEYS = ['VFMainLightScale', 'VFProbeSkyLightScale', 'VFProbeBakeLightScale',
                    'VFProbeBounceLightScale', 'VFDynamicLightScale']
KEEP_AREA = re.compile(r'SceneConnect|BlackFade|Blackfade|Lava|RemoveFog|VolumeFogOff|Damage', re.I)
GAMEPLAY_PAL = re.compile(r'HountedWasteland', re.I)


def f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def factors_for(preset, glow=EMU_LIGHT):
    """The full factor set for a preset name (the four sliders = vol, scatter, glow, mie)."""
    p = dict(PRESETS[preset])
    p['glow'] = glow
    return p


# v0.8.6: the game's own fog (every factor x1, glow x1, volumetric fog on). Only used live; at build time
# "Vanilla" means no fog folder enabled at all.
VANILLA = dict(vol=1.0, scatter=1.0, linear=1.0, area=1.0, demo=1.0, mie=1.0, glow=1.0, off=False)


def custom_factors(vol, scatter, glow, mie):
    """The Custom sliders. The other three factors follow the presets' pattern (a guess, close to Light and
    Half): local fog volumes = volumetric density; linear and cutscene fog = the mean of density and haze.
    Volumetric density 0 switches volumetric fog off, as in None."""
    vol, scatter, glow, mie = (round(float(x), 4) for x in (vol, scatter, glow, mie))
    mid = round((vol + scatter) / 2, 4)
    return dict(vol=vol, scatter=scatter, linear=mid, area=vol, demo=mid, mie=mie, off=vol <= 0, glow=glow)


def preset_of(factors):
    """'light' etc. when the factors are exactly a UI preset's, else 'custom'."""
    for k, _l in UI_PRESETS:
        p = factors_for(k, factors.get('glow', EMU_LIGHT))
        if all(abs(float(p[x]) - float(factors.get(x, -1))) < 1e-6 for x in ('vol', 'scatter', 'linear', 'area', 'demo', 'mie')) \
                and bool(p['off']) == bool(factors.get('off')) and abs(factors.get('glow', 0) - EMU_LIGHT) < 1e-6:
            return k
    return 'custom'


# ================================================================ game files (was gamefiles.py)
class GameFiles:
    """The fog-related game files, with gyml $parent inheritance resolved on lookup."""

    def __init__(self, romfs, rf=None):
        self.romfs = romfs
        self.rf = rf or gamedata.RomFS(romfs)
        self.trees, self.orig, self.where, self.packs = {}, {}, {}, {}
        for p in PACKS:
            raw = self.rf.read(p)
            files = sarc.sarc_read(raw)
            self.packs[p] = (raw, files)
            for n, b in files:
                if b[:2] == b'YB':
                    self._add(n, b, p)
        for pat in LOOSE:
            for f in sorted(glob.glob(os.path.join(romfs, *pat.split('/')))):
                rel = os.path.relpath(f, romfs).replace(os.sep, '/')
                self._add(rel, open(f, 'rb').read(), None)

    def _add(self, name, b, pack):
        self.orig[name] = b
        self.trees[name] = Byml(b).tree()
        self.where[name] = pack

    @staticmethod
    def ref_to_path(ref):
        """'Work/a/b.gyml?Sub' -> ('a/b.bgyml', 'Sub')"""
        sub = None
        if '?' in ref:
            ref, sub = ref.split('?', 1)
        if ref.startswith('Work/'):
            ref = ref[5:]
        if ref.endswith('.gyml'):
            ref = ref[:-5] + '.bgyml'
        return ref, sub

    def parent_of(self, node):
        if isinstance(node, BDict) and '$parent' in node:
            path, sub = self.ref_to_path(node['$parent'].v)
            t = self.trees.get(path)
            if t is None:
                return None
            if sub:
                for k in sub.split('/'):
                    t = t.get(k) if isinstance(t, BDict) else None
                    if t is None:
                        return None
            return t
        return None

    def resolve(self, node, keys, depth=0):
        """Deep-merge lookup of a key path through $parent chains. Returns Val/container or None."""
        if depth > 16 or node is None:
            return None
        if not keys:
            return node
        k = keys[0]
        if isinstance(node, BDict) and k in node:
            child = node[k]
            if len(keys) == 1:
                return child
            if isinstance(child, BDict):
                r = self.resolve(child, keys[1:], depth + 1)
                if r is not None:
                    return r
        par = self.parent_of(node)
        return self.resolve(par, keys, depth + 1) if par is not None else None

    def names(self, prefix):
        return sorted(n for n in self.trees if n.startswith(prefix))


# ================================================================ editing (was make_fogmod.py)
def tree_flat(node, path='', out=None):
    if out is None:
        out = {}
    items = enumerate(node) if isinstance(node, BArr) else node.items()
    for k, v in items:
        ks = ('#%08x' % k) if isinstance(node, BDict) and isinstance(k, int) else str(k)
        p = '%s/%s' % (path, ks) if path else ks
        if isinstance(v, (BDict, BArr)):
            tree_flat(v, p, out)
        else:
            out[p] = v
    return out


def serialize(orig, tree, version):
    """Patch values in place when only values changed (no size change), else rewrite with byml.write."""
    y = Byml(orig)
    flat = {p: (t, v, o) for p, t, v, o in y.flat() if t not in CONTAINERS}
    new = tree_flat(tree)
    if set(flat) == set(new) and all(flat[p][0] == new[p].t for p in new):
        data = bytearray(orig)
        e = y.e
        for p, nv in new.items():
            t, v, o = flat[p]
            if nv.v == v:
                continue
            if t == T_FLOAT:
                struct.pack_into(e + 'f', data, o, nv.v)
            elif t == T_F64:
                struct.pack_into(e + 'd', data, o, nv.v)
            elif t == T_BOOL:
                struct.pack_into(e + 'I', data, o, 1 if nv.v else 0)
            elif t == T_INT:
                struct.pack_into(e + 'i', data, o, nv.v)
            elif t == T_UINT:
                struct.pack_into(e + 'I', data, o, nv.v)
            else:
                raise TypeError(p)
        return bytes(data), 'patched'
    return byml.write(tree, version=version), 'rewritten'


class Editor:
    def __init__(self, log):
        self.log = log
        self.changed = set()

    def scale(self, name, node, key, f, where=''):
        if f == 1.0 or not isinstance(node, BDict) or key not in node:
            return
        v = node[key]
        if v.t != T_FLOAT:
            return
        nv = f32(v.v * f)
        if nv != v.v:
            node[key] = Val(T_FLOAT, nv)
            self.changed.add(name)
            self.log.append('%s  %s%s: %g -> %g' % (name, where, key, v.v, nv))

    def setv(self, name, node, key, val, where=''):
        old = node.get(key)
        if old is not None and old.t == val.t and old.v == val.v:
            return
        node[key] = val
        self.changed.add(name)
        self.log.append('%s  %s%s: %s -> %s' % (name, where, key, None if old is None else old.v, val.v))


def edit(g, P, include_all=False, sf_default=SF_DEFAULT, emu=1.0):
    """Applies factor set P (PRESETS entry + 'glow') to the trees in g. Returns (changed, log, skipped).
    Same edits, in the same order, as make_fogmod.py build()."""
    log = []
    ed = Editor(log)
    emu_light = float(P.get('glow', 1.0))
    vol = P['vol'] * emu
    area = P['area'] * emu

    # 1. time-of-day palettes
    for n in g.names('WorldMgr/ResEnvPalette/'):
        t = g.trees[n]
        if emu_light != 1.0 and not P['off']:
            for k in LIGHT_KEYS:
                if k in t:
                    ed.scale(n, t, k, emu_light)
                elif g.resolve(t, [k]) is None:
                    ed.setv(n, t, k, Val(T_FLOAT, f32(1.0 * emu_light)))
        if not include_all and GAMEPLAY_PAL.search(n):
            continue
        for sub in ('VolumeFogParams1', 'VolumeFogParams2'):
            if isinstance(t.get(sub), BDict):
                for k in ('NearDensity', 'FarDensity'):
                    ed.scale(n, t[sub], k, vol, sub + '/')
        if 'SfParam_density' in t:
            ed.scale(n, t, 'SfParam_density', P['scatter'])
        elif sf_default is not None and (P['scatter'] != 1.0 or P.get('force_sf')) \
                and g.resolve(t, ['SfParam_density']) is None:
            ed.setv(n, t, 'SfParam_density', Val(T_FLOAT, f32(sf_default * P['scatter'])))
        if isinstance(t.get('FogColor'), BDict):
            ed.scale(n, t['FogColor'], 'A', P['linear'], 'FogColor/')
        ed.scale(n, t, 'SkyRParam_mie_amplifier', P['mie'])
        if P['off']:
            ed.setv(n, t, 'VolumeFogIsEnabled', Val(T_BOOL, False))

    # 2. indoor fixed palettes (override volumetric densities)
    for n in g.names('WorldMgr/ResFixedEnvPalette/'):
        t = g.trees[n]
        for k in list(t):
            if re.match(r'VF(Near|Far)Density\d$', k):
                ed.scale(n, t, k, vol)
            elif k in FIXED_LIGHT_KEYS:
                ed.scale(n, t, k, emu_light)

    # 3. cutscene palettes
    for n in g.names('WorldMgr/ResDemoPalette/'):
        t = g.trees[n]
        ed.scale(n, t, 'FogDensity', P['demo'])
        ed.scale(n, t, 'YFogDensity', P['demo'])
        ed.scale(n, t, 'VolumeFogIntensity', P['demo'] * emu)

    # 4. global sky fallback
    for n in g.names('agl/SkyParam/'):
        ed.scale(n, g.trees[n], 'ScatterFogDensity', P['scatter'])

    # 5. local fog volumes
    skipped = []
    for n in g.names('System/GameGraphics/VolumeFogAreaParam/'):
        if not include_all and KEEP_AREA.search(n):
            skipped.append(n.split('/')[-1].split('.')[0])
            continue
        t = g.trees[n]
        for sub in ('Fog1Params', 'Fog2Params'):
            if isinstance(t.get(sub), BDict):
                for k in ('NearDensity', 'FarDensity'):
                    ed.scale(n, t[sub], k, area, sub + '/')
        ed.scale(n, t, 'MainLightScale', emu_light)
        if P['off']:
            ed.setv(n, t, 'EnableFog1', Val(T_BOOL, False))
            ed.setv(n, t, 'EnableFog2', Val(T_BOOL, False))
    return ed.changed, log, skipped


# ================================================================ live markers (camera.py's method)
MARK = 0xFFFF
MARKER_SCHEME = 1


def marker_code(idx):
    """A scrambled, unique 16-bit code per value index (odd multiplier = bijective). Differs from the
    camera's (other constants) so a fog marker is never mistaken for a camera one."""
    return (((idx + 1) * 0x6F4B) & 0xFFFF) ^ 0xA3C5


def marked(value, idx, en='<'):
    bits = struct.unpack(en + 'I', struct.pack(en + 'f', value))[0]
    return (bits & ~MARK) | marker_code(idx)


def _live_kind(name, path, include_all):
    """Which slider a float in the EnvPalette pack belongs to, or None. Same selection rules as edit()."""
    if name.startswith('WorldMgr/ResEnvPalette/'):
        if '/' not in path and path in LIGHT_KEYS:
            return 'glow'
        if not include_all and GAMEPLAY_PAL.search(name):
            return None
        if re.match(r'VolumeFogParams[12]/(Near|Far)Density$', path):
            return 'vol'
        if path == 'SfParam_density':
            return 'scatter'
        if path == 'SkyRParam_mie_amplifier':
            return 'mie'
    elif name.startswith('WorldMgr/ResFixedEnvPalette/') and '/' not in path:
        if re.match(r'VF(Near|Far)Density\d$', path):
            return 'vol'
        if path in FIXED_LIGHT_KEYS:
            return 'glow'
    return None


def mark_file(name, built, orig, include_all, start_idx, values):
    """Marks every slider float of one palette file in place (same size). values gets
    [name, off, base, kind, idx, installed bits]; base = the vanilla value (or the value assumed for an
    added key) that a slider factor multiplies. Zero values stay exact zeros (a marked zero would be a
    denormal and a weak marker), so they are not live."""
    y = Byml(built)
    vanilla = {p: v for p, t, v, o in Byml(orig).flat() if t == T_FLOAT}
    data = bytearray(built)
    seen = set()
    idx = start_idx
    for path, t, v, off in y.flat():
        if t != T_FLOAT or off in seen:
            continue
        kind = _live_kind(name, path, include_all)
        if kind is None or not v:
            continue
        seen.add(off)
        base = vanilla.get(path, 1.0 if kind == 'glow' else SF_DEFAULT)
        bits = marked(v, idx, y.e)
        struct.pack_into(y.e + 'I', data, off, bits)
        values.append([name, off, base, kind, idx, bits])
        idx += 1
    return bytes(data), idx


# ================================================================ build
def emit(g, changed, outdir, markers=True, include_all=False):
    """Writes outdir/romfs/... Returns (written, restbl_sizes, stats, live). No RESTBL file is written:
    restbl_sizes = {RESTBL key: [old size, new size]} for entries that exist in the vanilla table and grew
    (what make_fogmod.py raised in its own table), for mods.rebuild_restbl()."""
    rf = g.rf
    os.makedirs(outdir, exist_ok=True)
    rpath = restbl.find_restbl(g.romfs)
    van = restbl.Restbl(rf.read(rpath)) if rpath else None
    sizes = {}

    def grew(key, old, new):
        if new > old and van is not None and van.get(key) is not None:
            sizes[key] = [old, new]

    newbytes, stats = {}, {'patched': 0, 'rewritten': 0}
    for n in sorted(changed):
        b, how = serialize(g.orig[n], g.trees[n], Byml(g.orig[n]).version)
        assert Byml(b).tree() == g.trees[n], n
        stats[how] += 1
        newbytes[n] = b
        grew(n, len(g.orig[n]), len(b))
    live = None
    if markers:
        values, idx = [], 0
        for n in g.names('WorldMgr/'):
            if g.where.get(n) != LIVE_PACK or not n.startswith(('WorldMgr/ResEnvPalette/', 'WorldMgr/ResFixedEnvPalette/')):
                continue
            b, idx = mark_file(n, newbytes.get(n, g.orig[n]), g.orig[n], include_all, idx, values)
            if b != newbytes.get(n, g.orig[n]):
                newbytes[n] = b
        assert idx < 0x10000
        live = {'scheme': MARKER_SCHEME, 'pack': LIVE_PACK, 'values': values}
    written = []
    for p, (raw, files) in g.packs.items():
        if not any(g.where.get(n) == p for n in newbytes):
            continue
        files2 = [(nm, newbytes.get(nm, b)) for nm, b in files]
        sb = sarc.sarc_write(files2)
        grew(p[:-3], len(raw), len(sb))           # RESTBL key 'Pack/EnvPalette.pack'
        dst = os.path.join(outdir, 'romfs', *p.split('/'))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, 'wb').write(rf.compress(sb, p))
        written.append(p)
        if live is not None and p == LIVE_PACK:
            starts = {nm: s for nm, s, _e in _sarc_offsets(sb)}
            live['pack_size'] = len(sb)
            live['pack_head'] = sb[:64].hex()
            live['files'] = {nm: starts[nm] for nm in {v[0] for v in live['values']}}
    for n, b in newbytes.items():
        if g.where[n] is None:
            dst = os.path.join(outdir, 'romfs', *n.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, 'wb').write(b)
            written.append(n)
    return written, sizes, stats, live


def _sarc_offsets(data):
    """[(name, abs start, abs end)] of a SARC buffer."""
    hl = struct.unpack_from('<H', data, 4)[0]
    doff = struct.unpack_from('<I', data, 0xC)[0]
    n = struct.unpack_from('<H', data, hl + 6)[0]
    p = hl + 12
    nodes = [struct.unpack_from('<IIII', data, p + 16 * i) for i in range(n)]
    names = p + 16 * n + 8
    out = []
    for _h, attr, s, e in nodes:
        no = (attr & 0xFFFFFF) * 4
        out.append((data[names + no:data.index(b'\0', names + no)].decode(), doff + s, doff + e))
    return out


def mod_dir(data, tid):
    return os.path.join(data, 'load', tid, MOD_NAME)


def build(romfs, outdir, factors, include_all=False, markers=True, rf=None, label=None):
    """Builds one fog mod folder at outdir (replaced). Returns the report dict (also outdir/devtool_fog.json)."""
    g = GameFiles(romfs, rf)
    changed, log, skipped = edit(g, factors, include_all)
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    written, sizes, stats, live = emit(g, changed, outdir, markers, include_all)
    preset = label or preset_of(factors)
    rep = {'built': time.strftime('%Y-%m-%d %H:%M'), 'preset': preset, 'factors': factors, 'include_all': include_all,
           'markers': bool(markers), 'files': written, 'edited': len(changed), 'stats': stats,
           'restbl': sizes, 'kept': skipped, 'live': live}
    json.dump(rep, open(os.path.join(outdir, REPORT), 'w'))
    txt = ['DevTool Fog: preset %s, factors %s%s' % (preset, {k: factors[k] for k in sorted(factors)},
                                                     ', --all' if include_all else ''),
           '%d files edited (%d patched in place, %d rewritten); markers for live fog: %s' % (
               len(changed), stats['patched'], stats['rewritten'], '%d values' % len(live['values']) if live else 'off'),
           'written: ' + ', '.join(written),
           'RESTBL (goes into the merged table, "%s"): %s' % (restbl.MERGED_MOD, '; '.join(
               '%s %d->%d' % (k, a, b) for k, (a, b) in sorted(sizes.items())) or 'no entry grew'),
           'kept unchanged (transition/lava volumes): ' + ', '.join(skipped), '', 'Changes:'] + log
    open(os.path.join(outdir, 'fogmod_report.txt'), 'w', encoding='utf-8').write('\n'.join(txt) + '\n')
    return rep


def install(romfs, data, tid, factors, include_all=False, markers=True, rf=None, label=None):
    """Builds into the tool's cache, then swaps the finished folder into <load>/DevTool Fog (so a failed
    build never leaves a half-written mod folder where Lichii would load it). Lichii must be closed."""
    tmp = os.path.join(gamedata.CACHE_DIR, 'fog_build', MOD_NAME)
    rep = build(romfs, tmp, factors, include_all, markers, rf, label)
    dst = mod_dir(data, tid)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(tmp, dst)
    return rep


def report(data, tid):
    try:
        return json.load(open(os.path.join(mod_dir(data, tid), REPORT)))
    except Exception:
        return None


# ================================================================ old "Fog - *" folders
def old_folders(data, tid):
    ld = os.path.join(data, 'load', tid)
    return sorted(d for d in os.listdir(ld) if d.startswith(OLD_PREFIX) and os.path.isdir(os.path.join(ld, d))) \
        if os.path.isdir(ld) else []


def move_old(data, tid, dest):
    """Moves every "Fog - *" folder into dest (user\\backups\\fog_old). Returns [(name, new path)]."""
    ld = os.path.join(data, 'load', tid)
    os.makedirs(dest, exist_ok=True)
    out = []
    for d in old_folders(data, tid):
        to = os.path.join(dest, d)
        if os.path.exists(to):
            to += time.strftime(' %Y%m%d_%H%M%S')
        shutil.move(os.path.join(ld, d), to)
        out.append((d, to))
    return out


def current_choice(data, tid, disabled):
    """(preset or 'custom', factors or None, where): what the user plays with now. DevTool Fog when it is
    enabled; else the first enabled old "Fog - *" folder (first in Lichii's layer order wins); else None,
    which is what the user plays with."""
    rep = report(data, tid)
    if rep and os.path.isdir(mod_dir(data, tid)) and MOD_NAME not in disabled:
        return rep.get('preset', 'custom'), rep.get('factors'), MOD_NAME
    inv = {v: k for k, v in OLD_FOLDER.items()}
    for d in old_folders(data, tid):
        if d not in disabled and d in inv:
            return inv[d], None, d
    return 'vanilla', None, ''          # v0.8.6: was 'none', which selected the None (no fog) preset by mistake


# ================================================================ live fog
class LiveFog:
    """Writes fog slider values into the palette data the game keeps in RAM (unconfirmed that it re-reads it)."""

    def __init__(self, dbg, host, live):
        self.dbg, self.host, self.live = dbg, host, live
        self.pack = None          # guest address of the unpacked EnvPalette SARC, if found
        self.parsed = {}          # idx -> [addresses] of copies outside the pack (parsed palettes?)
        self.expect = {}          # address -> bits we last saw/wrote there (compare-and-write)

    def _addr_in_pack(self, row):
        name, off = row[0], row[1]
        return self.pack + self.live['files'][name] + off

    def find(self, progress=None, report=None, exclude=()):
        """1. the unpacked pack by its SARC header (verified by marker words at known offsets);
        2. copies elsewhere: search a few marked values, keep windows where several marked values cluster."""
        rows = self.live['values']
        expect = {r[5]: i for i, r in enumerate(rows) if r[5] & 0x7F800000}    # skip denormals
        head = bytes.fromhex(self.live['pack_head'])
        if progress:
            progress('looking for the EnvPalette pack')
        hits, _ = self.dbg.search(head, regions='all', max_hits=16, total_ms=90000)
        sample = rows[::max(1, len(rows) // 7)][:7]
        for h in hits:
            good = 0
            for r in sample:
                try:
                    if struct.unpack('<I', self.dbg.read(h + self.live['files'][r[0]] + r[1], 4))[0] == r[5]:
                        good += 1
                except Exception:
                    pass
            if report is not None:
                report.append('pack header hit %#x: %d/%d sample markers match' % (h, good, len(sample)))
            if good >= max(2, len(sample) - 1):
                self.pack = h
                break
        lo_hi = [(self.pack, self.pack + self.live['pack_size'])] if self.pack else []
        known = lo_hi + list(exclude)
        inside = lambda a: any(lo <= a < hi for lo, hi in known)
        if self.pack:
            for r in rows:
                self.expect[self._addr_in_pack(r)] = r[5]
        seeds = self._seeds(rows)
        scanned = []
        for k, i in enumerate(seeds):
            r = rows[i]
            if progress:
                progress('looking for working copies (%d/%d)' % (k + 1, len(seeds)))
            hits, _ = self.dbg.search(struct.pack('<I', r[5]), regions='all', max_hits=64, total_ms=60000)
            other = [h for h in hits if not inside(h)]
            if report is not None:
                report.append('seed %s %s (%s): %d hit(s), %d outside the pack: %s' % (
                    r[0].split('/')[-1].split('.')[0], r[3], '%.4g' % struct.unpack('<f', struct.pack('<I', r[5]))[0],
                    len(hits), len(other), ' '.join('%#x' % h for h in other[:12])))
            for h in other:
                if any(lo <= h < hi for lo, hi in scanned):
                    continue
                base, blob = self._window(h)
                scanned.append((base, base + len(blob)))
                local = []
                for j in range(0, len(blob) - 3, 4):
                    w = struct.unpack_from('<I', blob, j)[0]
                    ix = expect.get(w)
                    if ix is not None and not inside(base + j):
                        local.append((base + j, ix, w))
                good = [x for x in local if sum(1 for y in local if y is not x and abs(y[0] - x[0]) <= 0x400) >= 2]
                for a, ix, w in good:
                    lst = self.parsed.setdefault(ix, [])
                    if a not in lst:
                        lst.append(a)
                        self.expect[a] = w
                if report is not None:
                    report.append('    window %#x..%#x: %d marked, %d kept (clustered)' % (
                        base, base + len(blob), len(local), len(good)))
        return self.found()

    def _seeds(self, rows, n=6):
        """Field palettes first (Hyrule Field is where the haze matters), one of each slider kind."""
        out, kinds = [], {}
        order = sorted(range(len(rows)), key=lambda i: (0 if 'MainField' in rows[i][0] else 1 if 'Field' in rows[i][0] else 2, i))
        for i in order:
            r = rows[i]
            if not (r[5] & 0x7F800000) or kinds.get(r[3], 0) >= 2:
                continue
            kinds[r[3]] = kinds.get(r[3], 0) + 1
            out.append(i)
            if len(out) >= n:
                break
        return out

    def _window(self, center, radius=0x8000):
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

    def found(self):
        return {'pack': self.pack, 'parsed': sum(len(v) for v in self.parsed.values()), 'distinct': len(self.parsed)}

    def apply(self, factors):
        """Writes base x slider factor (still marked) wherever we found a value; compare-and-write, so memory
        that was freed and reused is never touched. Returns the number of writes."""
        n = 0
        for i, r in enumerate(self.live['values']):
            name, off, base, kind, idx, _bits = r
            bits = marked(f32(base * float(factors.get(kind, 1.0))), idx)
            raw = struct.pack('<I', bits)
            addrs = ([self._addr_in_pack(r)] if self.pack else []) + self.parsed.get(i, [])
            for a in addrs:
                try:
                    cur = struct.unpack('<I', self.host.read(a, 4))[0]
                except OSError:
                    continue
                if cur != self.expect.get(a):
                    continue
                try:
                    self.host.write(a, raw)
                except OSError:
                    continue
                self.expect[a] = bits
                n += 1
        return n


# ================================================================ Mod manager: the Fog section
class FogPanel:
    """The Fog section of the Mod manager tab: presets, Custom sliders, Build fog mod, live fog."""

    def __init__(self, app, parent, header=None):
        """header: an existing row of the Mod manager (the one with the perf preset): the compact controls go at
        its left, so the tab is no taller than before; sliders and live fog open below it on demand."""
        import tkinter as tk
        from tkinter import ttk
        self.app, self.tk = app, tk
        self.live = None
        self._job = None
        self._quiet = False
        if header is None:
            header = ttk.Frame(parent)
            header.pack(fill='x')
        self.header = header
        ttk.Label(header, text='Fog:').pack(side='left')
        self.preset = tk.StringVar(value='none')
        for key, label in UI_PRESETS + [('custom', 'Custom')]:
            ttk.Radiobutton(header, text=label, value=key, variable=self.preset, command=self._picked).pack(side='left', padx=(4, 2))
        ttk.Button(header, text='Build fog mod', command=self.build).pack(side='left', padx=4)
        ttk.Button(header, text='Vanilla', command=self.vanilla).pack(side='left', padx=(0, 4))   # v0.8.6
        self.more_btn = ttk.Button(header, text='Sliders + live >', command=self.toggle)
        self.more_btn.pack(side='left')
        ttk.Label(header, text='  ').pack(side='left')
        box = ttk.LabelFrame(parent, text='Fog sliders ("%s"; x1 = the console look; glow x0.3 = the Lichii fix: Lichii draws '
                                          'volumetric fog 3-4x too bright, PR #87)' % MOD_NAME, padding=(6, 2))
        self.box = box
        self.open = False
        r = ttk.Frame(box)
        r.pack(fill='x')
        self.now_lbl = ttk.Label(r, text='')
        self.now_lbl.pack(side='left')
        grid = ttk.Frame(box)
        grid.pack(fill='x')
        self.vars, self.val_lbls = {}, {}
        for i, (key, label) in enumerate(SLIDERS):
            row, col = i // 2, (i % 2) * 3
            ttk.Label(grid, text=label).grid(row=row, column=col, sticky='w', padx=(0 if col == 0 else 16, 4))
            v = tk.DoubleVar(value=1.0)
            self.vars[key] = v
            ttk.Scale(grid, from_=0.0, to=2.0, variable=v, length=220,
                      command=lambda _x, k=key: self._moved(k)).grid(row=row, column=col + 1, sticky='w')
            lb = ttk.Label(grid, text='x1.00', width=6)
            lb.grid(row=row, column=col + 2, sticky='w', padx=4)
            self.val_lbls[key] = lb
        self.all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(grid, text='--all: also fades, lava/damage volumes, wasteland sandstorm',
                        variable=self.all_var).grid(row=0, column=6, sticky='w', padx=(16, 0))
        self.mark_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text='mark values for live fog (< 0.8% change)', variable=self.mark_var).grid(
            row=1, column=6, sticky='w', padx=(16, 0))
        r3 = ttk.Frame(box)
        r3.pack(fill='x', pady=(2, 0))
        ttk.Button(r3, text='Find live fog (after Connect)', command=self.find_live).pack(side='left')
        self.live_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text='Apply slider changes live', variable=self.live_on, command=self._kick).pack(side='left', padx=6)
        self.live_lbl = ttk.Label(r3, text='live fog: not searched yet (unconfirmed: the game may parse the palettes into '
                                           'structs the markers do not survive)', foreground='#808080')
        self.live_lbl.pack(side='left', padx=8)
        self.preselect()

    # ------------------------------------------------------------ state
    def toggle(self, show=None):
        """Shows / hides the sliders and live fog (hidden by default so the Core controls stay on screen)."""
        show = (not self.open) if show is None else show
        if show and not self.open:
            self.box.pack(fill='x', pady=(2, 4), after=self.header)
        elif not show and self.open:
            self.box.pack_forget()
        self.open = show
        self.more_btn.configure(text='< hide sliders' if show else 'Sliders + live >')

    def _say(self, text, color='black'):
        if hasattr(self.app, 'mods_txt'):
            self.app.mods_txt.insert('end', text + '\n')
            self.app.mods_txt.see('end')
        self.app.status(text, color)

    def factors(self):
        p = self.preset.get()
        if p == 'vanilla':
            return dict(VANILLA)
        if p == 'custom':
            v = {k: self.vars[k].get() for k, _l in SLIDERS}
            return custom_factors(v['vol'], v['scatter'], v['glow'], v['mie'])
        return factors_for(p)

    def _show(self, f):
        self._quiet = True
        try:
            for k, _l in SLIDERS:
                self.vars[k].set(float(f[k]))
                self.val_lbls[k].configure(text='x%.2f' % float(f[k]))
        finally:
            self._quiet = False

    def preselect(self):
        """Reads which fog folder is enabled now ([DisabledAddOns] in qt-config.ini) and picks that preset."""
        import mods
        try:
            dis = mods.disabled(self.app.data)
            p, f, where = current_choice(self.app.data, self.app.tid, dis)
        except Exception as e:
            p, f, where = 'vanilla', None, ''
            self.app.log('[fog] could not read the current fog choice: %s' % e)
        self.preset.set(p)      # 'vanilla' matches no radio button: none is selected
        self._show(f if (p == 'custom' and f) else VANILLA if p == 'vanilla' else factors_for(p if p != 'custom' else 'none'))
        self.now_lbl.configure(text=('enabled now: %s' % where) if where else 'no fog folder enabled (vanilla fog)')
        return p

    CUSTOM_NOTE = ('Custom: local fog volumes follow the density slider and linear/cutscene fog the mean of density and haze '
                   '(a guess that matches Light and Half); density 0 switches volumetric fog off, as in None.')

    def _picked(self):
        if self.preset.get() != 'custom':
            self._show(factors_for(self.preset.get()))
        else:
            self.toggle(True)
            self.app.status(self.CUSTOM_NOTE, 'blue')
        self._kick()

    def _moved(self, key):
        self.val_lbls[key].configure(text='x%.2f' % self.vars[key].get())
        if self._quiet:
            return
        if self.preset.get() != 'custom':
            self.preset.set('custom')
            self.app.status(self.CUSTOM_NOTE, 'blue')
        self._kick()

    # ------------------------------------------------------------ build
    def build(self, ask=True):
        import lichii
        import mods
        from tkinter import messagebox
        app = self.app
        if lichii.lichii_pids():
            return app.status('Close the game and Lichii first (mod files are locked while it runs).', 'red')
        if self.preset.get() == 'vanilla':
            return app.status('Vanilla fog needs no build: pick a preset (or Custom) to build the fog mod.', 'blue')
        old = old_folders(app.data, app.tid)
        move = False
        if old and not app.settings.get('fog_old_asked'):
            move = messagebox.askyesno('DevTool Fog', 'Move the old fog folders (%s) out of the load folder into '
                                       'user\\backups\\fog_old\\? "%s" replaces them. You are asked this once; if you say no '
                                       'they stay where they are, disabled.' % (', '.join(old), MOD_NAME)) if ask else False
            app.settings['fog_old_asked'] = True
            app._save_settings()
        f = self.factors()
        label = self.preset.get()
        inc, mk = self.all_var.get(), self.mark_var.get()
        rf = gamedata.RomFS(app.romfs)
        app.status('Building %s (%s)...' % (MOD_NAME, label), 'blue')

        def work():
            notes = []
            if move:
                for d, to in move_old(app.data, app.tid, os.path.join(gamedata.USER_DIR, 'backups', 'fog_old')):
                    notes.append('moved %s -> %s' % (d, to))
            rep = install(app.romfs, app.data, app.tid, f, inc, mk, rf, label)
            mods.set_enabled(app.data, MOD_NAME, True)
            for d in old_folders(app.data, app.tid):
                if d not in mods.disabled(app.data):
                    mods.set_enabled(app.data, d, False)
                    notes.append('disabled %s' % d)
            mods.rebuild_restbl(app.data, app.romfs, rf, app.tid, log=app.log)
            return rep, notes

        def done(r):
            rep, notes = r
            for n in notes:
                app.log('[fog] ' + n)
            self._say('%s built (%s): %d files edited, %d written, %d RESTBL entries into the merged table, %s; enabled. '
                      'Boot the loose dump.' % (MOD_NAME, rep['preset'], rep['edited'], len(rep['files']), len(rep['restbl']),
                                                '%d live markers' % len(rep['live']['values']) if rep['live'] else 'no markers'),
                      'darkgreen')
            self.live = None
            self.live_lbl.configure(text='live fog: rebuilt, boot and search again')
            app.mods_refresh()
            self.preselect()
        app.bg(work, done)

    # ------------------------------------------------------------ vanilla (v0.8.6)
    def vanilla(self):
        """Back to the game's own fog. Game closed: disable DevTool Fog and every old "Fog - *" folder in Lichii's
        add-on list and rebuild the merged RESTBL (nothing is deleted; Build fog mod turns it back on). Game running
        with live fog found: write the vanilla values live (until the next boot)."""
        import lichii
        import mods
        app = self.app
        if lichii.lichii_pids():
            if self.live and app.host:
                self.preset.set('vanilla')
                self._show(VANILLA)

                def done_live(n):
                    self.live_lbl.configure(text='live: wrote %d vanilla values' % n)
                    app.status('Vanilla fog live: wrote %d values (until the next boot; unconfirmed that the game re-reads '
                               'them). The fog folder is still on for the next boot: click Vanilla again with the game and '
                               'Lichii closed to switch it off.' % n, 'darkgreen')
                    app.log('[fog] vanilla live: wrote %d values' % n)
                return app.bg(lambda: self.live.apply(VANILLA), done_live)
            return app.status('Vanilla fog: close the game from its menu, then Lichii, first (Lichii rewrites its add-on '
                              'list when it closes). With live fog found it also works in-game.', 'red')
        rf = gamedata.RomFS(app.romfs)

        def work():
            dis = mods.disabled(app.data)
            names = ([MOD_NAME] if os.path.isdir(mod_dir(app.data, app.tid)) else []) + old_folders(app.data, app.tid)
            off = []
            for d in names:
                if d not in dis:
                    mods.set_enabled(app.data, d, False)
                    off.append(d)
            mods.rebuild_restbl(app.data, app.romfs, rf, app.tid, log=app.log)
            return off

        def done(off):
            for d in off:
                app.log('[fog] vanilla: disabled %s' % d)
            self._say('Vanilla fog: %s; merged RESTBL rebuilt. Boot the loose dump for the game\'s own fog. Nothing was '
                      'deleted: Build fog mod (or enabling the folder) turns it back on.' % (
                          ('disabled ' + ', '.join(off)) if off else 'no fog folder was enabled'), 'darkgreen')
            self.live = None
            self.live_lbl.configure(text='live fog: vanilla now, boot and search again')
            app.mods_refresh()
            self.preselect()
        app.status('Switching fog back to vanilla...', 'blue')
        app.bg(work, done)

    # ------------------------------------------------------------ live
    def find_live(self):
        app = self.app
        if not app.dbg or not app.host:
            return app.status('Live fog: Connect first (with write access), in the game world.', 'red')
        rep = report(app.data, app.tid)
        if not rep or not rep.get('live'):
            return app.status('Live fog: "%s" has no markers. Build it with "mark values for live fog" ticked (game '
                              'closed), boot, then search.' % MOD_NAME, 'red')
        self.live_lbl.configure(text='searching...')
        installed = rep['factors']

        def prog(text):
            app.q.put((lambda t: self.live_lbl.configure(text=t), text, None))

        def work():
            lf = LiveFog(app.dbg, app.host, rep['live'])
            lines = ['Live fog probe %s (built %s, preset %s)' % (time.strftime('%Y-%m-%d %H:%M:%S'), rep['built'], rep['preset'])]
            r = lf.find(progress=prog, report=lines, exclude=[(b, b + sz) for b, sz in getattr(app, 'modules', {}).values()])
            lines.append('result: %s' % r)
            open(os.path.join(gamedata.USER_DIR, 'fog_probe.txt'), 'w').write('\n'.join(lines) + '\n')
            return lf, r

        def done(res):
            lf, r = res
            if not r['pack'] and not r['parsed']:
                self.live = None
                self.live_lbl.configure(text='live fog: NOT FOUND. The game keeps no marked palette values in RAM that '
                                             'the tool can see; use the build-time presets. (details: user\\fog_probe.txt)',
                                        foreground='#c04040')
                return app.status('Live fog: the palettes were not found in RAM (markers not present). The presets still '
                                  'work at build time; live fog is not available this way.', '#a00000')
            self.live = lf
            self.live.installed = installed
            self.live_lbl.configure(text='live fog: pack copy %s, %d working values (unconfirmed that the game re-reads '
                                         'them; watch the haze)' % ('%#x' % r['pack'] if r['pack'] else 'not found', r['parsed']),
                                    foreground='#40a040')
            app.status('Live fog target found (pack %s, %d other copies). Tick "Apply slider changes live" and move a '
                       'slider: unconfirmed whether the game uses these copies after load.' % (
                           '%#x' % r['pack'] if r['pack'] else 'no', r['parsed']), 'darkgreen')
            self._kick()
        app.bg(work, done)

    def _kick(self):
        if not (self.live_on.get() and self.live and self.app.host):
            return
        if self._job:
            self.app.root.after_cancel(self._job)
        self._job = self.app.root.after(150, self._apply)

    def _apply(self):
        self._job = None
        f = self.factors()
        lf = self.live
        note = ' (volumetric fog is switched off in this build: density has no effect)' if lf.installed.get('off') else ''

        def done(n):
            self.live_lbl.configure(text='live: wrote %d values (density x%.2f, haze x%.2f, glow x%.2f, mie x%.2f)%s' % (
                n, f['vol'], f['scatter'], f['glow'], f['mie'], note))
            self.app.log('[fog] live: wrote %d values %s' % (n, {k: f[k] for k, _l in SLIDERS}))
        self.app.bg(lambda: lf.apply(f), done)
