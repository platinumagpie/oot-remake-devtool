"""Probes: the tool runs them in-game and writes reports to user/, so the next version can be built
from what they found.

GameData probe (v0.4.1)
  prepare(): plants unique 32-bit markers in 16 inert saved values (minigame records, enemy-defeat
             counters, boss-rush times) of one save slot, after a backup, plus two visible edits
             (rupees, Deku seeds) that show at a glance whether the game took the edited file. The
             game must be closed: quitting saves over the slot. You then load that slot in-game.
  run():     searches all guest memory for each marker, tells the game's working copy apart from the
             loaded save-file buffer, looks for each entry's hash next to its value, tests which
             ordering (definition order, hash order, save order) puts the values at a fixed stride,
             follows pointers to the table, puts your original values back (compare-and-write) and
             writes user/probe_gamedata.txt plus raw memory windows in user/dump/.
"""
import json
import os
import re
import struct
import time

import gamedata
import savefile

PLAN = 'gamedata_probe_plan.json'
INT_NAMES = [r'^MiniGame_.*Best', r'^MiniGame_Fishing_RecordFishSize', r'^SubEvent_RunningRace_BestTime',
             r'^DreamWorld_.*Result(Minute|Second)', r'^Minigame_SkullKidSession_Max', r'^DreamWorld_.*BestTime']
INT_NAMES2 = [r'^DefeatedEnemyNum\.Enemy_']          # plain counters, nothing validates them
VISIBLE = [('PouchData.Item_DekuSeed.StockNum', lambda v: 17 if v != 17 else 19),
           ('PlayerStatus.CurrentRupee', lambda v: 77 if v != 77 else 78)]
FLOAT_NAMES = [r'^DreamWorld_.*BestTime', r'^DreamWorld_.*ResultFrame', r'^MiniGame_HorseRace_BestTime$']


def marker_int(i):
    return (gamedata.mmh('DevToolGameDataMarker%d' % i) & 0x0FFFFFFF) | 0x50000000


def marker_float(i):
    return 0x4A000000 | (gamedata.mmh('DevToolGameDataMarkerF%d' % i) & 0x7FFFFF)


def choose(save, defs, names, n_int=6, n_int2=6, n_float=4):
    def pick(typ, pats, n):
        out = []
        for pat in pats:
            for e in save.entries:
                nm = names.get(e.hash) or ''
                if e.type == typ and re.search(pat, nm) and e not in out:
                    out.append(e)
        return out[:n]
    return pick('Int', INT_NAMES, n_int) + pick('Int', INT_NAMES2, n_int2) + pick('Float', FLOAT_NAMES, n_float)


def _md5(path):
    import hashlib
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


def prepare(save_path, defs, names, user_dir):
    """Writes markers into the save (backup first) and the plan to user/gamedata_probe_plan.json."""
    s = savefile.SaveFile(save_path)
    chosen = choose(s, defs, names)
    if len(chosen) < 6:
        raise RuntimeError('only %d probe entries found in this save' % len(chosen))
    plan = {'version': 2, 'save': save_path, 'prepared': time.strftime('%Y-%m-%d %H:%M:%S'),
            'save_size': len(s.data), 'data_offset': s.data_offset, 'entries': [], 'visible': []}
    for name, newv in VISIBLE:
        e = s.by_hash.get(gamedata.mmh(name))
        if e is not None and e.type == 'Int':
            orig = s.raw(e)
            nv = newv(orig)
            struct.pack_into('<I', s.data, e.pos + 4, nv)
            plan['visible'].append({'name': name, 'hash': e.hash, 'type': e.type, 'orig': orig, 'new': nv,
                                    'save_pos': e.pos})
    ni = nf = 0
    for e in chosen:
        orig = s.raw(e)
        if e.type == 'Int':
            mk, ni = marker_int(ni), ni + 1
        else:
            mk, nf = marker_float(nf), nf + 1
        struct.pack_into('<I', s.data, e.pos + 4, mk)
        plan['entries'].append({'name': names.get(e.hash), 'hash': e.hash, 'type': e.type, 'orig': orig,
                                'marker': mk, 'save_pos': e.pos})
    s.dirty = True
    s.save(backup_root=os.path.join(user_dir, 'backups'))
    plan['md5'] = _md5(save_path)
    json.dump(plan, open(os.path.join(user_dir, PLAN), 'w'), indent=1)
    return plan


def load_plan(user_dir):
    p = os.path.join(user_dir, PLAN)
    return json.load(open(p)) if os.path.exists(p) else None


# ---------------------------------------------------------------- run
def _window(dbg, addr, before=0x80, after=0x80):
    """Bytes around addr (shrinks to the page if the wider read hits an unmapped page)."""
    for b, a in ((before, after), (min(before, addr & 0xFFF), min(after, 0x1000 - (addr & 0xFFF)))):
        try:
            return addr - b, dbg.read(addr - b, b + a)
        except Exception:
            continue
    return addr, b''


def _orders(defs, save, typ):
    """Candidate index orders for the runtime table of one type."""
    gdl = [h for h, d in defs.items() if d.type == typ]
    return {
        'definition order (all %s)' % typ: {h: i for i, h in enumerate(gdl)},
        'definition order (saved %s only)' % typ: {h: i for i, h in enumerate([h for h in gdl if defs[h].save_index >= 0])},
        'hash order (all %s)' % typ: {h: i for i, h in enumerate(sorted(gdl))},
        'hash order (saved %s only)' % typ: {h: i for i, h in enumerate(sorted(h for h in gdl if defs[h].save_index >= 0))},
        'save-file order': {e.hash: i for i, e in enumerate([e for e in save.entries if e.type == typ])},
    }


def _fit(points):
    """points: [(index, addr)]. Returns (base, stride) if all lie on addr = base + index*stride."""
    if len(points) < 3:
        return None
    pts = sorted(points)
    (i0, a0), (i1, a1) = pts[0], pts[-1]
    if i1 == i0 or (a1 - a0) % (i1 - i0):
        return None
    stride = (a1 - a0) // (i1 - i0)
    if stride <= 0 or stride > 0x400:
        return None
    base = a0 - i0 * stride
    if all(base + i * stride == a for i, a in pts):
        return base, stride
    return None


def _read_big(dbg, addr, size):
    out = bytearray()
    while len(out) < size:
        n = min(0x10000, size - len(out))
        try:
            out += dbg.read(addr + len(out), n)
        except Exception:
            break
    return bytes(out)


def _save_images(dbg, plan, say, dump):
    """Copies of the slot in save-file format in RAM (found from a known entry's hash and its
    position in the file). Returns [(base, SaveFile)]."""
    images = []
    seen = set()
    for ent in (plan.get('visible') or []) + plan['entries'][:2]:
        say('looking for the save image in RAM (%s)' % ent['name'])
        hits, _ = dbg.search(struct.pack('<I', ent['hash']), regions='all', max_hits=32, total_ms=90000)
        for h in hits:
            base = h - ent['save_pos']
            if base in seen:
                continue
            try:
                if dbg.read(base, 4) != struct.pack('<I', 0x01020304):
                    continue
            except Exception:
                continue
            seen.add(base)
            data = _read_big(dbg, base, plan.get('save_size') or 0x20000)
            open(os.path.join(dump, 'gd_saveimage_%x.bin' % base), 'wb').write(data)
            try:
                images.append((base, savefile.SaveFile('<RAM %#x>' % base, data)))
            except Exception:
                pass
        if images:
            break
    return images


def _compare_image(img, plan):
    """How many probe values in a save image are the prepared ones, the originals, or other."""
    prep = orig = other = 0
    rows = []
    for ent in plan['entries'] + (plan.get('visible') or []):
        e = img.by_hash.get(ent['hash'])
        if e is None:
            continue
        v = img.raw(e)
        want = ent.get('marker', ent.get('new'))
        if v == want:
            prep += 1
        elif v == ent['orig']:
            orig += 1
        else:
            other += 1
        if 'new' in ent:
            rows.append('%s = %d (original %d, prepared %d)' % (ent['name'], v, ent['orig'], ent['new']))
    return prep, orig, other, rows


def _int_orders(defs, save):
    gdl = [h for h, d in defs.items() if d.type == 'Int']
    sv = [e.hash for e in save.entries if e.type == 'Int'] if save else []
    return gdl, {h: i for i, h in enumerate(gdl)}, sv


def _classify_hash_hits(dbg, defs, save, h_ent, in_module):
    """What sits right after each copy of a hash: tells index maps, hash arrays and images apart."""
    gdl, gidx, sv = _int_orders(defs, save)
    nxt_save = {sv[i]: sv[i + 1] for i in range(len(sv) - 1)}
    nxt_gdl = {gdl[i]: gdl[i + 1] for i in range(len(gdl) - 1)}
    hits, _ = dbg.search(struct.pack('<I', h_ent), regions='all', max_hits=32, total_ms=90000)
    rows = []
    for h in hits:
        if in_module(h):
            rows.append((h, 'code constant in %s' % in_module(h), None))
            continue
        try:
            nxt = struct.unpack('<I', dbg.read(h + 4, 4))[0]
        except Exception:
            rows.append((h, 'unreadable', None))
            continue
        if nxt == gidx.get(h_ent):
            kind = 'index map: hash -> GDL index %d' % nxt
        elif nxt == nxt_save.get(h_ent):
            kind = 'hash array in save order'
        elif nxt == nxt_gdl.get(h_ent):
            kind = 'hash array in GDL order'
        else:
            kind = 'pair/other: next u32 = %08X' % nxt
        rows.append((h, kind, nxt))
    return rows


def _pattern_search(dbg, defs, img, say, want=3):
    """Finds the runtime Int value array by its contents: 16 consecutive values in GDL order (from
    the save image) as one search pattern. Returns [(base, stride, hits_verified)]."""
    gdl = [h for h, d in defs.items() if d.type == 'Int']
    vals = {e.hash: img.raw(e) for e in img.entries if e.type == 'Int'}
    windows = []
    for st in range(0, len(gdl) - 16):
        w = gdl[st:st + 16]
        if all(h in vals for h in w):
            seq = [vals[h] for h in w]
            d = len(set(seq))
            if d >= 5:
                windows.append((d, st, seq))
    windows.sort(reverse=True)
    found = []
    for d, st, seq in windows[:want]:
        say('searching for the Int value array (GDL index %d)' % st)
        hits, _ = dbg.search(struct.pack('<16I', *[v & 0xFFFFFFFF for v in seq]), regions='all', max_hits=8,
                             total_ms=90000)
        for h in hits:
            base = h - st * 4
            ok = tot = 0
            for i, hh in enumerate(gdl):
                if hh in vals and i % 37 == 0:
                    tot += 1
                    try:
                        ok += struct.unpack('<I', dbg.read(base + i * 4, 4))[0] == vals[hh] & 0xFFFFFFFF
                    except Exception:
                        pass
            found.append((base, 4, '%d/%d spot checks match' % (ok, tot)))
        if found:
            break
    if found:
        return found
    # The 1.1.1 layout: one object per entry with (default, live value, copy) side by side, so an entry
    # whose saved value differs from its default shows up as the 12 bytes default|value|value.
    picks = []
    for i, h in enumerate(gdl):
        d = defs[h]
        if h in vals and d.default is not None and (vals[h] & 0xFFFFFFFF) != (int(d.default) & 0xFFFFFFFF):
            picks.append((i, int(d.default) & 0xFFFFFFFF, vals[h] & 0xFFFFFFFF))
    picks.sort(key=lambda x: -(x[2] not in (0, 1)) * 1000 - (x[2] > 255))   # unusual values first
    for i, dv, v in picks[:want]:
        say('searching for the Int records (GDL index %d)' % i)
        hits, _ = dbg.search(struct.pack('<3I', dv, v, v), regions='all', max_hits=8, total_ms=90000)
        informative = [(j, vals[hh] & 0xFFFFFFFF) for j, hh in enumerate(gdl) if vals.get(hh, 0) & 0xFFFFFFFF]
        informative = informative[::max(1, len(informative) // 40)]
        best = None
        for h in hits:
            for stride in (0x28, 0x30, 0x20, 0x38, 0x40):
                base = h + 4 - i * stride
                ok = 0
                for j, v in informative:
                    try:
                        ok += struct.unpack('<I', dbg.read(base + j * stride, 4))[0] == v
                    except Exception:
                        pass
                if informative and ok >= len(informative) * 0.95 and (best is None or ok > best[0]):
                    best = (ok, base, stride)
        if best:
            found.append((best[1], best[2], '%d/%d non-zero spot checks match' % (best[0], len(informative))))
            break
    return found


def run(dbg, host, plan, defs, modules, user_dir, say=print, shot=None):
    rep = ['GameData probe  %s' % time.strftime('%Y-%m-%d %H:%M:%S'),
           'save: %s (prepared %s)' % (plan['save'], plan['prepared'])]
    dump = os.path.join(user_dir, 'dump')
    os.makedirs(dump, exist_ok=True)
    try:
        save_now = savefile.SaveFile(plan['save'])
        file_bytes = bytes(save_now.data)
    except Exception as e:
        save_now, file_bytes = None, b''
        rep.append('note: cannot reread the save (%s)' % e)
    mods = [(b, b + s, n) for n, (b, s) in modules.items()]
    in_module = lambda a: next((n for lo, hi, n in mods if lo <= a < hi), None)
    found = {}      # entry index -> [hit dicts]
    out = {'entries': [], 'layout': {}, 'pointers': {}, 'restore': [], 'images': []}
    entries = plan['entries']
    # ---- did the game write the slot after "Prepare"? (quitting the game saves over it)
    if plan.get('md5'):
        try:
            changed = _md5(plan['save']) != plan['md5']
            out['slot_rewritten'] = changed
            rep.append('slot file: %s' % ('CHANGED since "Prepare" (the game saved over it)' if changed
                                          else 'unchanged since "Prepare"'))
        except OSError as e:
            rep.append('slot file: cannot read (%s)' % e)
    # ---- copies of the slot in save-file format in RAM: did the game load the prepared values?
    images = _save_images(dbg, plan, say, dump)
    img_ranges = []
    for base, img in images:
        prep, orig, other, rows = _compare_image(img, plan)
        img_ranges.append((base, base + img.data_offset))
        out['images'].append({'addr': base, 'prepared': prep, 'original': orig, 'other': other})
        rep.append('save image in RAM at %#x: %d prepared values, %d original values, %d other' % (base, prep, orig, other))
        for r in rows:
            rep.append('    ' + r)
    if not images:
        rep.append('no save image found in RAM')
    rep.append('')
    for k, ent in enumerate(entries):
        say('searching marker %d/%d (%s)' % (k + 1, len(entries), ent['name']))
        pat = struct.pack('<I', ent['marker'])
        hits, complete = dbg.search(pat, regions='all', max_hits=32, total_ms=90000)
        hs = []
        for h in hits:
            wbase, w = _window(dbg, h)
            rel = h - wbase
            kind = 'module:' + in_module(h) if in_module(h) else 'heap'
            # the loaded save buffer: the bytes around match the file around this entry
            sp = ent['save_pos'] + 4
            if file_bytes and len(w) >= rel + 4:
                lo = max(0, rel - 0x40)
                seg = w[lo:rel + 0x40]
                fseg = file_bytes[sp - (rel - lo):sp - (rel - lo) + len(seg)]
                if seg == fseg:
                    kind = 'save-buffer'
            if any(lo <= h < hi for lo, hi in img_ranges):
                kind = 'save-buffer'
            deltas = [i - rel for i in range(0, len(w) - 3, 4) if struct.unpack_from('<I', w, i)[0] == ent['hash']]
            hs.append({'addr': h, 'kind': kind, 'hash_at': deltas, 'window': (wbase, w)})
        found[k] = hs
        rep.append('%-60s %-5s hash %08X marker %08X: %d hit(s)%s' % (
            ent['name'], ent['type'], ent['hash'], ent['marker'], len(hits), '' if complete else ' (search incomplete)'))
        for x in hs:
            rep.append('    %#014x  %-12s hash at %s' % (x['addr'], x['kind'], x['hash_at'] or 'not within +-0x80'))
    # ---- layout: paired hash/value?
    work = {k: [x for x in v if x['kind'] == 'heap'] for k, v in found.items()}
    deltas = {}
    for k, v in work.items():
        for x in v:
            for d in x['hash_at']:
                deltas[d] = deltas.get(d, 0) + 1
    rep.append('')
    rep.append('working-copy hits (not the save buffer): %d of %d entries' % (sum(1 for v in work.values() if v), len(entries)))
    if deltas:
        best = max(deltas.items(), key=lambda kv: kv[1])
        rep.append('hash found next to the value: offset %+d in %d hit(s)  (all: %s)' % (best[0], best[1], deltas))
        out['layout']['hash_delta'] = best[0]
    else:
        rep.append('hash never next to the value: values are stored apart from their hashes')
    # ---- layout: which order puts values at a fixed stride?
    for typ in ('Int', 'Float'):
        ks = [k for k, e in enumerate(entries) if e['type'] == typ and work.get(k)]
        if len(ks) < 3 or save_now is None:
            continue
        for oname, order in _orders(defs, save_now, typ).items():
            if not all(entries[k]['hash'] in order for k in ks):
                continue
            # try every combination of one hit per entry (usually one hit each)
            import itertools
            combos = itertools.product(*[[x['addr'] for x in work[k]] for k in ks])
            for n, combo in enumerate(combos):
                if n > 2000:
                    break
                fit = _fit([(order[entries[k]['hash']], a) for k, a in zip(ks, combo)])
                if fit:
                    base, stride = fit
                    rep.append('%s table: %s fits: base %#x, stride %d' % (typ, oname, base, stride))
                    out['layout'].setdefault(typ, []).append({'order': oname, 'base': base, 'stride': stride,
                                                              'count': len(order)})
                    break
        if typ not in out['layout']:
            rep.append('%s table: no tested order fits the hit addresses' % typ)
    # ---- what structures hold an Int's hash? (index map, hash arrays, images)
    ref = next((v for v in (plan.get('visible') or []) if v['type'] == 'Int'), None) or entries[0]
    say('classifying the copies of a hash')
    try:
        rows = _classify_hash_hits(dbg, defs, save_now, ref['hash'], in_module)
        out['hash_structures'] = [{'addr': h, 'kind': k} for h, k, _n in rows]
        rep.append('copies of the hash of %s:' % ref['name'])
        for h, k, _n in rows:
            rep.append('    %#014x  %s' % (h, k))
    except Exception as e:
        rep.append('hash classification failed: %s' % e)
    # ---- the Int value array by its contents, if the markers did not give it
    if 'Int' not in out['layout'] and (images or save_now):
        src = images[0][1] if images else save_now
        try:
            pats = _pattern_search(dbg, defs, src, say)
            for base, stride, note in pats:
                rep.append('Int value array by pattern: base %#x, stride %d (%s)' % (base, stride, note))
                out['layout'].setdefault('Int', []).append({'order': 'definition order (all Int), by pattern',
                                                            'base': base, 'stride': stride})
            if not pats:
                rep.append('Int value array by pattern: not found (values are not a plain 4-byte array in GDL order)')
        except Exception as e:
            rep.append('pattern search failed: %s' % e)
    # ---- live write test on a value you can see on the HUD (Deku seed count)
    seed = gamedata.mmh('PouchData.Item_DekuSeed.StockNum')
    lay = (out['layout'].get('Int') or [None])[0]
    gdl, gidx, _sv = _int_orders(defs, save_now)
    if lay and host is not None and seed in gidx and 'definition order (all Int)' in lay['order']:
        a = lay['base'] + gidx[seed] * lay['stride']
        try:
            cur = struct.unpack('<I', dbg.read(a, 4))[0]
            new = cur - 3 if cur >= 3 else cur + 3
            host.write(a, struct.pack('<I', new))
            time.sleep(2.0)
            after = struct.unpack('<I', dbg.read(a, 4))[0]
            def snap(label):
                if shot:
                    try:
                        rep.append('%s screenshot: %s' % (label.split('_')[1], shot(label)))
                    except Exception as e:
                        rep.append('screenshot %s failed: %s' % (label, e))
            snap('live_write_seeds_%d' % new)
            if struct.unpack('<I', dbg.read(a, 4))[0] == new:
                host.write(a, struct.pack('<I', cur))
            time.sleep(1.0)
            snap('live_restore_seeds_%d' % cur)
            out['live_write'] = {'addr': a, 'was': cur, 'wrote': new, 'after_2s': after}
            rep.append('live write test: Deku seeds at %#x: %d -> wrote %d -> %d after 2 s -> restored %d' % (
                a, cur, new, after, cur))
        except Exception as e:
            rep.append('live write test failed: %s' % e)
    rep.append('')
    # ---- where do the hashes themselves live? (matters if values are stored apart from hashes)
    sample = [k for k, e in enumerate(entries) if e['type'] == 'Int'][:2] + \
             [k for k, e in enumerate(entries) if e['type'] == 'Float'][:1]
    for k in sample:
        ent = entries[k]
        say('searching for the hash of %s' % ent['name'])
        hits, _ = dbg.search(struct.pack('<I', ent['hash']), regions='all', max_hits=32, total_ms=90000)
        rows = []
        for h in hits:
            wb, w = _window(dbg, h, 0x40, 0x40)
            rel = h - wb
            kind = 'module:' + in_module(h) if in_module(h) else 'heap'
            sp = ent['save_pos']
            if file_bytes and w[max(0, rel - 0x20):rel + 0x20] == file_bytes[sp - min(rel, 0x20):sp + 0x20][:len(w[max(0, rel - 0x20):rel + 0x20])]:
                kind = 'save-buffer'
            rows.append('%#x %s' % (h, kind))
            if kind == 'heap' and len(rows) <= 6:
                fn = 'gd_hash_%08x_%x.bin' % (ent['hash'], h & ~0xFFF)
                try:
                    open(os.path.join(dump, fn), 'wb').write(dbg.read(h & ~0xFFF, 0x1000))
                except Exception:
                    pass
        out.setdefault('hash_hits', {})[ent['name']] = rows
        rep.append('hash %08X (%s) found at: %s' % (ent['hash'], ent['name'], ', '.join(rows) or 'nowhere'))
    # ---- raw windows for offline study: 64 KiB around the first working hit of each type
    for typ in ('Int', 'Float'):
        for k, e in enumerate(entries):
            if e['type'] == typ and work.get(k):
                a = work[k][0]['addr'] & ~0xFFF
                lo = a - 0x8000
                try:
                    blob = dbg.read(lo, 0x10000)
                except Exception:
                    lo, blob = a, dbg.read(a, 0x1000)
                fn = 'gd_%s_%x.bin' % (typ.lower(), lo)
                open(os.path.join(dump, fn), 'wb').write(blob)
                rep.append('window %s: %s (%#x..%#x)' % (typ, fn, lo, lo + len(blob)))
                break
    # ---- who points at the tables?
    for typ, fits in list(out['layout'].items()):
        if not isinstance(fits, list):
            continue
        for f in fits[:1]:
            base = f['base']
            say('looking for pointers to the %s table' % typ)
            ptrs = {}
            hd = out['layout'].get('hash_delta', 0)
            cands = {base, base + min(hd, 0), base & ~0xF, base - 0x8}
            stride = f.get('stride', 4)
            if stride > 4:   # an array of records: the pointer goes to the first record, before the value
                cands |= {base - k for k in range(4, stride, 4) if (base - k) % 8 == 0}
            for cand in sorted(cands):
                hits, _ = dbg.search(struct.pack('<Q', cand), regions='all', max_hits=16, total_ms=60000)
                for h in hits:
                    ptrs.setdefault(cand, []).append(h)
            out['pointers'][typ] = {hex(c): [hex(h) for h in v] for c, v in ptrs.items()}
            for c, v in ptrs.items():
                rep.append('pointers to %#x (%s table%s): %s' % (c, typ, '' if c == base else ' %+d' % (c - base),
                           ' '.join('%#x%s' % (h, ' [%s]' % in_module(h) if in_module(h) else '') for h in v) or 'none'))
                for h in v[:4]:
                    wb, w = _window(dbg, h, 0x100, 0x100)
                    fn = 'gd_ptr_%s_%x.bin' % (typ.lower(), wb)
                    open(os.path.join(dump, fn), 'wb').write(w)
    # ---- put the original values back (compare-and-write), then check they stick
    if host is None:
        rep.append('restore: skipped (writes unavailable); the markers stay until the slot is restored from '
                   'user\\backups or the values are changed in-game')
    else:
        written = []
        for k, ent in enumerate(entries):
            for x in work.get(k, []):
                if x['addr'] % 4:
                    rep.append('restore %s at %#x: unaligned chance match, left alone' % (ent['name'], x['addr']))
                    continue
                try:
                    near = struct.unpack('<3I', host.read(x['addr'] - 4, 12))
                except OSError as e:
                    rep.append('restore %s at %#x: read failed (%s)' % (ent['name'], x['addr'], e))
                    continue
                cur = near[1]
                if cur == ent['marker'] and ent['marker'] not in (near[0], near[2]):
                    # real GameData records hold the value twice (+0x1C live, +0x20 copy); a lone match is chance
                    rep.append('restore %s at %#x: lone chance match, left alone' % (ent['name'], x['addr']))
                    continue
                if cur != ent['marker']:
                    rep.append('restore %s at %#x: value changed to %08X, left alone' % (ent['name'], x['addr'], cur))
                    continue
                try:
                    host.write(x['addr'], struct.pack('<I', ent['orig']))
                    written.append((ent, x['addr']))
                except OSError as e:
                    rep.append('restore %s at %#x: write failed (%s)' % (ent['name'], x['addr'], e))
        time.sleep(1.5)
        for ent, a in written:
            try:
                v = struct.unpack('<I', dbg.read(a, 4))[0]
            except Exception:
                v = None
            ok = v == ent['orig']
            out['restore'].append({'name': ent['name'], 'addr': a, 'stuck': ok})
            rep.append('restore %-58s %#x -> %08X: %s' % (ent['name'], a, ent['orig'],
                       'OK, still there after 1.5 s' if ok else 'reverted to %s' % ('%08X' % v if v is not None else '?')))
    # ---- hex around every working hit, for the record
    rep.append('')
    rep.append('bytes around each working hit (value at the middle of the line):')
    for k, ent in enumerate(entries):
        for x in work.get(k, [])[:2]:
            wb, w = x['window']
            rel = x['addr'] - wb
            rep.append('  %s @%#x: %s | %s' % (ent['name'], x['addr'], w[max(0, rel - 32):rel].hex(' '),
                                              w[rel:rel + 32].hex(' ')))
    out['entries'] = [{'name': e['name'], 'hash': e['hash'], 'type': e['type'],
                       'hits': [{'addr': x['addr'], 'kind': x['kind'], 'hash_at': x['hash_at']} for x in found.get(k, [])]}
                      for k, e in enumerate(entries)]
    open(os.path.join(user_dir, 'probe_gamedata.txt'), 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    json.dump(out, open(os.path.join(user_dir, 'probe_gamedata.json'), 'w'), indent=1, default=str)
    return out, rep


def scan_pointers(ram_dump, main_base, lo, hi, segs):
    """[(main offset, value)]: pointers in main's .data/.bss (from a RAM dump) whose value is in [lo, hi)."""
    out = []
    spans = [segs['data'], segs['bss']] if len(ram_dump) >= segs['data'][0] else [(0, len(ram_dump))]
    for a, b in spans:
        for off in range(a & ~7, min(b, len(ram_dump)) - 7, 8):
            v = struct.unpack_from('<Q', ram_dump, off)[0]
            if lo <= v < hi:
                out.append((off, v))
    return out
