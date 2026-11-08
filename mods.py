"""Mod manager (v0.6): the RomFS mod folders under <Lichii data>\\load\\<title id>\\.

* list():        every mod folder, enabled or not, with the romfs files it ships
* set_enabled(): Lichii's own mechanism, the [DisabledAddOns] section of config\\qt-config.ini (same
                 format tools\\perfmod\\addons.py wrote). Lichii rewrites that file when it closes, so
                 toggles only stick while Lichii is closed; the GUI refuses while it runs.
* conflicts():   the same romfs path in two enabled mods (the alphabetically first folder wins, see
                 restbl.py), and more than one enabled RESTBL
* rebuild_restbl(): the merged RESTBL mod folder "0 DevTool RESTBL" from vanilla + every enabled mod's
                 table + the Core report's size needs
* builders:      the tool's own mods: camera (camera.py), fog and perf (tools\\fogmod and tools\\perfmod
                 scripts run as subprocesses with their presets), Core (core.py)
* loaded_from_log(): what the Lichii log says about RomFS mods on the last boot. Lichii only logs
                 "RomFS: LayeredFS patches applied successfully" (no per-mod line) and the IPS/pchtxt
                 patch names, so "loaded" = that line was seen and the folder was enabled at boot.
"""
import json
import os
import re
import subprocess
import sys
import time

import lichii
import restbl

TITLE_DEC = str(int(lichii.TITLE_ID, 16))
OWN = {'camera': 'DevTool Camera', 'core': 'DevTool Core', 'player': 'DevTool Player', 'fog': 'DevTool Fog',
       'restbl': restbl.MERGED_MOD}
FOG_PRESETS = ['lichii-fix', 'light', 'half', 'none']
PERF_PRESETS = ['quality', 'balanced', 'maxfps']
TOOLS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def load_dir(data, tid=lichii.TITLE_ID):
    return os.path.join(data, 'load', tid)


def cfg_path(data):
    return os.path.join(data, 'config', 'qt-config.ini')


# ---------------------------------------------------------------- qt-config.ini [DisabledAddOns]
def _parse(text):
    m = re.search(r'^\[DisabledAddOns\]\n(.*?)(?=^\[|\Z)', text, re.S | re.M)
    kv = dict(re.findall(r'^([^=\n]+)=(.*)$', m.group(1) if m else '', re.M))
    titles = []
    for i in range(1, int(kv.get('size', '0')) + 1):
        n = int(kv.get('%d\\disabled\\size' % i, '0'))
        titles.append([kv.get('%d\\title_id' % i),
                       [kv['%d\\disabled\\%d\\d' % (i, j)] for j in range(1, n + 1) if '%d\\disabled\\%d\\d' % (i, j) in kv]])
    return m, titles


def disabled(data, tid_dec=TITLE_DEC):
    p = cfg_path(data)
    if not os.path.isfile(p):
        return []
    text = open(p, encoding='utf-8', newline='').read().replace('\r\n', '\n')
    _m, titles = _parse(text)
    for t, names in titles:
        if t == tid_dec:
            return list(names)
    return []


def set_enabled(data, name, enabled, tid_dec=TITLE_DEC):
    """Enable/disable one add-on folder for the title. Returns the disabled list afterwards."""
    p = cfg_path(data)
    raw = open(p, encoding='utf-8', newline='').read() if os.path.isfile(p) else ''
    text = raw.replace('\r\n', '\n')
    m, titles = _parse(text)
    t = next((t for t in titles if t[0] == tid_dec), None)
    if t is None:
        t = [tid_dec, []]
        titles.append(t)
    t[1] = [x for x in t[1] if x != name]
    if not enabled:
        t[1].append(name)
    out = ['size=%d' % len(titles)]
    for i, (tid, names) in enumerate(titles, 1):
        out += ['%d\\title_id\\default=false' % i, '%d\\title_id=%s' % (i, tid), '%d\\disabled\\size=%d' % (i, len(names))]
        for j, nm in enumerate(names, 1):
            out += ['%d\\disabled\\%d\\d\\default=false' % (i, j), '%d\\disabled\\%d\\d=%s' % (i, j, nm)]
    sec = '[DisabledAddOns]\n' + '\n'.join(out) + '\n\n'
    text = text[:m.start()] + sec + text[m.end():] if m else sec + text
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, 'w', encoding='utf-8', newline='\r\n' if '\r\n' in raw else '\n').write(text)
    return t[1]


# ---------------------------------------------------------------- listing
def romfs_files(folder):
    out = []
    for sub in ('romfs', 'romfslite'):
        r = os.path.join(folder, sub)
        if not os.path.isdir(r):
            continue
        for cur, _d, files in os.walk(r):
            for f in files:
                out.append(os.path.relpath(os.path.join(cur, f), r).replace(os.sep, '/'))
    return sorted(out)


def list_mods(data, tid=lichii.TITLE_ID):
    """[{'name', 'path', 'enabled', 'files', 'restbl', 'own', 'exefs'}] sorted the way Lichii layers them."""
    ld = load_dir(data, tid)
    off = set(disabled(data))
    out = []
    if os.path.isdir(ld):
        for d in sorted(os.listdir(ld)):
            p = os.path.join(ld, d)
            if not os.path.isdir(p):
                continue
            files = romfs_files(p)
            out.append({'name': d, 'path': p, 'enabled': d not in off, 'files': files,
                        'restbl': [f for f in files if f.endswith('.rsizetable.zs')],
                        'own': d in OWN.values(), 'exefs': os.path.isdir(os.path.join(p, 'exefs')) or
                        os.path.isdir(os.path.join(p, 'cheats'))})
    out.sort(key=lambda m: m['name'])      # byte order, like Lichii's std::sort on the name
    return out


def conflicts(mods):
    """[(kind, text)] for enabled mods: same romfs path in two mods, and more than one RESTBL."""
    out = []
    seen = {}
    for m in mods:
        if not m['enabled']:
            continue
        for f in m['files']:
            if f.endswith('.rsizetable.zs'):
                continue           # handled below
            if f in seen:
                out.append(('file', '%s is in both "%s" and "%s" (Lichii loads "%s")' % (f, seen[f], m['name'], seen[f])))
            else:
                seen[f] = m['name']
    tables = [(m['name'], f) for m in mods if m['enabled'] for f in m['restbl']]
    if len(tables) > 1 and tables[0][0] != restbl.MERGED_MOD:
        out.append(('restbl', '%d enabled RESTBLs: %s. Only "%s" should ship one (Rebuild merged RESTBL).' % (
            len(tables), ', '.join(n for n, _f in tables), restbl.MERGED_MOD)))
    elif len(tables) > 1:
        out.append(('note', '%d RESTBLs enabled; the merged one wins (it sorts first) and covers the others: %s' % (
            len(tables), ', '.join(n for n, _f in tables[1:]))))
    return out


# ---------------------------------------------------------------- merged RESTBL
def rebuild_restbl(data, romfs, rf, tid=lichii.TITLE_ID, core_report=None, log=None):
    """Write <load>/<MERGED_MOD>/romfs/System/Resource/<restbl> merged from vanilla + enabled mods' tables,
    then grown for the Core files. Returns (path, changes, notes)."""
    rel = restbl.find_restbl(romfs)
    if not rel:
        raise FileNotFoundError('no RESTBL under ' + romfs)
    vanilla = rf.read(rel)
    mods = list_mods(data, tid)
    tables = []
    for m in mods:
        if not m['enabled'] or m['name'] == restbl.MERGED_MOD:
            continue
        for f in m['restbl']:
            tables.append((m['name'], rf.decompress(open(os.path.join(m['path'], 'romfs', f.replace('/', os.sep)), 'rb').read(), f)))
    merged, changes = restbl.merge(vanilla, tables, log)
    notes = []
    rep = core_report
    if rep is None:
        p = os.path.join(load_dir(data, tid), OWN['core'], 'devtool_core.json')
        if os.path.isfile(p) and OWN['core'] not in disabled(data):
            rep = json.load(open(p))
    reps = [rep] if rep else []
    pp = os.path.join(load_dir(data, tid), 'DevTool Player', 'devtool_player.json')
    if os.path.isfile(pp) and 'DevTool Player' not in disabled(data):
        reps.append(json.load(open(pp)))
    # v0.8.4: DevTool Fog ships no RESTBL; its devtool_fog.json lists only entries vanilla has that grew
    fp = os.path.join(load_dir(data, tid), 'DevTool Fog', 'devtool_fog.json')
    if os.path.isfile(fp) and 'DevTool Fog' not in disabled(data):
        reps.append(json.load(open(fp)))
    for rep in reps:
        for key, (old, new) in rep.get('restbl', {}).items():
            if old:
                r = merged.grow(key, old, new)
                if r:
                    notes.append('%s: %d -> %d' % (key, r[0], r[1]))
                else:
                    merged.set(key, restbl.size_for_new_file(new))
                    notes.append('%s: no vanilla entry, set %d' % (key, restbl.size_for_new_file(new)))
            else:
                merged.set(key, restbl.size_for_new_file(new))
                notes.append('%s: new file, entry %d (unconfirmed margin)' % (key, restbl.size_for_new_file(new)))
    out = os.path.join(load_dir(data, tid), restbl.MERGED_MOD, 'romfs', rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, 'wb').write(rf.compress(merged.to_bytes(), rel))
    json.dump({'built': time.strftime('%Y-%m-%d %H:%M'), 'from': [n for n, _t in tables], 'changes': len(changes),
               'core': notes}, open(os.path.join(load_dir(data, tid), restbl.MERGED_MOD, 'devtool_restbl.json'), 'w'), indent=1)
    if log:
        log('[restbl] merged %d mod table(s), %d entries changed, %d Core entries -> %s' % (len(tables), len(changes), len(notes), out))
    return out, changes, notes


# ---------------------------------------------------------------- builders (fog / perf scripts)
def script(name):
    p = {'fog': os.path.join(TOOLS, 'fogmod', 'make_fogmod.py'),
         'perf': os.path.join(TOOLS, 'perfmod', 'make_perfmod.py')}[name]
    return p if os.path.isfile(p) else None


def build_fog(romfs, data, preset, tid=lichii.TITLE_ID, log=None):
    """tools\\fogmod\\make_fogmod.py --romfs <romfs> --out <load dir> --preset <preset>  -> "Fog - <Name>"."""
    # v0.8.4: superseded by fog.py (the Fog section builds "DevTool Fog"); kept for the command line only.
    s = script('fog')
    if not s:
        raise FileNotFoundError('tools\\fogmod\\make_fogmod.py not found')
    return _run([sys.executable, s, '--romfs', romfs, '--out', load_dir(data, tid), '--preset', preset], log)


def build_perf(romfs, data, preset, tid=lichii.TITLE_ID, log=None):
    s = script('perf')
    if not s:
        raise FileNotFoundError('tools\\perfmod\\make_perfmod.py not found')
    return _run([sys.executable, s, '--romfs', romfs, '--out', load_dir(data, tid), '--preset', preset], log)


def _run(cmd, log=None):
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=lichii.NOWIN, timeout=1800)
    out = (r.stdout + '\n' + r.stderr).strip()
    if log:
        for ln in out.splitlines()[-12:]:
            log('[build] ' + ln)
    if r.returncode:
        raise RuntimeError('%s failed (%d): %s' % (os.path.basename(cmd[1]), r.returncode, out[-400:]))
    return out


# ---------------------------------------------------------------- Lichii log
def loaded_from_log(data, mods):
    """(layeredfs_seen, [lines]) from the latest boot in eden_log.txt."""
    lines = lichii.log_excerpt(data, 200)
    seen = any('LayeredFS patches applied' in ln or 'Applied RomFS patches' in ln for ln in lines)
    rel = [ln for ln in lines if 'layeredfs' in ln.lower() or 'romfs' in ln.lower() or 'add-on' in ln.lower()]
    return seen, rel
