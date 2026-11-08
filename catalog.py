"""Game catalog: the lists every menu of the tool reads, rebuilt from your own dump.

    data/catalog_<build16>.json
      bancs      {map: {"main": <banc file>, "startpos": {name: {"pos": [x,y,z], "rot": [x,y,z]}}}}
      items      [{"name", "category", "tab", "get_type", "bottle", "assignable", "bundle"}]
      events     {event name: {"flow": <flowchart>, "entry": <entry point>, "components": <list>}}
      ai_nodes   [node type names]                       (AI/NodeDefinition)
      event_api  {actor: {"actions": {...}, "queries": {...}}, "params": {...}}   (needs: pip install evfl)
Rebuild after a game update (Session tab: "Rebuild game catalog").
"""
import glob
import json
import os
import time

import byml
import gamedata


def _v(x):
    if isinstance(x, dict):
        return {str(k): _v(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_v(i) for i in x]
    if hasattr(x, 'v'):
        return x.v.hex() if isinstance(x.v, bytes) else x.v
    return x


def _rsdb(rf, romfs, name):
    c = sorted(glob.glob(os.path.join(romfs, 'RSDB', name + '.Product.*.rstbl.byml.zs')))
    if not c:
        return []
    return _v(byml.Byml(rf.read(os.path.relpath(c[-1], romfs))).tree())


def path(tool_dir, build_id):
    return os.path.join(tool_dir, 'data', 'catalog_%s.json' % build_id[:16])


def load(tool_dir, build_id):
    p = path(tool_dir, build_id)
    return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None


def event_api(rf, romfs, progress=None):
    import collections
    import evfl
    files = sorted(glob.glob(os.path.join(romfs, 'Event', 'EventFlow', '*.bfevfl.zs')))
    acts = collections.defaultdict(collections.Counter)
    qs = collections.defaultdict(collections.Counter)
    params = collections.defaultdict(set)
    for i, f in enumerate(files):
        if progress and i % 50 == 0:
            progress('event flows %d/%d' % (i, len(files)))
        ef = evfl.EventFlow()
        try:
            ef.read(rf.read(os.path.relpath(f, romfs)))
        except Exception:
            continue
        fc = ef.flowchart
        if fc is None:
            continue
        for a in fc.actors:
            for x in a.actions:
                acts[a.identifier.name][x.v] += 1
            for x in a.queries:
                qs[a.identifier.name][x.v] += 1
        for ev in fc.events:
            d = ev.data
            if isinstance(d, evfl.event.ActionEvent):
                nm = d.actor.v.identifier.name + '::' + d.actor_action.v.v
            elif isinstance(d, evfl.event.SwitchEvent):
                nm = d.actor.v.identifier.name + '::' + d.actor_query.v.v
            else:
                continue
            if getattr(d, 'params', None) is not None:
                params[nm].update(d.params.data.keys())
    return {'flows': len(files),
            'actors': {k: {'actions': dict(acts[k]), 'queries': dict(qs.get(k, {}))} for k in acts},
            'params': {k: sorted(v) for k, v in params.items()}}


def build(romfs, tool_dir, build_id, progress=None, with_events=True):
    t0 = time.time()
    rf = gamedata.RomFS(romfs)
    say = progress or (lambda s: None)
    say('maps and start positions')
    sp = _v(byml.Byml(rf.read('Banc/BancBasedResource/StartPos.byml.zs')).tree())
    bancs = {}
    for row in _rsdb(rf, romfs, 'GameBancInfo'):
        bancs[row.get('__RowId')] = {'main': row.get('BancMain', ''), 'startpos': {}}
    for b, poses in sp.items():
        e = bancs.setdefault(b, {'main': '', 'startpos': {}})
        for name, v in poses.items():
            e['startpos'][name] = {'pos': v.get('Trans'), 'rot': v.get('Rot')}
    say('items')
    items = []
    for row in _rsdb(rf, romfs, 'PouchActorInfo'):
        items.append({'name': row.get('__RowId'), 'category': row.get('PouchCategory'),
                      'tab': row.get('PouchTabCategory'), 'get_type': row.get('PouchGetType'),
                      'bottle': row.get('BottleItem'), 'assignable': row.get('AssignableItem'),
                      'bundle': row.get('BundleActorNum')})
    say('events')
    events = {}
    for row in _rsdb(rf, romfs, 'EventSetting'):
        events[row.get('__RowId')] = {'flow': os.path.basename(row.get('FlowchartFileName', '')).split('.')[0],
                                      'entry': row.get('EntryPointName'),
                                      'components': os.path.basename(row.get('EventSettingComponentList', '')).split('.')[0]}
    say('AI node types')
    nodes = []
    c = glob.glob(os.path.join(romfs, 'AI', 'NodeDefinition', 'Node.Product.*.aidefn.byml.zs'))
    if c:
        nodes = sorted(_v(byml.Byml(rf.read(os.path.relpath(sorted(c)[-1], romfs))).tree()).keys())
    cat = {'version': 1, 'build_id': build_id, 'built': time.strftime('%Y-%m-%d %H:%M'),
           'bancs': bancs, 'items': items, 'events': events, 'ai_nodes': nodes}
    if with_events:
        try:
            cat['event_api'] = event_api(rf, romfs, say)
        except ImportError:
            cat['event_api'] = None
    p = path(tool_dir, build_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    json.dump(cat, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, p)
    n_sp = sum(len(b['startpos']) for b in bancs.values())
    summary = ('catalog: %d maps, %d start positions, %d items, %d events, %d AI node types, event API %s (%.0fs)'
               % (len(bancs), n_sp, len(items), len(events), len(nodes),
                  ('%d flows' % cat['event_api']['flows']) if cat.get('event_api') else 'skipped (pip install evfl)',
                  time.time() - t0))
    return p, summary
