"""Lichii build status (v0.8.6): a READ-ONLY view of the "Lichii Builds" tray app.

Since Nov 7 the tray app in Desktop\\lichii-checker\\LichiiChecker is the only thing
that downloads or installs Lichii builds:
  * Frizti -> ZELDAOOT\\Lichii Frizti (the launch build)
  * Main   -> ZELDAOOT\\Other builds\\Lichii Main
  * old builds -> Desktop\\ARCHIVE\\Lichii (the tray app moves them itself when it installs)
The dev tool only reads the tray app's state.json (installed tag, latest tag, update available, last
result, per source) and shows it. It never talks to GitHub, never downloads, unpacks or archives.
(v0.5.9-v0.8.5 had their own GitHub checker with Download / Unpack / Archive buttons; it once put a
duplicate build-117 into ZELDAOOT\\Main build-117 because its copied "installed" tag was stale.)

`STATE_FILE` is a module global so a test can point it at a fake state.
"""
import json
import os
import time

import lichii

TRAY_DIR = os.path.join(lichii.DESKTOP, 'lichii-checker', 'LichiiChecker')
STATE_FILE = os.path.join(TRAY_DIR, 'state.json')
REREAD_EVERY = 60          # seconds; reading a local 3 KB file
TRAY_NAME = 'Lichii Builds'

# Rows shown on the Session tab. 'install' is only a fallback for when state.json has no Folder yet.
SOURCES = [
    {'name': 'frizti', 'label': 'Frizti', 'install': lichii.FRIZTI_DIR, 'default': True},
    {'name': 'main', 'label': 'Main', 'install': os.path.join(lichii.ZELDAOOT, 'Other builds', 'Lichii Main'), 'default': False},
]

_last_good = None
_last_logged = {}


def _when(s):
    """'2026-11-07T22:39:01.0000000Z' -> '2026-11-07 22:39' (as written; Published is UTC, LastChecked local)."""
    s = str(s or '')
    return s[:16].replace('T', ' ')


def kind_of(s):
    """'new' | 'busy' | 'error' | 'ok' | 'unknown' for one source's state (the tray app's LastResult texts)."""
    if not s:
        return 'unknown'
    res = str(s.get('LastResult') or '').lower()
    if s.get('UpdateAvailable') or res.startswith('new build'):
        return 'new'
    if res.startswith(('installing', 'waiting')):
        return 'busy'
    if res.startswith(("couldn't", 'could not', 'install failed', 'no windows build', 'skipped')):
        return 'error'
    if s.get('Tag') or res:
        return 'ok'
    return 'unknown'


def read_status(path=None, log=None):
    """Read the tray app's state.json. Returns {name: row, ..., '_file', '_read', '_error'}.
    A row: tag, title, latest_tag, latest_title, latest_url, latest_published, latest_size_mb, update,
    result, last_checked, folder, installed_at, archived_to, kind. Never raises."""
    global _last_good
    path = path or STATE_FILE
    out = {'_file': path, '_read': time.strftime('%H:%M'), '_error': ''}
    try:
        raw = json.load(open(path, encoding='utf-8-sig'))
        if not isinstance(raw, dict):
            raise ValueError('not a JSON object')
    except FileNotFoundError:
        out['_error'] = '%s not found: is %s set up? (Desktop\\lichii-checker\\LichiiChecker)' % (path, TRAY_NAME)
        raw = None
    except Exception as e:
        # the tray app may be rewriting the file right now: keep the last good read
        out['_error'] = 'could not read %s (%s)' % (path, str(e)[:120])
        raw = None
        if _last_good:
            for k, v in _last_good.items():
                if not k.startswith('_'):
                    out[k] = v
            out['_error'] += '; showing the last good read'
    if raw is not None:
        for src in SOURCES:
            s = raw.get(src['name']) or {}
            out[src['name']] = {
                'tag': s.get('Tag') or '', 'title': s.get('Title') or '',
                'latest_tag': s.get('LatestTag') or '', 'latest_title': s.get('LatestTitle') or '',
                'latest_url': s.get('LatestUrl') or '', 'latest_published': _when(s.get('LatestPublishedAt')),
                'latest_size_mb': s.get('LatestSizeMB'), 'update': bool(s.get('UpdateAvailable')),
                'result': s.get('LastResult') or '', 'last_checked': _when(s.get('LastChecked')),
                'folder': s.get('Folder') or '', 'installed_at': _when(s.get('InstalledAt')),
                'archived_to': s.get('ArchivedTo') or '', 'kind': kind_of(s), 'missing': not s}
        _last_good = {k: v for k, v in out.items() if not k.startswith('_')}
    if log:
        for src in SOURCES:
            r = out.get(src['name'])
            line = ('[builds] %s (from %s): installed %s, latest %s, %s' % (
                src['label'], TRAY_NAME, r['tag'] or '?', r['latest_tag'] or '?', r['result'] or 'no result yet')
                if r else '[builds] %s: %s' % (src['label'], out['_error'] or 'not in state.json'))
            if _last_logged.get(src['name']) != line:     # log changes only, not every re-read
                _last_logged[src['name']] = line
                log(line)
    return out


def summary(st):
    """('ok'|'new'|'busy'|'error'|'unknown', text) for the tray-style indicator."""
    rows = [(src['label'], st.get(src['name'])) for src in SOURCES]
    rows = [(l, r) for l, r in rows if r]
    if not rows:
        if st.get('_error'):
            return 'error', st['_error']
        return 'unknown', 'no build state'
    news = [(l, r) for l, r in rows if r['kind'] == 'new']
    if news:
        return 'new', 'new build: ' + ', '.join('%s %s' % (l, r['latest_tag']) for l, r in news) + ' (install it from %s)' % TRAY_NAME
    busy = [(l, r) for l, r in rows if r['kind'] == 'busy']
    if busy:
        return 'busy', '; '.join('%s: %s' % (l, r['result']) for l, r in busy)
    errs = [(l, r) for l, r in rows if r['kind'] == 'error']
    if errs:
        return 'error', '; '.join('%s: %s' % (l, r['result']) for l, r in errs)
    if all(r['kind'] == 'ok' for l, r in rows):
        return 'ok', 'builds up to date'
    return 'unknown', 'builds not checked yet'


def row_text(src, r):
    """One Session-tab line for a source."""
    if not r:
        return 'not in %s state.json' % TRAY_NAME
    if r['missing']:
        return 'not set up in %s' % TRAY_NAME
    k = r['kind']
    if k == 'new':
        t = 'NEW BUILD %s (published %s UTC%s); installed: %s. Install it from the %s tray icon.' % (
            r['latest_tag'], r['latest_published'] or '?', ', %s MB' % r['latest_size_mb'] if r['latest_size_mb'] else '',
            r['tag'] or 'unknown', TRAY_NAME)
    elif k in ('busy', 'error'):
        t = 'installed %s; %s' % (r['tag'] or '?', r['result'])
    else:
        t = 'up to date: %s' % (r['tag'] or '?')
        if r['latest_tag'] and r['latest_tag'] != r['tag']:
            t += ' (GitHub: %s)' % r['latest_tag']
    if r['last_checked']:
        t += '   [checked %s]' % r['last_checked']
    return t


def tag_for(folder, st=None):
    """The build tag for a folder: the tray app's state when its Folder is this folder, else a legacy
    devtool_build.json tag file (folders the v0.5.9 checker unpacked), else ''."""
    st = st if st is not None else read_status()
    for src in SOURCES:
        r = st.get(src['name'])
        if r and r['folder'] and _same_path(r['folder'], folder):
            return r['tag']
    try:
        return json.load(open(os.path.join(folder, 'devtool_build.json'))).get('tag', '')
    except Exception:
        return ''


def _same_path(a, b):
    def n(p):
        if os.sep != '\\':
            p = p.replace('\\', os.sep)
        return os.path.normcase(os.path.normpath(p))
    return n(a) == n(b)
