"""Player bridge and player parameter mod (v0.8): the "DevTool Player" RomFS mod.

The bridge edits AI\\Player.scheduler.module.ainb inside Pack\\Actor\\Player_Base / _Child / _Adult.pack.zs
(the three copies are byte-identical in the dump). The scheduler's Root command has a main root
(SelectorPlayerSchedulerReaction, node 11) and a secondary root (SelectorPlayerSchedulerDebug, node 6,
children DebugMove -> ExecuteBSASetActionVerb 62, Normal -> 11). Each part is its own switch:

  pos        every frame: QueryActorGetPosition -> OneShotSetNonStructGameDataVector3("DevTool.PlayerPos")
  setpos     DevTool.Cmd == 30: ExecuteActorForceSetPos(DevTool.Pos), then Cmd = 0, Ack = 1
             (the Sequence hook ignores Cmd 30-39, so the DevTool event is not started for these)
  moonjump   DevTool.MoonJump: OneShotPhysicsSetLinearVelocity((0, 12, 0)) every frame while True
  gravity    OneShotPhysicsCctSetGravityScale(DevTool.F0) every frame
  god        OneShotLifeSetInvincible(DevTool.God) every frame
  debugmove  secondary root becomes a BoolSelector on DevTool.DebugMove: True -> the DebugMove verb,
             False -> the original debug selector

The bridge nodes hang under a new Element_Simultaneous that becomes the main root with the original
root as its last child. Node templates were copied from the game's own AI files (see docs\\research).
Parameters (same-size in-place float patches of Player\\PlayerGameParam\\Player.game__player__PlayerGameParam.bgyml):
  GroundSpeedScale  x speed multiplier (C4, first version)
  LargeLandHeight   -> 100000 when "no fall damage" is on (the landing-height threshold; unconfirmed)
All of it is unconfirmed until seen in-game; a bad graph edit can crash at boot, so bisect with the switches.
"""
import json
import os
import shutil
import struct
import time
import uuid

import byml
import gamedata
import restbl

MOD_NAME = 'DevTool Player'
PACKS = ['Pack/Actor/Player_Base.pack.zs', 'Pack/Actor/Player_Child.pack.zs', 'Pack/Actor/Player_Adult.pack.zs']
SCHED = 'AI/Player.scheduler.module.ainb'
GAMEPARAM = 'Player/PlayerGameParam/Player.game__player__PlayerGameParam.bgyml'
PARTS = [('pos', 'PlayerPos copy every frame'),
         ('setpos', 'teleport on Cmd 30 (ForceSetPos <- DevTool.Pos)'),
         ('moonjump', 'moon jump while DevTool.MoonJump'),
         ('gravity', 'gravity scale = DevTool.F0'),
         ('god', 'invincible while DevTool.God'),
         ('debugmove', 'DevTool.DebugMove picks the DebugMove branch')]
CMD_SETPOS = 30
MOON_UP = 12.0
NO_FALL_HEIGHT = 100000.0


def _guid():
    return str(uuid.uuid4())


def _in(name, typ, default, node=-1, out=0, flags=None):
    d = {'Name': name, 'Default Value': default, 'Node Index': node, 'Output Index': out,
         'Flags': list(flags) if flags is not None else (['Uses Default'] if node < 0 else [])}
    return typ, d


def _node(idx, name, kind='UserDefined', flags=(), queries=(), props=None, inputs=(), outputs=None, plugs=None):
    ins = {}
    for typ, d in inputs:
        ins.setdefault(typ, []).append(d)
    return {'Node Type': kind, 'Node Index': idx, 'Name': name, 'GUID': _guid(), 'Flags': list(flags),
            'Queries': list(queries), 'Attachments': [], 'Properties': props or {},
            'Parameters': {'Inputs': ins, 'Outputs': outputs or {}}, 'XLink Actions': [], 'Plugs': plugs or {}}


def bridge(sched, parts):
    """sched: the scheduler as the ainb package's dict. Returns (new dict, [added nodes]) ."""
    d = json.loads(json.dumps(sched))
    nodes = d['Nodes']
    cmd = d['Commands'][0]
    main_root, dbg_root = cmd['Root Node Index'], cmd.get('Secondary Root Node Index', -1)
    added = []

    def add(n):
        n['Node Index'] = len(nodes)
        nodes.append(n)
        added.append(n)
        return n['Node Index']
    every = {'Bool': [{'Name': 'ExecuteEveryFrame', 'Default Value': True, 'Flags': []}]}
    q_props = {'Bool': [{'Name': 'DispError', 'Default Value': False, 'Flags': []},
                        {'Name': 'OutputOnlyOnEnter', 'Default Value': False, 'Flags': []}]}

    def gd_query(kind, full, outtyp):
        return add(_node(0, 'QueryGetNonStructGameData' + kind, flags=['Is Query'], props=q_props,
                         inputs=[_in('GameDataIndex', 'Int', -1, flags=['Uses Default', 'Is Output']),
                                 _in('IsDirect', 'Bool', False, flags=['Uses Default', 'Is Output']),
                                 _in('GameDataFullName', 'String', full)],
                         outputs={outtyp: [{'Name': 'Value', 'Is Output': True}]}))
    children = []
    if 'pos' in parts:
        q = add(_node(0, 'QueryActorGetPosition', flags=['Is Query'],
                      props={'Bool': [{'Name': 'OutputOnlyOnEnter', 'Default Value': False, 'Flags': []}]},
                      outputs={'Vector3F': [{'Name': 'Position', 'Is Output': True}]}))
        s = add(_node(0, 'OneShotSetNonStructGameDataVector3', queries=[q], props=every,
                      inputs=[_in('GameDataIndex', 'Int', -1, flags=['Uses Default', 'Is Output']),
                              _in('GameDataFullName', 'String', 'DevTool.PlayerPos'),
                              _in('Value', 'Vector3F', [0.0, 0.0, 0.0], node=q, out=0)]))
        nodes[q]['Plugs'] = {'Generic': [{'Node Index': s, 'Name': 'Position'}]}
        children.append(s)
    if 'setpos' in parts:
        qc = gd_query('Int', 'DevTool.Cmd', 'Int')
        qs = add(_node(0, 'QueryGetNonStructGameDataStruct', flags=['Is Query'], props=q_props,
                       inputs=[_in('GameDataIndex', 'Int', -1, flags=['Uses Default', 'Is Output']),
                               _in('IsDirect', 'Bool', False, flags=['Uses Default', 'Is Output']),
                               _in('GameDataFullName', 'String', 'DevTool')],
                       outputs={'Pointer': [{'Name': 'Value', 'Classname': 'const void', 'Is Output': True}]}))
        qp = add(_node(0, 'QueryGetGameDataVector3', flags=['Is Query'], queries=[qs], props=q_props,
                       inputs=[_in('GameDataIndex', 'Int', -1, flags=['Uses Default', 'Is Output']),
                               _in('GameDataName', 'String', 'Pos'),
                               ('Pointer', {'Name': 'ParentStruct', 'Classname': 'const void', 'Default Value': None,
                                            'Node Index': qs, 'Output Index': 0, 'Flags': []})],
                       outputs={'Vector3F': [{'Name': 'Value', 'Is Output': True}]}))
        nodes[qs]['Plugs'] = {'Generic': [{'Node Index': qp, 'Name': 'Value'}]}
        fs = add(_node(0, 'ExecuteActorForceSetPos', queries=[qp],
                       inputs=[_in('Destination', 'Vector3F', [0.0, 0.0, 0.0], node=qp, out=0)]))
        nodes[qp]['Plugs'] = {'Generic': [{'Node Index': fs, 'Name': 'Value'}]}
        clr = add(_node(0, 'OneShotSetNonStructGameDataInt', props={'Bool': [{'Name': 'ExecuteEveryFrame', 'Default Value': False, 'Flags': []}]},
                        inputs=[_in('GameDataIndex', 'Int', -1, flags=['Uses Default', 'Is Output']), _in('Value', 'Int', 0),
                                _in('GameDataFullName', 'String', 'DevTool.Cmd')]))
        ack = add(_node(0, 'OneShotSetNonStructGameDataInt', props={'Bool': [{'Name': 'ExecuteEveryFrame', 'Default Value': False, 'Flags': []}]},
                        inputs=[_in('GameDataIndex', 'Int', -1, flags=['Uses Default', 'Is Output']), _in('Value', 'Int', 1),
                                _in('GameDataFullName', 'String', 'DevTool.Ack')]))
        sim = add(_node(0, '', kind='Element_Simultaneous',
                        props={'Int': [{'Name': 'EndPolicy', 'Default Value': 0, 'Flags': []}, {'Name': 'ResultPolicy', 'Default Value': 0, 'Flags': []}]},
                        plugs={'Child': [{'Node Index': fs, 'Name': ''}, {'Node Index': ack, 'Name': ''}, {'Node Index': clr, 'Name': ''}]}))
        dummy = add(_node(0, 'ExecuteGenericDummyAction'))
        sel = add(_node(0, '', kind='Element_S32Selector', queries=[qc],
                        props={'Int': [{'Name': 'CalculateTiming', 'Default Value': 0, 'Flags': []}, {'Name': 'InputValue', 'Default Value': 0, 'Flags': []}],
                               'Bool': [{'Name': 'ChildFrameSync', 'Default Value': False, 'Flags': []},
                                        {'Name': 'IsNoSelectIfSameInstance', 'Default Value': False, 'Flags': []},
                                        {'Name': 'IsNoSelectWhenChildBusy', 'Default Value': False, 'Flags': []}]},
                        inputs=[_in('Input', 'Int', 0, node=qc, out=0)],
                        plugs={'Child': [{'Node Index': sim, 'Name': '', 'Condition': CMD_SETPOS}, {'Node Index': dummy, 'Name': '', 'Is Default': True}],
                               'Int': [{'Node Index': qc, 'Name': 'Input', 'Unknown': 0, 'Default Value': 0}]}))
        nodes[qc]['Plugs'] = {'Generic': [{'Node Index': sel, 'Name': 'Value'}]}
        children.append(sel)
    if 'moonjump' in parts:
        qb = gd_query('Bool', 'DevTool.MoonJump', 'Bool')
        up = add(_node(0, 'OneShotPhysicsSetLinearVelocity', props=every,
                       inputs=[_in('Velocity', 'Vector3F', [0.0, MOON_UP, 0.0])]))
        dummy = add(_node(0, 'ExecuteGenericDummyAction'))
        bs = add(_node(0, '', kind='Element_BoolSelector', queries=[qb],
                       props={'Int': [{'Name': 'CalculateTiming', 'Default Value': 0, 'Flags': []}],
                              'Bool': [{'Name': 'ChildFrameSync', 'Default Value': False, 'Flags': []},
                                       {'Name': 'InputValue', 'Default Value': False, 'Flags': []},
                                       {'Name': 'IsNoSelectWhenChildBusy', 'Default Value': False, 'Flags': []}]},
                       inputs=[_in('Input', 'Bool', False, node=qb, out=0)],
                       plugs={'Generic': [{'Node Index': qb, 'Name': 'Input', 'Unknown 1': 0, 'Unknown 2': 0}],
                              'Child': [{'Node Index': up, 'Name': 'True'}, {'Node Index': dummy, 'Name': 'False'}]}))
        nodes[qb]['Plugs'] = {'Generic': [{'Node Index': bs, 'Name': 'Value'}]}
        children.append(bs)
    if 'gravity' in parts:
        qf = gd_query('Float', 'DevTool.F0', 'Float')
        g = add(_node(0, 'OneShotPhysicsCctSetGravityScale', queries=[qf], props=every,
                      inputs=[_in('GravityScale', 'Float', 1.0, node=qf, out=0)]))
        nodes[qf]['Plugs'] = {'Generic': [{'Node Index': g, 'Name': 'Value'}]}
        children.append(g)
    if 'god' in parts:
        qg = gd_query('Bool', 'DevTool.God', 'Bool')
        inv = add(_node(0, 'OneShotLifeSetInvincible', queries=[qg],
                        props={'Bool': [{'Name': 'ExecuteEveryFrame', 'Default Value': True, 'Flags': []},
                                        {'Name': 'IsNoReaction', 'Default Value': False, 'Flags': []}],
                               'String': [{'Name': 'InvincibleFlagName', 'Default Value': 'LifeParam', 'Flags': []}]},
                        inputs=[_in('Bool', 'Bool', False, node=qg, out=0)]))
        nodes[qg]['Plugs'] = {'Generic': [{'Node Index': inv, 'Name': 'Value'}]}
        children.append(inv)
    if children:
        root = add(_node(0, '', kind='Element_Simultaneous', flags=['Is Root Node'],
                         props={'Int': [{'Name': 'EndPolicy', 'Default Value': 0, 'Flags': []}, {'Name': 'ResultPolicy', 'Default Value': 0, 'Flags': []}]},
                         plugs={'Child': [{'Node Index': c, 'Name': ''} for c in children] + [{'Node Index': main_root, 'Name': ''}]}))
        nodes[main_root]['Flags'] = [f for f in nodes[main_root]['Flags'] if f != 'Is Root Node']
        cmd['Root Node Index'] = root
    if 'debugmove' in parts and dbg_root >= 0:
        old = nodes[dbg_root]
        dm = next(c['Node Index'] for c in old['Plugs']['Child'] if c['Name'] == 'DebugMove')
        qd = gd_query('Bool', 'DevTool.DebugMove', 'Bool')
        bs = add(_node(0, '', kind='Element_BoolSelector', flags=['Is Root Node'], queries=[qd],
                       props={'Int': [{'Name': 'CalculateTiming', 'Default Value': 0, 'Flags': []}],
                              'Bool': [{'Name': 'ChildFrameSync', 'Default Value': False, 'Flags': []},
                                       {'Name': 'InputValue', 'Default Value': False, 'Flags': []},
                                       {'Name': 'IsNoSelectWhenChildBusy', 'Default Value': False, 'Flags': []}]},
                       inputs=[_in('Input', 'Bool', False, node=qd, out=0)],
                       plugs={'Generic': [{'Node Index': qd, 'Name': 'Input', 'Unknown 1': 0, 'Unknown 2': 0}],
                              'Child': [{'Node Index': dm, 'Name': 'True'}, {'Node Index': dbg_root, 'Name': 'False'}]}))
        nodes[qd]['Plugs'] = {'Generic': [{'Node Index': bs, 'Name': 'Value'}]}
        old['Flags'] = [f for f in old['Flags'] if f != 'Is Root Node']
        cmd['Secondary Root Node Index'] = bs
    return d, added


# ---------------------------------------------------------------- ainb helpers (shared with core.py's way of writing)
def ainb_dict(data):
    import ainb
    import ainb.utils as U
    if not getattr(U.ParseWarning, '_devtool_patched', False):   # 0.2.16 raises on duplicate pool strings
        def _init(self, reader, msg=None):
            U.WarningBase.__init__(self, str(msg) if msg is not None else str(reader))
        U.ParseWarning.__init__ = _init
        U.ParseWarning._devtool_patched = True
    import warnings
    ainb.set_tears_of_the_kingdom()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return json.loads(ainb.AINB.from_binary(data).to_json())


def ainb_bytes(d):
    import ainb
    ainb.set_tears_of_the_kingdom()
    a = ainb.AINB.from_dict(d, override_filename=d['Filename'])
    if a.blackboard is None:
        a.blackboard = ainb.Blackboard()
    return a.to_binary()


# ---------------------------------------------------------------- in-place bgyml float patch
def patch_floats(orig, changes):
    """changes: {'/Key/Path': new float}. Same-size patch; returns (bytes, [(path, old, new)])."""
    y = byml.Byml(orig)
    flat = {p: (t, v, o) for p, t, v, o in y.flat()}
    data = bytearray(orig)
    done = []
    for p, nv in changes.items():
        key = p.lstrip('/')
        hit = [k for k in flat if k.lstrip('/') == key]
        if not hit:
            raise KeyError('no key %s in the file' % p)
        t, v, o = flat[hit[0]]
        if t != byml.T_FLOAT:
            raise TypeError('%s is not a float' % p)
        struct.pack_into(y.e + 'f', data, o, float(nv))
        done.append((p, v, float(nv)))
    out = bytes(data)
    assert len(out) == len(orig)
    byml.Byml(out).tree()
    return out, done


# ---------------------------------------------------------------- the mod
class Player:
    def __init__(self, romfs, load_dir, tid, log=print):
        self.romfs, self.load_dir, self.tid, self.log = romfs, load_dir, tid, log
        self.rf = gamedata.RomFS(romfs)
        self.mod = os.path.join(load_dir, tid, MOD_NAME)
        self.report = {}

    def remove(self):
        if os.path.isdir(self.mod):
            shutil.rmtree(self.mod)
            return True
        return False

    def status(self):
        try:
            return json.load(open(os.path.join(self.mod, 'devtool_player.json')))
        except Exception:
            return None

    def build(self, parts=('pos', 'setpos', 'moonjump', 'gravity', 'god', 'debugmove'), speed=1.0, no_fall=False, research_dir=None):
        import gamedata as gd
        from sarc import sarc_read, sarc_write
        self.remove()
        rep = {'parts': list(parts), 'speed': speed, 'no_fall': no_fall, 'files': [], 'restbl': {}, 'checks': [],
               'built': time.strftime('%Y-%m-%d %H:%M'), 'cmd_setpos': CMD_SETPOS, 'moon_up': MOON_UP}
        sched_new = added = None
        for rel in PACKS:
            raw = self.rf.read(rel)
            files = sarc_read(raw)
            d = dict(files)
            changed = {}
            if parts and SCHED in d:
                if sched_new is None:
                    sd = ainb_dict(d[SCHED])
                    nd, added = bridge(sd, parts)
                    sched_new = ainb_bytes(nd)
                    back = ainb_dict(sched_new)
                    ok = len(back['Nodes']) == len(nd['Nodes']) and back['Commands'][0]['Root Node Index'] == nd['Commands'][0]['Root Node Index']
                    rep['checks'].append(('scheduler re-read', ok, '%d nodes (+%d), root %d, secondary %d' % (
                        len(back['Nodes']), len(added), back['Commands'][0]['Root Node Index'], back['Commands'][0].get('Secondary Root Node Index', -1))))
                    if not ok:
                        raise ValueError('bridged scheduler does not read back the same')
                    self.log('[player] scheduler: %d nodes added for %s' % (len(added), '+'.join(parts)))
                changed[SCHED] = sched_new
            pchanges = {}
            if speed != 1.0:
                pchanges['/GroundSpeedScale'] = float(speed)
            if no_fall:
                pchanges['/LargeLandHeight'] = NO_FALL_HEIGHT
            if pchanges and GAMEPARAM in d:
                nb, done = patch_floats(d[GAMEPARAM], pchanges)
                changed[GAMEPARAM] = nb
                rep['checks'].append(('PlayerGameParam patch in %s' % os.path.basename(rel), True, ', '.join('%s %g -> %g' % x for x in done)))
            if not changed:
                continue
            new_files = [(n, changed.get(n, b)) for n, b in files]
            sarc = sarc_write(new_files)
            back = dict(sarc_read(sarc))
            ok = all(back[n] == b for n, b in changed.items()) and len(back) == len(files)
            rep['checks'].append(('%s re-read' % os.path.basename(rel), ok, '%d files, %d -> %d bytes' % (len(back), len(raw), len(sarc))))
            if not ok:
                raise ValueError('pack re-read failed: ' + rel)
            out = os.path.join(self.mod, 'romfs', rel.replace('/', os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            open(out, 'wb').write(self.rf.compress(sarc, rel))
            rep['files'].append(rel)
            rep['restbl'][rel[:-3]] = (len(raw), len(sarc))
            self.log('[player] wrote %s (%d -> %d bytes)' % (rel, len(raw), len(sarc)))
        os.makedirs(self.mod, exist_ok=True)
        rep['added_nodes'] = added or []
        json.dump(rep, open(os.path.join(self.mod, 'devtool_player.json'), 'w'), indent=1)
        if research_dir and added:
            os.makedirs(research_dir, exist_ok=True)
            json.dump({'file': SCHED, 'packs': PACKS, 'parts': list(parts), 'built': rep['built'], 'added_nodes': added,
                       'note': 'nodes appended to Player.scheduler.module.ainb by player.bridge(); the vanilla root 11 becomes '
                               'the last child of the new Element_Simultaneous root; the secondary (debug) root is wrapped '
                               'in a BoolSelector on DevTool.DebugMove'},
                      open(os.path.join(research_dir, 'player_bridge_nodes.json'), 'w'), indent=1)
        self.report = rep
        return rep
