"""Rebuild names.tsv: recover GameData flag names by hashing strings found in the game files.

    python names_build.py [path\\to\\romfs]

Scans every data file in the loose romfs (decompressing .zs and opening SARC packs), pulls out
identifier-like strings and their pieces, hashes each with murmur3 and keeps the ones that
match a GameData hash. Re-run it after a game update. Takes a few minutes.

Extra candidates (no romfs scan needed, use --lists-only for a quick merge):
  data/name_lists/*.txt          one candidate per line (e.g. totk_names.txt, from the public TotK
                                 hash lists by MacSpazzy/MrCheeze and lotuskit)
  user/dump/main_*_strings.txt   strings from the game's own code (Session tab: Ghidra ELF / RAM dump)
Struct members are resolved too: a candidate whose hash matches a member of a known struct gives
"<Struct>.<candidate>" (checked against the full entry hash).
"""
import glob
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gamedata  # noqa: E402

SKIP_DIRS = {'Model', 'Tex', 'Shader', 'Sound', 'Movie', 'Bake', 'TerrainArc', 'Voice',
             'Foliage', 'PreComputedCulling', 'VolumeStats', 'Font2', 'Layout2Shader', 'Lib',
             'Effect', 'Phive', 'Chemical'}
SKIP_EXT = ('.bfres.zs', '.bntx.zs', '.bfres', '.bntx', '.bfsha', '.webm', '.bwav', '.bars',
            '.bfstm', '.ptcl')
RX = re.compile(rb'[A-Za-z_][A-Za-z0-9_.\-]{2,120}')
RXW = re.compile(rb'(?:[A-Za-z_]\x00)(?:[A-Za-z0-9_.\-]\x00){2,120}')  # UTF-16 names


def pieces(s):
    yield s
    for sep in ('.', '/', '-'):
        if sep in s:
            for p in s.split(sep):
                if len(p) > 2:
                    yield p
    # TotK-style "Name.Suffix" and "Prefix_Name" splits
    parts = s.split('_')
    if len(parts) > 2:
        for i in range(1, len(parts)):
            yield '_'.join(parts[:i])
            yield '_'.join(parts[i:])


def harvest(blob, out):
    for m in RX.findall(blob):
        out.add(m.decode('ascii', 'ignore'))
    for m in RXW.findall(blob):
        out.add(m.decode('utf-16-le', 'ignore'))


def extra_candidates():
    """Candidate names from data/name_lists/*.txt and user/dump/main_*_strings.txt."""
    out = set()
    for p in glob.glob(os.path.join(HERE, 'data', 'name_lists', '*.txt')):
        for line in open(p, encoding='utf-8', errors='replace'):
            line = line.strip()
            if line and not line.startswith('#') and '*' not in line:
                out.add(line)
    for p in glob.glob(os.path.join(HERE, 'user', 'dump', 'main_*_strings.txt')):
        for line in open(p, encoding='utf-8', errors='replace'):
            if line.startswith('#') or '\t' not in line:
                continue
            out.add(line.rstrip('\n').split('\t', 1)[1])
    return out


def struct_members(defs, found, cands):
    """Names "<Struct>.<member>" for candidates that hash to a member of a named struct."""
    by_member = {}
    for d in defs.values():
        if d.members:
            for mh, fh in d.members:
                by_member.setdefault(mh, []).append((d.hash, fh))
    hits = {}
    for c in cands:
        for p in pieces(c):
            lst = by_member.get(gamedata.mmh(p))
            if not lst:
                continue
            for sh, fh in lst:
                sname = found.get(sh)
                if sname and fh not in found and fh not in hits:
                    full = sname + '.' + p
                    if gamedata.mmh(full) == fh:
                        hits[fh] = full
    return hits


def main(romfs, only=None, merge=False, lists_only=False):
    t0 = time.time()
    defs, meta = gamedata.load_definitions(romfs)
    known = set(defs)
    for d in defs.values():
        if d.values:
            known.update(d.values)
    strings = set()
    nfiles = 0
    rf = None if lists_only else gamedata.RomFS(romfs)
    for root, dirs, files in ([] if lists_only else os.walk(romfs)):
        rel = os.path.relpath(root, romfs).replace('\\', '/')
        top = rel.split('/')[0]
        if rel != '.' and (top in SKIP_DIRS or (only and top not in only)):
            dirs[:] = []
            continue
        for fn in files:
            if fn.lower().endswith(SKIP_EXT):
                continue
            p = os.path.join(root, fn)
            try:
                if os.path.getsize(p) > 64 << 20:
                    continue
                b = open(p, 'rb').read()
                if fn.endswith('.zs'):
                    b = rf.decompress(b, fn)
                if b[:4] == b'SARC':
                    for name, sub in gamedata._sarc(b).items():
                        strings.add(name)
                        if sub[:4] == b'SARC':
                            continue
                        harvest(sub, strings)
                else:
                    harvest(b, strings)
            except Exception:
                continue
            strings.add(fn)
            nfiles += 1
            if nfiles % 5000 == 0:
                print('  %d files, %d strings, %.0fs' % (nfiles, len(strings), time.time() - t0),
                      flush=True)
    extra = extra_candidates()
    strings |= extra
    found = {}
    out = os.path.join(HERE, 'names.tsv')
    if (merge or lists_only) and os.path.exists(out):
        for line in open(out, encoding='utf-8'):
            if line[:1] != '#' and '\t' in line:
                h, n = line.rstrip('\n').split('\t', 1)
                found[int(h, 16)] = n
    for s in strings:
        for p in pieces(s):
            h = gamedata.mmh(p)
            if h in known and h not in found:
                found[h] = p
    for _round in range(3):   # struct members of structs named in the previous round
        add = struct_members(defs, found, strings)
        if not add:
            break
        found.update(add)
    if lists_only:
        _write(out, found, meta, defs, nfiles, len(strings), t0)
        return
    # Phase 2: dotted names are struct members ("PouchData.Item_Token_Gold.StockNum").
    # Turn each into a template with one identifier slot and fill it with other identifiers.
    # Only fill a slot with identifiers of the same family (Item_ -> Item_*, Npc_ -> Npc_*),
    # which keeps random 32-bit hash collisions (false names) down to a handful.
    ident = re.compile(r'^[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+$')
    fam = {}
    for s in strings:
        for p in pieces(s):
            if ident.match(p) and len(p) < 64:
                fam.setdefault(p.split('_', 1)[0], set()).add(p)
    templates = set()
    for n in list(found.values()):
        parts = n.split('.')
        if len(parts) < 2:
            continue
        for i, p in enumerate(parts):
            if ident.match(p):
                templates.add(('.'.join(parts[:i] + ['{}'] + parts[i + 1:]), p.split('_', 1)[0]))
    print('  phase 2: %d templates' % len(templates), flush=True)
    t1 = time.time()
    for tpl, family in sorted(templates):
        pre, post = tpl.split('{}')
        for x in fam.get(family, ()):
            c = pre + x + post
            h = gamedata.mmh(c)
            if h in known and h not in found:
                found[h] = c
        if time.time() - t1 > 600:
            print('  phase 2 stopped after 10 minutes', flush=True)
            break
    _write(out, found, meta, defs, nfiles, len(strings), t0)


def _write(out, found, meta, defs, nfiles, nstrings, t0):
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('# GameData names recovered from %s (%s) by names_build.py\n'
                % (meta.get('file'), time.strftime('%Y-%m-%d')))
        for h in sorted(found, key=lambda h: found[h].lower()):
            f.write('%08X\t%s\n' % (h, found[h]))
    os.replace(tmp, out)
    flags = sum(1 for h in found if h in defs)
    print('%d files, %d strings -> %d names (%d of %d GameData entries) in %.0fs -> %s'
          % (nfiles, nstrings, len(found), flags, len(defs), time.time() - t0, out))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('romfs', nargs='?')
    ap.add_argument('--only', help='comma-separated top-level romfs folders to scan')
    ap.add_argument('--merge', action='store_true', help='add to the existing names.tsv')
    ap.add_argument('--lists-only', action='store_true',
                    help='skip the romfs scan: only merge data/name_lists and main strings (fast)')
    a = ap.parse_args()
    if not a.romfs:
        import lichii
        a.romfs = lichii.find_romfs(lichii.ZELDAOOT)
    main(a.romfs, set(a.only.split(',')) if a.only else None, a.merge, a.lists_only)
