"""DevTool Core (Engine 2, F4): a generated RomFS mod with a command channel (v0.6: the "hello" command).

Parts (each its own switch, so a boot crash can be bisected):
  gamedata   GameData\\GameDataList.Product.*.byml.zs with the DevTool.* entries added (non-saved,
             SaveFileIndex -1, reset type 0) and a Struct "DevTool" listing them, so Sequence nodes can
             reach them through SeqQueryGetNonStructGameDataStruct("DevTool").
  sequence   Sequence\\DevTool.module.ainb (every frame: Cmd != 0 -> SeqOneShotRequestEvent "DevTool") and
             Sequence\\MainUpdate.module.ainb with that module added to the root Element_Simultaneous.
  event      Event\\EventFlow\\DevTool.bfevfl.zs (switch on Cmd: 1 = +1 rupee through
             PouchSystemActor::EventTriggerIncreaseRupee; then Ack = Cmd, Cmd = 0) and
             RSDB\\EventSetting with a "DevTool" row (component list: the existing NoPauseEventWithIgnoreDissentPlay).
The merged RESTBL is the mod manager's job (restbl.py); this module reports the sizes it needs.

Fallback (switch 'reuse'): if the game refuses new GameDataList entries, the same flows are generated on
top of existing non-saved Ints that nothing uses during normal play (struct Npc_TimeSignal: Hour, HourAMPM,
Minute, Second are only written when the time-signal NPC talks). Unconfirmed either way until the morning test.

Everything written is re-read with byml / ainb / evfl before the build counts as done.
"""
import copy
import io
import json
import os
import shutil
import struct
import time
import uuid

import byml
import gamedata
import restbl

MOD_NAME = 'DevTool Core'
EVENT_NAME = 'DevTool'
MODULE_NAME = 'DevTool.module'
PARTS = [('gamedata', 'GameDataList with the DevTool.* entries'),
         ('sequence', 'MainUpdate hook + DevTool.module (starts the event when Cmd != 0)'),
         ('event', 'DevTool event flow + EventSetting row')]
CMD_HELLO = 1
# v0.7 (Core step 2): every command is a case of the DevTool event's switch on Cmd; Arg0 picks the entry
# of the matching list in devtool_core.json 'lists' (built from the dump at build time, so tool and flow agree).
CMDS = {'hello': 1, 'warp': 10, 'item_scene': 11, 'item': 12, 'time': 13, 'weather': 14, 'hud': 15,
        'dungeon': 16, 'event': 17, 'bgm': 18, 'se': 19, 'message': 20, 'bgm_stop': 21,
        'setpos': 30}      # 30-39 are handled by the player bridge (player.py), not by the DevTool event
WEATHER = ['(reset to the map default)', 'GanonCloudy', 'Thunderstorm']    # names seen in the flows; others unknown
HUD_BITS = ['hearts', 'minimap', 'small key', 'boss key']                 # Arg0 bit set = hidden
DUNGEON_ITEMS = ['small key', 'boss key', 'map', 'compass']               # current dungeon (EventGetDungeon*)

# logical name -> (type, default). Defaults are the markers the plan asked for (harmless: the tool reads
# the values by hash); Cmd/Ack/Arg0 start at 0 because the Sequence hook fires on Cmd != 0.
ENTRIES = [('Cmd', 'Int', 0), ('Arg0', 'Int', 0), ('Arg1', 'Int', 0x44540001), ('Arg2', 'Int', 0x44540002),
           ('Arg3', 'Int', 0x44540003), ('Ack', 'Int', 0),
           ('F0', 'Float', 1.0), ('F1', 'Float', 1.0), ('F2', 'Float', 1.0), ('F3', 'Float', 1.0),
           ('Str', 'String64', ''), ('Pos', 'Vector3', (0.0, 0.0, 0.0)), ('PlayerPos', 'Vector3', (0.0, 0.0, 0.0)),
           ('DebugMove', 'Bool', False), ('God', 'Bool', False), ('MoonJump', 'Bool', False), ('HideHud', 'Bool', False)]
STRUCT = 'DevTool'
# fallback: reuse existing non-saved entries (all SaveFileIndex -1 in the dump)
REUSE = {'Cmd': 'Npc_TimeSignal.Hour', 'Arg0': 'Npc_TimeSignal.Minute', 'Ack': 'Npc_TimeSignal.Second',
         'Arg1': 'Npc_TimeSignal.HourAMPM'}
REUSE_STRUCT = 'Npc_TimeSignal'
COMPONENT_LIST = ('Work/Event/EventSetting/EventSettingComponentList/'
                  'NoPauseEventWithIgnoreDissentPlay.engine__event__EventSettingComponentList.gyml')
MAIN_UPDATE = 'Sequence/MainUpdate.module.ainb'


def names(reuse=False):
    """{logical: full GameData name} for the channel in use."""
    if reuse:
        return dict(REUSE)
    return {n: STRUCT + '.' + n for n, _t, _d in ENTRIES}


def _v(x):
    return x.v if hasattr(x, 'v') else x


def _guid():
    return str(uuid.uuid4())


def build_lists(rf, romfs, log=None):
    """The index lists the generated flow and the tool share, plus parameter/actor templates copied from
    the game's own flows (so every action is called the way the game calls it)."""
    import glob
    import evfl
    import gamedata as gd
    sp = byml.Byml(rf.read('Banc/BancBasedResource/StartPos.byml.zs')).tree()
    warps = []
    for banc in sorted(sp.keys()):
        for name in sorted(sp[banc].keys()):
            v = sp[banc][name]
            tr = v.get('Trans')
            warps.append([banc, name, [float(_v(x)) for x in tr] if tr else None])
    items = []
    c = sorted(glob.glob(os.path.join(romfs, 'RSDB', 'PouchActorInfo.Product.*.rstbl.byml.zs')))
    if c:
        for row in byml.Byml(rf.read(os.path.relpath(c[-1], romfs).replace(os.sep, '/'))).tree():
            items.append([_v(row['__RowId']), _v(row.get('PouchCategory', byml.Val(byml.T_STR, ''))),
                          _v(row.get('PouchTabCategory', byml.Val(byml.T_STR, '')))])
    items.sort()
    events = []
    c = sorted(glob.glob(os.path.join(romfs, 'RSDB', 'EventSetting.Product.*.rstbl.byml.zs')))
    for row in byml.Byml(rf.read(os.path.relpath(c[-1], romfs).replace(os.sep, '/'))).tree():
        flow = os.path.basename(_v(row.get('FlowchartFileName', byml.Val(byml.T_STR, '')))).split('.')[0]
        entry = _v(row.get('EntryPointName', byml.Val(byml.T_STR, '')))
        if flow and entry:     # a sub-flow call needs both; rows without an entry point (timelines, movies) are left out
            events.append([_v(row['__RowId']), flow, entry])
    events.sort()
    bgm, se, msgs = {}, {}, {}
    templates, actors = {}, {}
    files = sorted(glob.glob(os.path.join(romfs, 'Event', 'EventFlow', '*.bfevfl.zs')))
    for i, f in enumerate(files):
        if log and i % 200 == 0:
            log('[core] scanning flows for lists and templates: %d/%d' % (i, len(files)))
        try:
            ef = evfl.EventFlow()
            ef.read(rf.read(os.path.relpath(f, romfs).replace(os.sep, '/')))
        except Exception:
            continue
        fc = ef.flowchart
        if fc is None:
            continue
        for a in fc.actors:
            nm = a.identifier.name
            if nm in TEMPLATE_ACTORS and nm not in actors and a.params is not None:
                actors[nm] = dict(a.params.data)
        for ev in fc.events:
            d = ev.data
            if not isinstance(d, evfl.event.ActionEvent) or d.params is None:
                continue
            an = d.actor_action.v.v
            key = d.actor.v.identifier.name + '::' + an
            pd = d.params.data
            if key in TEMPLATE_ACTIONS and key not in templates and not any(isinstance(x, evfl.common.Argument) for x in pd.values()):
                templates[key] = dict(pd)
            if an == 'EventTriggerStartBgmSimple' and isinstance(pd.get('BGMName'), str) and pd['BGMName'].startswith('BGM'):
                bgm[pd['BGMName']] = 1
            if an == 'EventSoundStartSound' and isinstance(pd.get('SLinkKey'), str) and pd['SLinkKey']:
                se[pd['SLinkKey']] = 1
            if d.actor.v.identifier.name == 'SystemTextNPC' and an == 'EventTalk' and str(pd.get('MessageId', '')).startswith('EventFlowMsg/'):
                msgs[pd['MessageId']] = 1
    missing = [k for k in TEMPLATE_ACTIONS if k not in templates] + [a for a in TEMPLATE_ACTORS if a not in actors]
    if missing:
        raise ValueError('no template found in the game flows for: ' + ', '.join(missing))
    return {'warps': warps, 'items': items, 'events': events, 'bgm': sorted(bgm), 'se': sorted(se), 'messages': sorted(msgs),
            'weather': list(WEATHER), 'hours': list(range(24)), 'hud': list(HUD_BITS), 'dungeon': list(DUNGEON_ITEMS),
            'templates': templates, 'actors': actors}


TEMPLATE_ACTIONS = ['GameSystemActor::EventReserveBancChange', 'PouchSystemActor::EventTriggerAddOrRemovePouchContent',
                    'EventWorldManagerController::EventTrigger_SetTime', 'EventWorldManagerController::Event_SetWeatherForFlowchart',
                    'EventWorldManagerController::Event_ResetWeatherForFlowchart', 'UISystemActor::EventTriggerSetDisplayHeartGauge',
                    'UISystemActor::EventTriggerSetDisplayMiniMap', 'UISystemActor::EventTriggerSetDisplaySmallKey',
                    'UISystemActor::EventTriggerSetDisplayBossKey', 'GameSystemActor::EventGetDungeonSmallKey',
                    'GameSystemActor::EventGetDungeonBossKey', 'GameSystemActor::EventGetDungeonMap', 'GameSystemActor::EventGetDungeonCompass',
                    'SoundSystemActor::EventTriggerStartBgmSimple', 'SoundSystemActor::EventSoundStartSound',
                    'SoundSystemActor::EventTriggerStopInGameBgm', 'SystemTextNPC::EventTalk']
TEMPLATE_ACTORS = ['GameDataActor', 'PouchSystemActor', 'GameSystemActor', 'EventWorldManagerController', 'UISystemActor',
                   'SoundSystemActor', 'SystemTextNPC']


class Core:
    def __init__(self, romfs, load_dir, tid, log=print):
        self.romfs, self.load_dir, self.tid, self.log = romfs, load_dir, tid, log
        self.rf = gamedata.RomFS(romfs)
        self.mod = os.path.join(load_dir, tid, MOD_NAME)
        self.report = {'files': [], 'checks': [], 'restbl': {}}

    # ------------------------------------------------------------ helpers
    def _out(self, rel):
        p = os.path.join(self.mod, 'romfs', rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def _write(self, rel, data, restbl_key=None, old_len=None):
        p = self._out(rel)
        if rel.endswith('.zs'):
            raw = self.rf.compress(data, rel)
        else:
            raw = data
        open(p, 'wb').write(raw)
        self.report['files'].append(rel)
        key = restbl_key or (rel[:-3] if rel.endswith('.zs') else rel)
        self.report['restbl'][key] = (old_len, len(data))
        self.log('[core] wrote %s (%d bytes%s)' % (rel, len(data), ', was %d' % old_len if old_len else ''))

    def _check(self, what, ok, detail=''):
        self.report['checks'].append((what, bool(ok), detail))
        self.log('[core] %s %s %s' % ('OK  ' if ok else 'FAIL', what, detail))
        if not ok:
            raise ValueError('Core build check failed: %s %s' % (what, detail))

    # ------------------------------------------------------------ part: gamedata
    def build_gamedata(self):
        rel = os.path.relpath(gamedata.find_gamedatalist(self.romfs), self.romfs).replace(os.sep, '/')
        raw = self.rf.read(rel)
        tree = byml.Byml(raw).tree()
        data = tree['Data']
        H = gamedata.mmh
        members = []
        for name, typ, dv in ENTRIES:
            full = STRUCT + '.' + name
            if typ == 'Vector3':
                d = byml.BDict({'x': byml.Val(byml.T_FLOAT, dv[0]), 'y': byml.Val(byml.T_FLOAT, dv[1]),
                                'z': byml.Val(byml.T_FLOAT, dv[2])})
            elif typ == 'Float':
                d = byml.Val(byml.T_FLOAT, float(dv))
            elif typ == 'Bool':
                d = byml.Val(byml.T_BOOL, bool(dv))
            elif typ == 'String64':
                d = byml.Val(byml.T_STR, dv)
            else:
                d = byml.Val(byml.T_INT, int(dv))
            e = byml.BDict({'DefaultValue': d, 'Hash': byml.Val(byml.T_UINT, H(full)),
                            'ResetTypeValue': byml.Val(byml.T_INT, 0), 'SaveFileIndex': byml.Val(byml.T_INT, -1)})
            if any(_v(x['Hash']) == H(full) for x in data[typ]):
                raise ValueError('%s already exists in the GameDataList' % full)
            data[typ].append(e)
            members.append(byml.BDict({'Hash': byml.Val(byml.T_UINT, H(name)), 'Value': byml.Val(byml.T_UINT, H(full))}))
        data['Struct'].append(byml.BDict({'DefaultValue': byml.BArr(members), 'Hash': byml.Val(byml.T_UINT, H(STRUCT)),
                                          'ResetTypeValue': byml.Val(byml.T_INT, 0), 'SaveFileIndex': byml.Val(byml.T_INT, -1)}))
        out = byml.write(tree, version=byml.Byml(raw).version)
        back = byml.Byml(out).tree()
        self._check('GameDataList re-read', back == tree, '%d -> %d bytes' % (len(raw), len(out)))
        self._check('DevTool.Cmd present', any(_v(x['Hash']) == H('DevTool.Cmd') for x in back['Data']['Int']))
        self._write(rel, out, old_len=len(raw))
        return rel

    # ------------------------------------------------------------ part: sequence
    ACK_REQUESTED = 100     # the Sequence hook writes this to Ack when it requests the event; the event overwrites it

    def _module_json(self, reuse):
        """DevTool.module, shaped like the game's own NaviHintEvent.module (v0.8.2):

            S32Selector on <struct>.Ack:  100 -> End (already requested, the event is running)
                                          else -> S32Selector on <struct>.Cmd:
                                                      0, 30..39 -> End (nothing / player-bridge commands)
                                                      else -> Simultaneous[RequestEvent "DevTool", Ack = 100]

        Every branch ends in a one-shot, so the module re-runs every frame. The first version parked the 0 case
        on SeqExecuteGenericDummyAction, which never ends, so the selector never looked at Cmd again (Nov 7:
        Cmd stayed 1 for 8 s with no reaction in the flag log)."""
        st = REUSE_STRUCT if reuse else STRUCT
        member = lambda k: REUSE[k].split('.')[1] if reuse else k
        prop_bool = lambda n, v: {'Name': n, 'Default Value': v, 'Flags': []}
        uses = lambda: ['Uses Default']

        def selector(idx, query_idx, children):
            return {'Node Type': 'Element_S32Selector', 'Node Index': idx, 'Name': '', 'GUID': _guid(),
                    'Flags': ['Is Root Node'] if idx == 0 else [], 'Queries': [query_idx], 'Attachments': [],
                    'Properties': {'Int': [{'Name': 'CalculateTiming', 'Default Value': 0, 'Flags': []},
                                           {'Name': 'InputValue', 'Default Value': 0, 'Flags': []}],
                                   'Bool': [prop_bool('ChildFrameSync', False), prop_bool('IsNoSelectIfSameInstance', False),
                                            prop_bool('IsNoSelectWhenChildBusy', False)]},
                    'Parameters': {'Inputs': {'Int': [{'Name': 'Input', 'Default Value': 0, 'Node Index': query_idx,
                                                       'Output Index': 0, 'Flags': []}]}, 'Outputs': {}},
                    'XLink Actions': [],
                    'Plugs': {'Child': children,
                              'Int': [{'Node Index': query_idx, 'Name': 'Input', 'Unknown': 0, 'Default Value': 0}]}}

        def get_int(idx, name):
            return {'Node Type': 'UserDefined', 'Node Index': idx, 'Name': 'SeqQueryGetGameDataInt', 'GUID': _guid(),
                    'Flags': ['Is Query'], 'Queries': [1], 'Attachments': [],
                    'Properties': {'Bool': [prop_bool('OutputOnlyOnEnter', False)]},
                    'Parameters': {'Inputs': {'Int': [{'Name': 'GameDataIndex', 'Default Value': -1, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}],
                                              'Bool': [{'Name': 'IsDirect', 'Default Value': False, 'Node Index': -1, 'Output Index': 0, 'Flags': ['Uses Default', 'Is Output']}],
                                              'String': [{'Name': 'GameDataName', 'Default Value': name, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}],
                                              'Pointer': [{'Name': 'ParentStruct', 'Classname': 'const void', 'Default Value': None, 'Node Index': 1, 'Output Index': 0, 'Flags': []}]},
                                   'Outputs': {'Int': [{'Name': 'Value', 'Is Output': True}]}},
                    'XLink Actions': [], 'Plugs': {}}

        def end(idx):
            return {'Node Type': 'UserDefined', 'Node Index': idx, 'Name': 'SeqOneShotEnd', 'GUID': _guid(),
                    'Flags': [], 'Queries': [], 'Attachments': [],
                    'Properties': {'Bool': [prop_bool('ExecuteEveryFrame', False)]},
                    'Parameters': {'Inputs': {}, 'Outputs': {}}, 'XLink Actions': [], 'Plugs': {}}

        nodes = [
            selector(0, 2, [{'Node Index': 5, 'Name': '', 'Condition': self.ACK_REQUESTED},
                            {'Node Index': 4, 'Name': '', 'Is Default': True}]),
            {'Node Type': 'UserDefined', 'Node Index': 1, 'Name': 'SeqQueryGetNonStructGameDataStruct', 'GUID': _guid(),
             'Flags': ['Is Query'], 'Queries': [], 'Attachments': [],
             'Properties': {'Bool': [prop_bool('OutputOnlyOnEnter', False)]},
             'Parameters': {'Inputs': {'Int': [{'Name': 'GameDataIndex', 'Default Value': -1, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}],
                                       'Bool': [{'Name': 'IsDirect', 'Default Value': False, 'Node Index': -1, 'Output Index': 0, 'Flags': ['Uses Default', 'Is Output']}],
                                       'String': [{'Name': 'GameDataFullName', 'Default Value': st, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}]},
                            'Outputs': {'Pointer': [{'Name': 'Value', 'Classname': 'const void', 'Is Output': True}]}},
             'XLink Actions': [], 'Plugs': {'Generic': [{'Node Index': 2, 'Name': 'Value'}]}},
            get_int(2, member('Ack')),
            get_int(3, member('Cmd')),
            selector(4, 3, [{'Node Index': 5, 'Name': '', 'Condition': 0}] +
                           [{'Node Index': 5, 'Name': '', 'Condition': k} for k in range(30, 40)] +      # player bridge commands (v0.8)
                           [{'Node Index': 6, 'Name': '', 'Is Default': True}]),
            end(5),
            {'Node Type': 'Element_Simultaneous', 'Node Index': 6, 'Name': '', 'GUID': _guid(), 'Flags': [], 'Queries': [],
             'Attachments': [],
             'Properties': {'Int': [{'Name': 'EndPolicy', 'Default Value': 0, 'Flags': []}, {'Name': 'ResultPolicy', 'Default Value': 0, 'Flags': []}]},
             'Parameters': {'Inputs': {}, 'Outputs': {}}, 'XLink Actions': [],
             'Plugs': {'Child': [{'Node Index': 7, 'Name': ''}, {'Node Index': 8, 'Name': ''}]}},
            {'Node Type': 'UserDefined', 'Node Index': 7, 'Name': 'SeqOneShotRequestEvent', 'GUID': _guid(),
             'Flags': [], 'Queries': [], 'Attachments': [],
             'Properties': {'Bool': [prop_bool('ExecuteEveryFrame', False)]},
             'Parameters': {'Inputs': {'Bool': [{'Name': 'IsBancParamValue', 'Default Value': False, 'Node Index': -1, 'Output Index': 0, 'Flags': ['Uses Default', 'Is Output']}],
                                       'String': [{'Name': 'EventName', 'Default Value': EVENT_NAME, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}]},
                            'Outputs': {}},
             'XLink Actions': [], 'Plugs': {}},
            {'Node Type': 'UserDefined', 'Node Index': 8, 'Name': 'SeqOneShotSetGameDataInt', 'GUID': _guid(),
             'Flags': [], 'Queries': [1], 'Attachments': [],
             'Properties': {'Bool': [prop_bool('ExecuteEveryFrame', False)]},
             'Parameters': {'Inputs': {'Int': [{'Name': 'GameDataIndex', 'Default Value': -1, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()},
                                               {'Name': 'Value', 'Default Value': self.ACK_REQUESTED, 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}],
                                       'String': [{'Name': 'GameDataName', 'Default Value': member('Ack'), 'Node Index': -1, 'Output Index': 0, 'Flags': uses()}],
                                       'Pointer': [{'Name': 'ParentStruct', 'Classname': 'const void', 'Default Value': None, 'Node Index': 1, 'Output Index': 0, 'Flags': []}]},
                            'Outputs': {}},
             'XLink Actions': [], 'Plugs': {}},
        ]
        return {'Version': 1032, 'Filename': MODULE_NAME, 'Category': 'Sequence',
                'Blackboard ID': int(uuid.uuid4().int & 0x7FFFFFFF) | 0x10000000, 'Parent Blackboard ID': 0,
                'Commands': [{'Name': 'Start', 'GUID': _guid(), 'Root Node Index': 0}],
                'Nodes': nodes, 'Blackboard': {}, 'Expressions': {}, 'Replacement Table': [], 'Modules': [],
                'Unknown Section 0x58': {}, 'Has Section 0x6C': True}

    @staticmethod
    def _ainb_bytes(d):
        import ainb
        ainb.set_tears_of_the_kingdom()
        a = ainb.AINB.from_dict(d, override_filename=d['Filename'])
        if a.blackboard is None:
            a.blackboard = ainb.Blackboard()      # ainb 0.2.16 writes a broken file for a None blackboard
        return a.to_binary()

    @staticmethod
    def _ainb_dict(data):
        import ainb
        ainb.set_tears_of_the_kingdom()
        return json.loads(ainb.AINB.from_binary(data).to_json())

    def build_sequence(self, reuse=False):
        import ainb  # noqa: F401  (fails early and clearly if the package is missing)
        mod = self._module_json(reuse)
        b = self._ainb_bytes(mod)
        back = self._ainb_dict(b)
        self._check('DevTool.module re-read', [n['Name'] for n in back['Nodes']] == [n['Name'] for n in mod['Nodes']],
                    '%d nodes' % len(back['Nodes']))
        self._write('Sequence/%s.ainb' % MODULE_NAME, b)
        # hook: MainUpdate root (Element_Simultaneous) gets one more child, a module node
        raw = self.rf.read(MAIN_UPDATE)
        d = self._ainb_dict(raw)
        root_idx = d['Commands'][0]['Root Node Index']
        root = d['Nodes'][root_idx]
        self._check('MainUpdate root is Element_Simultaneous', root['Node Type'] == 'Element_Simultaneous', root['Node Type'])
        if any(n['Name'] == MODULE_NAME for n in d['Nodes']):
            raise ValueError('MainUpdate already hooked')
        idx = len(d['Nodes'])
        d['Nodes'].append({'Node Type': 'UserDefined', 'Node Index': idx, 'Name': MODULE_NAME, 'GUID': _guid(),
                           'Flags': ['Is Module'], 'Queries': [], 'Attachments': [], 'Properties': {},
                           'Parameters': {'Inputs': {}, 'Outputs': {}}, 'XLink Actions': [], 'Plugs': {}})
        root['Plugs'].setdefault('Child', []).append({'Node Index': idx, 'Name': ''})
        d['Modules'].append({'Path': MODULE_NAME + '.ainb', 'Category': 'Sequence', 'Instance Count': 1})
        b2 = self._ainb_bytes(d)
        back = self._ainb_dict(b2)
        self._check('MainUpdate re-read', any(n['Name'] == MODULE_NAME for n in back['Nodes']) and
                    len(back['Nodes'][root_idx]['Plugs']['Child']) == len(root['Plugs']['Child']),
                    '%d nodes, %d root children' % (len(back['Nodes']), len(root['Plugs']['Child'])))
        self._write(MAIN_UPDATE, b2, old_len=len(raw))
        return mod

    # ------------------------------------------------------------ part: event
    def build_event(self, reuse=False, lists=None, event_player=False, hello_only=False):
        import evfl
        from evfl.util import make_index, make_rindex
        nm = names(reuse)
        L = lists or build_lists(self.rf, self.romfs, self.log)
        T, A = L['templates'], L['actors']
        src = self.rf.read('Event/EventFlow/NaviHint.bfevfl.zs')     # template header/version
        ef = evfl.EventFlow()
        ef.read(src)
        fc = evfl.Flowchart()
        fc.name = EVENT_NAME
        actors = {}

        def actor(name):
            if name in actors:
                return actors[name]
            a = evfl.Actor()
            a.identifier = evfl.common.ActorIdentifier(name, '')
            a.params = evfl.Container()
            a.params.data = dict(A.get(name) or A['GameDataActor'])
            a.concurrent_clips = 1
            fc.actors.append(a)
            actors[name] = a
            return a

        def use(name, action=None, query=None):
            a = actor(name)
            if action and not any(x.v == action for x in a.actions):
                a.actions.append(evfl.common.StringHolder(action))
            if query and not any(x.v == query for x in a.queries):
                a.queries.append(evfl.common.StringHolder(query))
            return a
        n_ev = [0]

        def act(name, actor_name, action, params, nxt=None):
            a = use(actor_name, action=action)
            ev = evfl.Event()
            ev.name = name
            ev.data = evfl.event.ActionEvent()
            ev.data.actor = make_rindex(a)
            ev.data.actor_action = make_rindex(a.find_action(action))
            ev.data.params = evfl.Container()
            ev.data.params.data = dict(params)
            ev.data.nxt = make_index(nxt)
            fc.events.append(ev)
            n_ev[0] += 1
            return ev

        def tmpl(key, **over):
            d = dict(T[key])
            d.update(over)
            return d

        def switch(name, gd_name, cases, default=None):
            a = use('GameDataActor', query='EventQueryGameDataIntBranch')
            sw = evfl.Event()
            sw.name = name
            sw.data = evfl.event.SwitchEvent()
            sw.data.actor = make_rindex(a)
            sw.data.actor_query = make_rindex(a.find_query('EventQueryGameDataIntBranch'))
            sw.data.params = evfl.Container()
            sw.data.params.data = {'GameDataName': gd_name}
            sw.data.cases = {int(k): make_rindex(v) for k, v in cases.items()}
            fc.events.append(sw)
            return sw

        def subflow(name, flow, entry, nxt):
            ev = evfl.Event()
            ev.name = name
            ev.data = evfl.event.SubFlowEvent()
            ev.data.res_flowchart_name = flow
            ev.data.entry_point_name = entry
            ev.data.params = None
            ev.data.nxt = make_index(nxt)
            fc.events.append(ev)
            return ev
        # shared tail: Ack = 1, Cmd = 0
        ev_clear = act('ClearCmd', 'GameDataActor', 'EventTriggerSetGameDataInt', {'GameDataName': nm['Cmd'], 'Index': -1, 'Value': 0})
        ev_ack = act('Ack', 'GameDataActor', 'EventTriggerSetGameDataInt', {'GameDataName': nm['Ack'], 'Index': -1, 'Value': 1}, ev_clear)
        ev_bad = act('BadArg', 'GameSystemActor', 'EventTriggerSendErrorToEventResource', {'Title': 'DevTool: Arg0 out of range'}, ev_clear)
        top = {0: ev_clear, CMDS['hello']: act('Hello_Rupee', 'PouchSystemActor', 'EventTriggerIncreaseRupee', {'Value': 1}, ev_ack)}

        def arg_switch(cmd, name, builder, n):
            """Cmd case = a switch on Arg0 with n cases built by builder(i) -> first event of the chain."""
            if hello_only:      # v0.8.3 bisect switch: a tiny flow with only the hello case (and the error case)
                return
            cases = {i: builder(i) for i in range(n)}
            top[CMDS[cmd]] = switch('Arg0_' + name, nm['Arg0'], cases)
        W = L['warps']
        arg_switch('warp', 'warp', lambda i: act('Warp%d' % i, 'GameSystemActor', 'EventReserveBancChange',
                                                  tmpl('GameSystemActor::EventReserveBancChange', NextBanc=W[i][0], NextStartPos=W[i][1],
                                                       NextPosition=[0.0, 0.0, 0.0], NextStartRot=[0.0, 0.0, 0.0], BootEventName='',
                                                       EventStarterName='', IsItemWarp=False, IsStartOnEvent=False, IsWaitEnd=True,
                                                       FadePattern=1), ev_ack), len(W))
        I = L['items']
        for cmd, scene in (('item_scene', True), ('item', False)):
            arg_switch(cmd, cmd, lambda i, scene=scene: act('%s%d' % (cmd, i), 'PouchSystemActor', 'EventTriggerAddOrRemovePouchContent',
                                                            tmpl('PouchSystemActor::EventTriggerAddOrRemovePouchContent', Actor=I[i][0], Num=1,
                                                                 IsGetEvent=scene, IsEquipDIrect=False, IsSetMessageTargetActorName=True), ev_ack), len(I))
        arg_switch('time', 'time', lambda i: act('Time%d' % i, 'EventWorldManagerController', 'EventTrigger_SetTime',
                                                  tmpl('EventWorldManagerController::EventTrigger_SetTime', TimeHour=i, TimeMin=0), ev_ack), 24)

        def weather(i):
            if i == 0:
                return act('WeatherReset', 'EventWorldManagerController', 'Event_ResetWeatherForFlowchart',
                           tmpl('EventWorldManagerController::Event_ResetWeatherForFlowchart', IsImmTrans=True, ChangeSpeed=2), ev_ack)
            return act('Weather%d' % i, 'EventWorldManagerController', 'Event_SetWeatherForFlowchart',
                       tmpl('EventWorldManagerController::Event_SetWeatherForFlowchart', Weather=L['weather'][i], IsImmTrans=True, ChangeSpeed=2), ev_ack)
        arg_switch('weather', 'weather', weather, len(L['weather']))

        def hud(i):      # bit set = hidden; four chained actions
            nxt = ev_ack
            for bit, (action, key) in reversed(list(enumerate([('EventTriggerSetDisplayHeartGauge', 'IsDisplayHeartGauge'),
                                                               ('EventTriggerSetDisplayMiniMap', 'IsDisplayMiniMap'),
                                                               ('EventTriggerSetDisplaySmallKey', 'IsDisplaySmallKey'),
                                                               ('EventTriggerSetDisplayBossKey', 'IsDisplayBossKey')]))):
                nxt = act('Hud%d_%d' % (i, bit), 'UISystemActor', action, {key: not (i >> bit) & 1}, nxt)
            return nxt
        arg_switch('hud', 'hud', hud, 16)
        dung = ['EventGetDungeonSmallKey', 'EventGetDungeonBossKey', 'EventGetDungeonMap', 'EventGetDungeonCompass']
        arg_switch('dungeon', 'dungeon', lambda i: act('Dungeon%d' % i, 'GameSystemActor', dung[i], {'IsWaitEnd': True}, ev_ack), 4)
        E = L['events'] if event_player else []
        if E:        # 2,000+ sub-flow cases: the event system may load every referenced flowchart, so it is its own switch
            arg_switch('event', 'event', lambda i: subflow('Ev%d' % i, E[i][1], E[i][2], ev_ack), len(E))
        arg_switch('bgm', 'bgm', lambda i: act('Bgm%d' % i, 'SoundSystemActor', 'EventTriggerStartBgmSimple',
                                                tmpl('SoundSystemActor::EventTriggerStartBgmSimple', BGMName=L['bgm'][i], IsReleaseDucking=False, CommandLife=2), ev_ack), len(L['bgm']))
        arg_switch('se', 'se', lambda i: act('Se%d' % i, 'SoundSystemActor', 'EventSoundStartSound',
                                              tmpl('SoundSystemActor::EventSoundStartSound', SLinkKey=L['se'][i], IsIgnoreDucking=False), ev_ack), len(L['se']))
        arg_switch('message', 'message', lambda i: act('Msg%d' % i, 'SystemTextNPC', 'EventTalk',
                                                        tmpl('SystemTextNPC::EventTalk', MessageId=L['messages'][i], IsWaitEnd=True), ev_ack), len(L['messages']))
        if not hello_only:
            top[CMDS['bgm_stop']] = act('BgmStop', 'SoundSystemActor', 'EventTriggerStopInGameBgm', tmpl('SoundSystemActor::EventTriggerStopInGameBgm'), ev_ack)
        for k in range(2, 32):
            top.setdefault(k, ev_bad)
        sw = switch('SwitchCmd', nm['Cmd'], top)
        ep = evfl.EntryPoint('Run')
        ep.main_event = make_index(sw)
        fc.entry_points.append(ep)
        ef.flowchart = fc
        ef.name = EVENT_NAME
        out = io.BytesIO()
        ef.write(out)
        b = out.getvalue()
        ef2 = evfl.EventFlow()
        ef2.read(b)
        f2 = ef2.flowchart
        self._check('DevTool.bfevfl re-read', f2.name == EVENT_NAME and len(f2.events) == len(fc.events) and f2.entry_points[0].name == 'Run',
                    '%d events, %d actors, %d bytes' % (len(f2.events), len(f2.actors), len(b)))
        sw2 = [e for e in f2.events if isinstance(e.data, evfl.event.SwitchEvent) and e.name == 'SwitchCmd'][0]
        if hello_only:
            self._check('hello only: switch Cmd 1 -> rupee, nothing else', sw2.data.cases[CMDS['hello']].v.name == 'Hello_Rupee'
                        and len(f2.events) < 50)
        else:
            self._check('switch Cmd 1 -> rupee, %d -> warp list' % CMDS['warp'],
                        sw2.data.cases[CMDS['hello']].v.name == 'Hello_Rupee' and sw2.data.cases[CMDS['warp']].v.name == 'Arg0_warp')
            w0 = [e for e in f2.events if e.name == 'Warp0'][0]
            self._check('warp 0 = %s / %s' % (W[0][0], W[0][1]), w0.data.params.data['NextBanc'] == W[0][0])
        self._write('Event/EventFlow/%s.bfevfl.zs' % EVENT_NAME, b)
        # EventSetting row
        import glob
        rs = sorted(glob.glob(os.path.join(self.romfs, 'RSDB', 'EventSetting.Product.*.rstbl.byml.zs')))[-1]
        rel = os.path.relpath(rs, self.romfs).replace(os.sep, '/')
        raw = self.rf.read(rel)
        rows = byml.Byml(raw).tree()
        if any(_v(r['__RowId']) == EVENT_NAME for r in rows):
            raise ValueError('EventSetting already has a DevTool row')
        tmplrow = next(r for r in rows if _v(r['__RowId']) == 'NaviLockOnHint')
        row = copy.deepcopy(tmplrow)
        row['__RowId'] = byml.Val(byml.T_STR, EVENT_NAME)
        row['EntryPointName'] = byml.Val(byml.T_STR, 'Run')
        row['FlowchartFileName'] = byml.Val(byml.T_STR, 'Work/Event/EventFlow/%s.evfc' % EVENT_NAME)
        row['EventSettingComponentList'] = byml.Val(byml.T_STR, COMPONENT_LIST)
        if 'EventEffectSetting' in row:
            del row['EventEffectSetting']
        # v0.8.3: the vanilla table is sorted by __RowId in byte order (all 2,379 rows; found after the
        # first in-game hello: Ack 100 but no event with the row appended at the end). The game most likely
        # binary-searches it, so the row goes into its sorted place; any table that is not sorted keeps
        # the row appended (and the check below says so).
        keys = [_v(r['__RowId']).encode() for r in rows]
        was_sorted = all(a <= b for a, b in zip(keys, keys[1:]))
        import bisect
        if was_sorted:
            pos = bisect.bisect_left(keys, EVENT_NAME.encode())
        else:
            pos = len(rows)
        rows.insert(pos, row)
        out = byml.write(rows, version=byml.Byml(raw).version)
        back = byml.Byml(out).tree()
        self._check('EventSetting re-read', back == rows and any(_v(r['__RowId']) == EVENT_NAME for r in back),
                    '%d rows' % len(back))
        bk = [_v(r['__RowId']).encode() for r in back]
        self._check('EventSetting row order', (not was_sorted) or all(a <= b for a, b in zip(bk, bk[1:])),
                    'DevTool at %d of %d, %s' % (pos, len(back), 'table sorted' if was_sorted else 'vanilla table was not sorted: appended'))
        self.report['eventsetting_pos'] = pos
        # v0.8.3: GameAnalyzedEventInfo. Every vanilla EventSetting row with a flowchart has a row here too
        # (2,379 of 2,380; the one without is PlayOcarina_ReplaceEvent, which has no flowchart), and the table is
        # sorted the same way. v0.8.2 wrote none for DevTool and the event never started (Ack 100, nothing else);
        # a missing row being the reason is a guess until the next hello. NaviLockOnHint's row is all defaults
        # (no recorded camera, no terrain centre, no sub-flows), so it is copied as is.
        ga = sorted(glob.glob(os.path.join(self.romfs, 'RSDB', 'GameAnalyzedEventInfo.Product.*.rstbl.byml.zs')))
        if ga:
            grel = os.path.relpath(ga[-1], self.romfs).replace(os.sep, '/')
            graw = self.rf.read(grel)
            grows = byml.Byml(graw).tree()
            if not any(_v(r['__RowId']) == EVENT_NAME for r in grows):
                gt = next(r for r in grows if _v(r['__RowId']) == 'NaviLockOnHint')
                grow = copy.deepcopy(gt)
                grow['__RowId'] = byml.Val(byml.T_STR, EVENT_NAME)
                gk = [_v(r['__RowId']).encode() for r in grows]
                gsorted = all(x <= y for x, y in zip(gk, gk[1:]))
                gpos = bisect.bisect_left(gk, EVENT_NAME.encode()) if gsorted else len(grows)
                grows.insert(gpos, grow)
                gout = byml.write(grows, version=byml.Byml(graw).version)
                gback = byml.Byml(gout).tree()
                gb = [_v(r['__RowId']).encode() for r in gback]
                self._check('GameAnalyzedEventInfo row', gback == grows and (not gsorted or all(x <= y for x, y in zip(gb, gb[1:]))),
                            'DevTool at %d of %d%s' % (gpos, len(gback), '' if gsorted else ' (vanilla table not sorted: appended)'))
                self._write(grel, gout, old_len=len(graw))
                self.report['analyzed_pos'] = gpos
        else:
            self.log('[core] note: no RSDB/GameAnalyzedEventInfo in this dump; skipped')
        self._write(rel, out, old_len=len(raw))
        self.report['lists'] = {k: v for k, v in L.items() if k not in ('templates', 'actors')}
        self.report['cmds'] = dict(CMDS)

    # ------------------------------------------------------------ build / remove
    def build(self, parts=('gamedata', 'sequence', 'event'), reuse=False, event_player=False, hello_only=False):
        self.report = {'files': [], 'checks': [], 'restbl': {}, 'parts': list(parts), 'reuse': reuse,
                       'event_player': event_player and not hello_only, 'hello_only': hello_only,
                       'built': time.strftime('%Y-%m-%d %H:%M')}
        self.remove()
        if 'gamedata' in parts and not reuse:
            self.build_gamedata()
        if 'sequence' in parts:
            self.build_sequence(reuse)
        L = build_lists(self.rf, self.romfs, self.log)
        if 'event' in parts:
            self.build_event(reuse, L, event_player, hello_only)
        else:
            self.report['lists'] = {k: v for k, v in L.items() if k not in ('templates', 'actors')}
            self.report['cmds'] = dict(CMDS)
        os.makedirs(self.mod, exist_ok=True)
        self.report['names'] = names(reuse)
        json.dump(self.report, open(os.path.join(self.mod, 'devtool_core.json'), 'w'), indent=1)
        return self.report

    def remove(self):
        if os.path.isdir(self.mod):
            shutil.rmtree(self.mod)
            return True
        return False

    def status(self):
        p = os.path.join(self.mod, 'devtool_core.json')
        try:
            return json.load(open(p))
        except Exception:
            return None

    def gamedatalist_path(self):
        """The mod's GameDataList if the gamedata part is built (the live tool must load its definitions
        from it, because the record counts in RAM then differ from the vanilla list)."""
        st = self.status()
        if not st or 'gamedata' not in st.get('parts', []) or st.get('reuse'):
            return None
        for f in st['files']:
            if f.startswith('GameData/'):
                p = os.path.join(self.mod, 'romfs', f.replace('/', os.sep))
                return p if os.path.isfile(p) else None
        return None
