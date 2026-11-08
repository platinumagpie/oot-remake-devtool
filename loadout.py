"""Loadout tab (v0.8.1): one screen that mirrors Link's inventory, equipment, upgrades and quest status,
with Give / Take per row, size pickers for the upgrades and editable presets (data/loadouts.json).

Also home of the v0.8.1 IsGet fix (found in-game Nov 7): the pouch lists an item only when IsGet.<item> is
True, so a PouchData.<item>.StockNum write > 0 alone leaves the item invisible (bombs = 30 with no
IsGet.Item_Bomb stayed hidden). isget_fix() wraps the live GameData and the loaded save file so that
every StockNum write > 0 from the tool also sets IsGet.<item> True; a count of 0 leaves IsGet alone.

What is known and what is a guess (shown in the UI too):
  confirmed in-game  IsGet + StockNum make Farore's Wind and the magic meter work; magic meter 240 / 480.
  from saves         The current ammo / rupee caps live in an unnamed GameData struct #F80F8077 whose
                     members are Rupee, DekuSeed, Bomb, Arrow, DekuStick, DekuNut (the PouchActorInfo
                     MaxNumGameData keys). In every save checked (7) the values equal AfterExpansionMaxNum
                     of the bag owned (wallet 200, bullet bag 30, stick bag 10, nut bag 20, 0 = no bag).
                     Whether the game re-reads it live or only at load: unconfirmed.
  from saves         Ocarina / sword / shield equip strings (PlayerStatus.Equip*: 'Item_Ocarina_Fairy',
                     'Weapon_Sword_Kokiri', ...), bottle slots (BottleInfo.BottleA-E: IsValid + Enum BottleItem).
  guesses            Smaller bag sizes owned alongside bigger ones, DiveDepth 3/6/9 for scales,
                     LiftGrade for gauntlets, IsDefenseUP for double defense, bombchus sharing the bomb
                     cap, the Kokiri tunic being Armor_Adult_Normal, equip slots taking effect at once.
"""
import json
import os
import re
import tkinter as tk
from tkinter import ttk

import gamedata

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS = os.path.join(HERE, 'data', 'loadouts.json')
# Set to True once the DevTool Core hello and give-item commands are confirmed in-game: it unlocks the
# "with the 'you got' scene" option. Until then every give uses the raw route.
CORE_CONFIRMED = False

MAX_STRUCT = 0xF80F8077          # unnamed: found by its member hashes (v0.8.1), see the module docstring
MAX_KEYS = ('Rupee', 'DekuSeed', 'Bomb', 'Arrow', 'DekuStick', 'DekuNut')
H = gamedata.mmh


def isget_name(stock_name):
    m = re.match(r'^PouchData\.(.+)\.StockNum$', stock_name or '')
    return 'IsGet.%s' % m.group(1) if m else None


# ---------------------------------------------------------------------------------------- the IsGet fix
def isget_pairs(by_hash, names=None, extra=()):
    """{StockNum hash: (IsGet hash, item name)} for every PouchData.<item>.StockNum that has an IsGet.<item>."""
    cands = set(extra)
    if names is not None:
        for n in list(names.map.values()) + list(names.user.values()):
            m = re.match(r'^PouchData\.(.+)\.StockNum$', n)
            if m:
                cands.add(m.group(1))
    out = {}
    for item in cands:
        sh, ih = H('PouchData.%s.StockNum' % item), H('IsGet.%s' % item)
        if sh in by_hash and ih in by_hash:
            out[sh] = (ih, item)
    return out


def isget_fix(app, obj):
    """Wrap obj (gdlive.LiveGameData: write(h, v); savefile.SaveFile: set(e, v)) so a StockNum write > 0 also
    sets IsGet.<item> True. Idempotent; returns obj."""
    if obj is None or getattr(obj, '_isget_fixed', False):
        return obj
    items = [i.get('name') for i in ((app._load_catalog() or {}).get('items') or []) if i.get('name')]
    items += [a for r in all_rows() for a in r.actors()]
    pairs = isget_pairs(obj.by_hash, getattr(app, 'names', None), items)

    def after(h, value):
        p = pairs.get(h)
        if not p:
            return
        try:
            if int(value) <= 0:
                return
        except (TypeError, ValueError):
            return
        ih, item = p
        ie = obj.by_hash[ih]
        cur = obj.vals.get(ih) if hasattr(obj, 'vals') else obj.get(ie)
        if cur:
            return
        if hasattr(obj, 'write'):
            got = orig(ih, True)
            try:
                app.flog_tool(ie, cur, got, 'set by the tool: count > 0 needs IsGet')
            except Exception:
                pass
        else:
            orig(ie, True)
            try:
                app.edited.add(ih)
            except Exception:
                pass
        app.log('IsGet.%s set True along with its count (the pouch lists items by IsGet; v0.8.1 fix)' % item)

    if hasattr(obj, 'write') and hasattr(obj, 'vals'):       # live
        orig = obj.write

        def write(h, value, also_copy=False):
            got = orig(h, value, also_copy)
            after(h, value)
            return got
        obj.write = write
    else:                                                    # save file
        orig = obj.set

        def set_(e, value, index=None):
            r = orig(e, value) if index is None else orig(e, value, index)
            if index is None:
                after(e.hash, value)
            return r
        obj.set = set_
    obj._isget_fixed = True
    obj._isget_pairs = pairs
    return obj


# ---------------------------------------------------------------------------------------- rows
class Row:
    """kind: flag (IsGet [+ StockNum 1]), count (IsGet + StockNum n, capped), tier (one of several levels),
    bottle (BottleInfo slot), hearts (MaxLife), magic (meter size)."""

    def __init__(self, rid, label, kind, items=(), cap=None, fixed=None, bag=None, levels=None, slot=None,
                 extra=None, note='', default=None):
        self.id, self.label, self.kind = rid, label, kind
        self.items = list(items)          # flag/count: actor names
        self.cap, self.fixed, self.bag = cap, fixed, bag
        self.levels = levels or []        # tier: [(level label, [actors], {name: value} extra writes)]
        self.slot = slot                  # PlayerStatus.Equip* for an Equip button
        self.extra = extra or {}          # flag: extra writes on Give ({name: (give value, take value)})
        self.note = note
        self.default = default

    def actors(self):
        out = list(self.items)
        for _, acts, _ in self.levels:
            out += acts
        return out


def F(rid, label, actor, **kw):
    return Row(rid, label, 'flag', [actor], **kw)


def CNT(rid, label, actor, **kw):
    return Row(rid, label, 'count', [actor], **kw)


def BAG(rid, label, prefix, key, fills=(), fills_if_owned=(), sizes=('Small', 'Medium', 'Large'), none=True, fallback=()):
    lv = [('none', [], {})] if none else []
    owned = []
    for s, cap in zip(sizes, fallback):     # smaller sizes stay owned (guess: nothing in the game clears IsGet)
        owned = owned + ['%s_%s' % (prefix, s)]
        lv.append((s, list(owned), {'cap': cap}))
    r = Row(rid, label, 'tier', levels=lv, cap=key)
    r.fills, r.fills_if_owned, r.bag_prefix = list(fills), list(fills_if_owned), prefix
    return r


SECTIONS = [
    ('Items', [
        F('slingshot', 'Fairy Slingshot', 'Weapon_Range_SlingShot'),
        CNT('seeds', 'Deku Seeds', 'Item_DekuSeed', cap='DekuSeed', bag='bullet_bag'),
        CNT('bombs', 'Bombs', 'Item_Bomb', cap='Bomb', bag='bomb_bag'),
        CNT('bombchus', 'Bombchus', 'Item_Bombchu', cap='Bomb', bag='bomb_bag',
            note='cap: PouchActorInfo says MaxNumGameData Bomb, so chus share the bomb bag cap (unconfirmed)'),
        F('boomerang', 'Boomerang', 'Item_Boomerang'),
        Row('hookshot', 'Hookshot / Longshot', 'tier',
            levels=[('none', [], {}), ('Hookshot', ['Item_Hookshot'], {}), ('Longshot', ['Item_Hookshot', 'Item_Longshot'], {})]),
        F('bow', 'Fairy Bow', 'Weapon_Range_Bow'),
        CNT('arrows', 'Arrows', 'Item_Arrow_Normal', cap='Arrow', bag='quiver'),
        F('arrow_fire', 'Fire Arrows', 'Item_Arrow_Fire'),
        F('arrow_ice', 'Ice Arrows', 'Item_Arrow_Ice'),
        F('arrow_light', 'Light Arrows', 'Item_Arrow_Light'),
        F('din', "Din's Fire", 'Item_DinFire'),
        F('farore', "Farore's Wind", 'Item_FaroreWind'),
        F('nayru', "Nayru's Love", 'Item_NayruLove'),
        F('lens', 'Lens of Truth', 'Armor_GlassTruth'),
        CNT('beans', 'Magic Beans', 'Item_MagicBean', fixed=10),
        F('hammer', 'Megaton Hammer', 'Item_Hammer'),
        CNT('sticks', 'Deku Sticks', 'Item_DekuStick', cap='DekuStick', bag='stick_bag'),
        CNT('nuts', 'Deku Nuts', 'Item_DekuNut', cap='DekuNut', bag='nut_bag'),
        Row('ocarina', 'Ocarina', 'tier', slot='PlayerStatus.EquipOcarina',
            levels=[('none', [], {}), ('Fairy', ['Item_Ocarina_Fairy'], {}), ('Time', ['Item_Ocarina_Fairy', 'Item_Ocarina_Time'], {})]),
        Row('bottle_a', 'Bottle 1', 'bottle', items=['BottleA']),
        Row('bottle_b', 'Bottle 2', 'bottle', items=['BottleB']),
        Row('bottle_c', 'Bottle 3', 'bottle', items=['BottleC']),
        Row('bottle_d', 'Bottle 4', 'bottle', items=['BottleD']),
    ]),
    ('Equipment', [
        F('sword_kokiri', 'Kokiri Sword', 'Weapon_Sword_Kokiri', slot='PlayerStatus.EquipWeapon'),
        F('sword_master', 'Master Sword', 'Weapon_Sword_Master', slot='PlayerStatus.EquipWeapon'),
        F('sword_giant', "Giant's Knife (guess: Weapon_Sword_Giant)", 'Weapon_Sword_Giant', slot='PlayerStatus.EquipWeapon'),
        F('sword_broken', "Broken Giant's Knife", 'Weapon_Sword_BrokenGiant', slot='PlayerStatus.EquipWeapon'),
        F('sword_biggoron', "Biggoron's Sword", 'Weapon_Sword_Biggoron', slot='PlayerStatus.EquipWeapon'),
        F('shield_deku', 'Deku Shield', 'Weapon_Shield_Deku', slot='PlayerStatus.EquipShield'),
        F('shield_hylian', 'Hylian Shield', 'Weapon_Shield_Hylian', slot='PlayerStatus.EquipShield'),
        F('shield_mirror', 'Mirror Shield', 'Weapon_Shield_Mirror', slot='PlayerStatus.EquipShield'),
        F('tunic_kokiri', 'Kokiri Tunic (guess: Armor_Adult_Normal)', 'Armor_Adult_Normal', slot='PlayerStatus.EquipArmor'),
        F('tunic_goron', 'Goron Tunic', 'Armor_Adult_Goron', slot='PlayerStatus.EquipArmor'),
        F('tunic_zora', 'Zora Tunic', 'Armor_Adult_Zora', slot='PlayerStatus.EquipArmor'),
        F('boots_kokiri', 'Kokiri Boots (Item_NormalBoot)', 'Item_NormalBoot', slot='PlayerStatus.EquipBoot'),
        F('boots_iron', 'Iron Boots', 'Armor_Adult_HeavyBoot', slot='PlayerStatus.EquipBoot'),
        F('boots_hover', 'Hover Boots', 'Armor_Adult_HoverBoot', slot='PlayerStatus.EquipBoot'),
    ]),
    ('Upgrades', [
        BAG('bomb_bag', 'Bomb Bag', 'Item_BombBag', 'Bomb', fills=['Item_Bomb'], fills_if_owned=['Item_Bombchu'], fallback=(20, 30, 40)),
        BAG('quiver', 'Quiver', 'Item_Quiver', 'Arrow', fills=['Item_Arrow_Normal'], fallback=(30, 40, 50)),
        BAG('bullet_bag', 'Bullet Bag', 'Item_BulletBag', 'DekuSeed', fills=['Item_DekuSeed'], fallback=(30, 40, 50)),
        BAG('stick_bag', 'Deku Stick capacity', 'Item_StickBag', 'DekuStick', fills=['Item_DekuStick'], fallback=(10, 20, 30)),
        BAG('nut_bag', 'Deku Nut capacity', 'Item_NutBag', 'DekuNut', fills=['Item_DekuNut'], fallback=(20, 30, 40)),
        BAG('wallet', 'Wallet', 'Item_Wallet', 'Rupee', none=False, fallback=(200, 500, 1000)),
        Row('scale', 'Scale', 'tier', note='DiveDepth 3 / 6 / 9: from PouchActorInfo and a save without a scale (unconfirmed)',
            levels=[('none', [], {'PlayerStatus.DiveDepth': 3.0}), ('Silver', ['Item_Scale_Silver'], {'PlayerStatus.DiveDepth': 6.0}),
                    ('Gold', ['Item_Scale_Silver', 'Item_Scale_Gold'], {'PlayerStatus.DiveDepth': 9.0})]),
        Row('gauntlets', 'Strength', 'tier', note='LiftGrade from PouchActorInfo LiftableGrade (unconfirmed)',
            levels=[('none', [], {'PlayerStatus.LiftGrade': 'None'}),
                    ("Goron's Bracelet", ['Armor_Child_Gauntlet_Goron'], {'PlayerStatus.LiftGrade': 'Low'}),
                    ('Silver Gauntlets', ['Armor_Child_Gauntlet_Goron', 'Armor_Adult_Gauntlet_Silver'], {'PlayerStatus.LiftGrade': 'Middle'}),
                    ('Golden Gauntlets', ['Armor_Child_Gauntlet_Goron', 'Armor_Adult_Gauntlet_Silver', 'Armor_Adult_Gauntlet_Gold'],
                     {'PlayerStatus.LiftGrade': 'High'})]),
        Row('magic', 'Magic meter', 'magic', note='240 single / 480 double: confirmed in-game Nov 7',
            levels=[('none', [], {}), ('Single', ['Item_FairyBless_MP'], {}), ('Double', ['Item_FairyBless_MP', 'Item_FairyBless_MPDouble'], {})]),
        F('defense', 'Double Defense', 'Item_FairyBless_DefenseDouble', extra={'PlayerStatus.IsDefenseUP': (True, False)},
          note='also sets PlayerStatus.IsDefenseUP (unconfirmed)'),
    ]),
    ('Quest', [
        F('stone_forest', "Kokiri's Emerald", 'Item_Stone_Forest'),
        F('stone_fire', "Goron's Ruby", 'Item_Stone_Fire'),
        F('stone_water', "Zora's Sapphire", 'Item_Stone_Water'),
        F('medal_forest', 'Forest Medallion', 'Item_Medal_Forest'),
        F('medal_fire', 'Fire Medallion', 'Item_Medal_Fire'),
        F('medal_water', 'Water Medallion', 'Item_Medal_Water'),
        F('medal_spirit', 'Spirit Medallion', 'Item_Medal_Spirit'),
        F('medal_shadow', 'Shadow Medallion', 'Item_Medal_Shadow'),
        F('medal_light', 'Light Medallion', 'Item_Medal_Light'),
    ] + [F('song_' + s.lower(), label, 'Item_Melody_' + s) for s, label in (
        ('ZeldaLullaby', "Zelda's Lullaby"), ('EponaSong', "Epona's Song"), ('SariaSong', "Saria's Song"), ('SunSong', "Sun's Song"),
        ('TimeSong', 'Song of Time'), ('StormSong', 'Song of Storms'), ('ForestMinuet', 'Minuet of Forest'),
        ('FireBolero', 'Bolero of Fire'), ('WaterSerenade', 'Serenade of Water'), ('SpiritRequiem', 'Requiem of Spirit'),
        ('ShadowNocturne', 'Nocturne of Shadow'), ('LightPrelude', 'Prelude of Light'), ('ScarecrowSong', "Scarecrow's Song"))] + [
        CNT('tokens', 'Gold Skulltula Tokens', 'Item_Token_Gold', fixed=100),
        F('gerudo_card', "Gerudo's Card", 'Item_Token_Gerudo'),
        F('agony', 'Stone of Agony', 'Item_AgonyStone'),
        CNT('heart_pieces', 'Heart Pieces', 'Obj_Heart_Piece', default=1,
            note='whether 4 pieces turn into a container by themselves: unconfirmed'),
        Row('hearts', 'Heart containers (hearts)', 'hearts', note='MaxLife = 4 per heart (live); hearts refill at the next load'),
    ] + [F('mask_' + m.lower(), label, 'Armor_Child_Mask_' + m) for m, label in (
        ('Keaton', 'Keaton Mask'), ('Skull', 'Skull Mask'), ('Spooky', 'Spooky Mask'), ('Bunny', 'Bunny Hood'),
        ('Truth', 'Mask of Truth'), ('Goron', 'Goron Mask'), ('Zora', 'Zora Mask'), ('Gerudo', 'Gerudo Mask'),
        ('Amiibo0', 'amiibo mask 0 (unconfirmed)'), ('Amiibo1', 'amiibo mask 1 (unconfirmed)'))] + [
        F('trade_' + a.lower(), label, 'Item_' + a) for a, label in (
            ('WeirdEgg', 'Weird Egg (child trade)'), ('AlarmCucco', 'Cucco (child trade)'), ('ZeldaLetter', "Zelda's Letter (child trade)"),
            ('PocketEgg', 'Pocket Egg'), ('PocketCucco', 'Pocket Cucco'), ('CojiroCucco', 'Cojiro'), ('OddMushroom', 'Odd Mushroom'),
            ('OddPowder', 'Odd Potion'), ('PoacherSaw', "Poacher's Saw"), ('BrokenSword', "Broken Goron's Sword"),
            ('Prescription', 'Prescription'), ('EyeballFrog', 'Eyeball Frog'), ('EyeDrop', "World's Finest Eye Drops"),
            ('ClaimCheck', 'Claim Check'))]),
]


def all_rows():
    return [r for _, rows in SECTIONS for r in rows]


ROWS = {r.id: r for r in all_rows()}

DEFAULT_PRESETS = {
    '_doc': ('Loadout presets for the Dev Tool (v0.8.1). Each preset: "mode" replace (Strip first) or add, and "set": '
             '{row id: value}. Values: flags true/false, counts a number, upgrades the level name ("none", "Small", '
             '"Medium", "Large", "Hookshot", "Longshot", "Fairy", "Time", "Silver", "Gold", "Single", "Double", '
             '"Goron\'s Bracelet", "Silver Gauntlets", "Golden Gauntlets"), bottles the content (Empty, RedPotion, '
             'GreenPotion, BluePotion, BlueFire, Fish, Letter, BigPoe, Poe, Bug, Fairy, MaxMilk, HalfMilk) or null, hearts a '
             'number of hearts. Row ids: see loadout.py SECTIONS. Edit freely; the tab re-reads this file on Reload presets.'),
    'presets': {
        'Child start': {'mode': 'replace', 'set': {
            'sword_kokiri': True, 'shield_deku': True, 'boots_kokiri': True, 'tunic_kokiri': True, 'slingshot': True,
            'bullet_bag': 'Small', 'seeds': 30, 'stick_bag': 'Small', 'sticks': 10, 'nut_bag': 'Small', 'nuts': 20,
            'ocarina': 'Fairy', 'wallet': 'Small', 'hearts': 3}},
        'All child items': {'mode': 'add', 'set': {
            'sword_kokiri': True, 'shield_deku': True, 'shield_hylian': True, 'boots_kokiri': True, 'tunic_kokiri': True,
            'slingshot': True, 'bullet_bag': 'Large', 'seeds': 50, 'bomb_bag': 'Large', 'bombs': 40, 'bombchus': 40,
            'boomerang': True, 'stick_bag': 'Large', 'sticks': 30, 'nut_bag': 'Large', 'nuts': 40, 'din': True,
            'farore': True, 'nayru': True, 'lens': True, 'beans': 10, 'ocarina': 'Time', 'wallet': 'Large',
            'scale': 'Gold', 'gauntlets': "Goron's Bracelet", 'magic': 'Double',
            'bottle_a': 'Empty', 'bottle_b': 'Empty', 'bottle_c': 'Empty', 'bottle_d': 'Empty',
            'mask_keaton': True, 'mask_skull': True, 'mask_spooky': True, 'mask_bunny': True, 'mask_truth': True,
            'mask_goron': True, 'mask_zora': True, 'mask_gerudo': True}},
        'All adult items': {'mode': 'add', 'set': {
            'sword_master': True, 'sword_biggoron': True, 'shield_hylian': True, 'shield_mirror': True,
            'tunic_kokiri': True, 'tunic_goron': True, 'tunic_zora': True, 'boots_kokiri': True, 'boots_iron': True,
            'boots_hover': True, 'bow': True, 'quiver': 'Large', 'arrows': 50, 'arrow_fire': True, 'arrow_ice': True,
            'arrow_light': True, 'hookshot': 'Longshot', 'hammer': True, 'bomb_bag': 'Large', 'bombs': 40, 'bombchus': 40,
            'din': True, 'farore': True, 'nayru': True, 'lens': True, 'beans': 10, 'ocarina': 'Time', 'wallet': 'Large',
            'scale': 'Gold', 'gauntlets': 'Golden Gauntlets', 'magic': 'Double', 'defense': True,
            'bottle_a': 'Empty', 'bottle_b': 'Empty', 'bottle_c': 'Empty', 'bottle_d': 'Empty'}},
        'Everything': {'mode': 'add', 'set': 'ALL'},
        'Strip': {'mode': 'replace', 'set': {}},
    },
}


def load_presets():
    """data/loadouts.json; written with the defaults when missing (and refused, not overwritten, when broken)."""
    if not os.path.exists(PRESETS):
        try:
            os.makedirs(os.path.dirname(PRESETS), exist_ok=True)
            json.dump(DEFAULT_PRESETS, open(PRESETS, 'w', encoding='utf-8'), indent=1)
        except Exception:
            pass
        return DEFAULT_PRESETS['presets'], None
    try:
        p = json.load(open(PRESETS, encoding='utf-8'))
        return p.get('presets') or {}, None
    except Exception as e:
        return DEFAULT_PRESETS['presets'], 'data\\loadouts.json is not valid JSON (%s): using the built-in presets' % e


def core_confirmed():
    """True once the Core hello / give-item commands are confirmed in-game (CORE_CONFIRMED above)."""
    return bool(CORE_CONFIRMED)


# ---------------------------------------------------------------------------------------- the tab
class LoadoutTab:
    def __init__(self, app, nb):
        self.app = app
        self.pouch = {}          # PouchActorInfo rows from the dump (filled in the background)
        self.ui = {}             # row id -> widgets
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='Loadout')
        self.frame = f
        ttk.Label(f, justify='left', wraplength=1300, text=(
            "v0.8.1. Link's pouch in one screen: Give / Take per row, sizes for the upgrades. Raw route = IsGet.<item> + "
            "PouchData.<item>.StockNum + the slot values (works live, or on the save loaded on World state when not "
            "connected: then Save changes there). Ammo caps come from GameData struct #F80F8077 (Rupee / DekuSeed / Bomb / "
            "Arrow / DekuStick / DekuNut), found in your saves, set when a bag is given: unconfirmed live. Labels with "
            "'guess' or 'unconfirmed' have not been seen in-game yet.")).pack(anchor='w')
        bar = ttk.Frame(f)
        bar.pack(fill='x', pady=4)
        ttk.Button(bar, text='Refresh', command=self.refresh).pack(side='left')
        ttk.Label(bar, text='   Preset').pack(side='left')
        self.preset = ttk.Combobox(bar, width=18, state='readonly')
        self.preset.pack(side='left', padx=4)
        ttk.Button(bar, text='Apply preset', command=self.apply_preset_ui).pack(side='left')
        ttk.Button(bar, text='Reload presets', command=self.load_presets).pack(side='left', padx=4)
        self.fill = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text='bags fill their ammo', variable=self.fill).pack(side='left', padx=8)
        self.scene = tk.BooleanVar(value=False)
        self.scene_cb = ttk.Checkbutton(bar, text='with the "you got" scene (Core)', variable=self.scene)
        self.scene_cb.pack(side='left', padx=8)
        self.scene_tip = Tip(self.scene_cb, '')
        self.msg = ttk.Label(f, text='', wraplength=1300, justify='left')
        self.msg.pack(anchor='w')
        # scrollable body: four columns
        outer = ttk.Frame(f)
        outer.pack(fill='both', expand=True)
        try:
            st = ttk.Style()
            st.configure('LO.TButton', padding=(4, 0))
            st.configure('LO.TLabel', padding=(0, 0))
        except Exception:
            pass
        cv = tk.Canvas(outer, highlightthickness=0, bg=_bg(app))
        sb = ttk.Scrollbar(outer, orient='vertical', command=cv.yview)
        sx = ttk.Scrollbar(outer, orient='horizontal', command=cv.xview)
        body = ttk.Frame(cv)
        body.bind('<Configure>', lambda e: cv.configure(scrollregion=cv.bbox('all')))
        cv.create_window((0, 0), window=body, anchor='nw')
        cv.configure(yscrollcommand=sb.set, xscrollcommand=sx.set)
        sx.pack(side='bottom', fill='x')
        cv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        cv.bind_all('<MouseWheel>', lambda e: self._wheel(cv, e), add='+')
        self.canvas = cv
        cols = [ttk.Frame(body) for _ in range(2)]
        for i, c in enumerate(cols):
            c.grid(row=0, column=i, sticky='n', padx=4)
        where = {'Items': cols[0], 'Equipment': cols[0], 'Upgrades': cols[1], 'Quest': cols[1]}
        for title, rows in SECTIONS:
            lf = ttk.LabelFrame(where[title], text=title, padding=4)
            lf.pack(fill='x', pady=(0, 6))
            for r, row in enumerate(rows):
                self._row(lf, r, row)
        sl = ttk.LabelFrame(cols[0], text='Equipped / assigned (raw, unconfirmed: may apply at the next load)', padding=4)
        sl.pack(fill='x')
        self.slot_ui = {}
        for r, (label, name) in enumerate((('child item button', 'PlayerStatus.AssignableItem.Child'),
                                            ('adult item button', 'PlayerStatus.AssignableItem.Adult'),
                                            ('sword', 'PlayerStatus.EquipWeapon'), ('shield', 'PlayerStatus.EquipShield'),
                                            ('tunic', 'PlayerStatus.EquipArmor'), ('boots', 'PlayerStatus.EquipBoot'),
                                            ('mask / head', 'PlayerStatus.EquipHeadArmor'), ('ocarina', 'PlayerStatus.EquipOcarina'))):
            ttk.Label(sl, text=label).grid(row=r, column=0, sticky='w')
            cb = ttk.Combobox(sl, width=24)
            cb.grid(row=r, column=1, padx=4, pady=1)
            ttk.Button(sl, text='Set', width=5, command=lambda n=name, cb=cb: self.set_slot(n, cb.get())).grid(row=r, column=2)
            self.slot_ui[name] = cb
        self.load_presets()
        self._scene_state()
        app.bg(self._read_pouch, self._pouch_done)
        app.root.after(1500, self._tick)

    # ------------------------------------------------------------ building
    def _row(self, parent, r, row):
        ttk.Label(parent, text=row.label, style='LO.TLabel').grid(row=r, column=0, sticky='w')
        st = ttk.Label(parent, text='-', width=18, foreground='#9aa3b2', style='LO.TLabel')
        st.grid(row=r, column=1, sticky='w', padx=4)
        w = {'state': st, 'pick': None}
        c = 2
        if row.kind in ('count', 'hearts'):
            top = 20 if row.kind == 'hearts' else 999
            var = tk.StringVar(value=str(3 if row.kind == 'hearts' else (row.default or row.fixed or 1)))
            sp = ttk.Spinbox(parent, from_=0, to=top, textvariable=var, width=5)
            sp.grid(row=r, column=c, padx=2)
            w['pick'] = var
        elif row.kind in ('tier', 'magic'):
            cb = ttk.Combobox(parent, values=[lv[0] for lv in row.levels], width=14, state='readonly')
            cb.set(row.levels[-1][0])
            cb.grid(row=r, column=c, padx=2)
            w['pick'] = cb
        elif row.kind == 'bottle':
            cb = ttk.Combobox(parent, values=BOTTLE_CONTENTS, width=14, state='readonly')
            cb.set('Empty')
            cb.grid(row=r, column=c, padx=2)
            w['pick'] = cb
        c += 1
        ttk.Button(parent, text='Give', width=5, style='LO.TButton', command=lambda: self.give(row)).grid(row=r, column=c, pady=1)
        ttk.Button(parent, text='Take', width=5, style='LO.TButton', command=lambda: self.take(row)).grid(row=r, column=c + 1, pady=1)
        if row.slot and row.kind == 'flag':
            ttk.Button(parent, text='Equip', width=6, style='LO.TButton', command=lambda: self.equip(row)).grid(row=r, column=c + 2, pady=1)
        if row.note:
            Tip(st, row.note)
        self.ui[row.id] = w

    def _wheel(self, cv, e):
        try:
            if self.app.nb.select() == str(self.frame):
                cv.yview_scroll(int(-e.delta / 120), 'units')
        except Exception:
            pass

    def load_presets(self):
        self.presets, err = load_presets()
        names = [n for n in self.presets if not n.startswith('_')]
        self.preset.configure(values=names)
        if names and self.preset.get() not in names:
            self.preset.set(names[0])
        if err:
            self.app.status(err, 'red')

    def _scene_state(self):
        L = self.app._core_lists()
        if not core_confirmed():
            self.scene.set(False)
            self.scene_cb.state(['disabled'])
            self.scene_tip.text = ('Disabled: the DevTool Core give-item command is not confirmed in-game yet. '
                                   'Raw gives work without it.')
        elif not L.get('items'):
            self.scene.set(False)
            self.scene_cb.state(['disabled'])
            self.scene_tip.text = 'Disabled: build DevTool Core (Mod manager) first; its give-item list is missing.'
        else:
            self.scene_cb.state(['!disabled'])
            self.scene_tip.text = 'Gives through the Core give-item command (one item, the game\'s own scene), then sets counts raw.'

    # ------------------------------------------------------------ dump data
    def _read_pouch(self):
        import catalog
        rf = gamedata.RomFS(self.app.romfs)
        return {r.get('__RowId'): r for r in catalog._rsdb(rf, self.app.romfs, 'PouchActorInfo')}

    def _pouch_done(self, rows):
        self.pouch = rows or {}
        n = 0
        for row in all_rows():
            if row.kind == 'tier' and getattr(row, 'bag_prefix', None):
                for i, (lv, acts, ex) in enumerate(row.levels):
                    if acts:
                        info = self.pouch.get(acts[-1]) or {}
                        if info.get('AfterExpansionMaxNum'):
                            ex['cap'] = int(info['AfterExpansionMaxNum'])
                            ex['stock_max_if_get'] = bool(info.get('IsItemStockMaxIfGet'))
                            n += 1
        self.app.log('Loadout: PouchActorInfo %d rows read, %d bag sizes from the dump' % (len(self.pouch), n))
        self.refresh()

    # ------------------------------------------------------------ reading
    def src(self):
        a = self.app
        if a.live and a.live.host:
            return 'live'
        if a.save:
            return 'save'
        return None

    def val(self, name):
        return self.app._val(name)

    def has(self, name):
        h = H(name)
        a = self.app
        return bool((a.live and h in a.live.by_hash) or (a.save and h in a.save.by_hash) or (h in (a.defs or {})))

    def max_hash(self, key):
        d = (self.app.defs or {}).get(MAX_STRUCT)
        if d is None or not d.members:     # after a game update: find the struct by its members
            want = {H(k) for k in MAX_KEYS}
            for h, dd in (self.app.defs or {}).items():
                if dd.type == 'Struct' and dd.members and want <= {m for m, _ in dd.members}:
                    d = dd
                    break
        if d is None or not d.members:
            return None
        for m, full in d.members:
            if m == H(key):
                return full
        return None

    def cap(self, row):
        if row.fixed:
            return row.fixed
        if row.cap:
            mh = self.max_hash(row.cap)
            if mh is None:
                return None
            return self._raw_val(mh)
        return None

    def _raw_val(self, h):
        a = self.app
        if a.live and h in a.live.by_hash:
            return a.live.vals.get(h)
        if a.save and h in a.save.by_hash:
            return a.save.get(a.save.by_hash[h])
        return None

    def isget(self, actor):
        return bool(self.val('IsGet.' + actor))

    def stock(self, actor):
        v = self.val('PouchData.%s.StockNum' % actor)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    def level(self, row):
        """Index of the highest level whose actors are all owned."""
        best = 0
        for i, (lv, acts, ex) in enumerate(row.levels):
            if acts and all(self.isget(a) for a in acts[-1:]):
                best = i
        return best

    def state_text(self, row):
        if row.kind == 'flag':
            return ('have' if self.isget(row.items[0]) else "don't"), self.isget(row.items[0])
        if row.kind == 'count':
            a = row.items[0]
            n, cap = self.stock(a), self.cap(row)
            capt = ('/%s' % cap) if cap is not None else ''
            if not self.isget(a):
                return ("don't (%d%s)" % (n, capt)) if n else ("don't%s" % ((' cap %s' % cap) if cap is not None else '')), False
            return '%d%s' % (n, capt), True
        if row.kind in ('tier', 'magic'):
            i = self.level(row)
            lv = row.levels[i][0]
            if row.cap:
                c = self._raw_val(self.max_hash(row.cap)) if self.max_hash(row.cap) else None
                return '%s (cap %s)' % (lv, c), i > 0 or row.id == 'wallet'
            return lv, i > 0
        if row.kind == 'bottle':
            b = row.items[0]
            ok = self.val('PlayerStatus.AssignableItem.BottleInfo.%s.IsValid' % b)
            cont = self.app.names.label(self.val('PlayerStatus.AssignableItem.BottleInfo.%s.BottleItem' % b) or 0)
            return ('have: %s' % cont) if ok else "don't", bool(ok)
        if row.kind == 'hearts':
            ml = self.val('PlayerStatus.MaxLife')
            return ('%g hearts' % (ml / 4.0)) if isinstance(ml, int) else '-', True
        return '-', False

    def refresh(self, full=True):
        if not self.src():
            for w in self.ui.values():
                w['state'].configure(text='(connect or load a save)', foreground='#9aa3b2')
            return
        for rid, w in self.ui.items():
            try:
                t, on = self.state_text(ROWS[rid])
            except Exception as e:
                t, on = 'n/a (%s)' % e.__class__.__name__, False
            w['state'].configure(text=t, foreground='#7ee787' if on else '#9aa3b2')
        for name, cb in self.slot_ui.items():
            d = (self.app.defs or {}).get(H(name))
            v = self.val(name)
            if d is not None and d.type == 'Enum' and d.raw_values:
                cb.configure(values=d.raw_values)
                v = self.app.names.label(v) if v is not None else ''
            else:
                cb.configure(values=self._equip_choices(name))
            try:
                focused = self.app.root.focus_get() is cb
            except Exception:
                focused = False
            if not focused:
                cb.set(v if v is not None else '')
        if full:
            self._scene_state()

    def _equip_choices(self, name):
        return [''] + sorted({a for r in all_rows() if r.slot == name for a in r.actors()})

    def _tick(self):
        try:
            if self.app.nb.select() == str(self.frame) and self.src() == 'live':
                self._ticks = getattr(self, '_ticks', 0) + 1
                self.refresh(full=self._ticks % 5 == 0)
        except Exception:
            pass
        self.app.root.after(1000, self._tick)

    # ------------------------------------------------------------ writing
    def _set(self, name, value, out):
        d = (self.app.defs or {}).get(H(name))
        if d is not None and d.type == 'Enum' and isinstance(value, str):
            if not d.raw_values or value not in d.raw_values:
                out.append('%s: no value %r' % (name, value))
                return
            value = d.values[d.raw_values.index(value)]
        cur = self.val(name)
        if cur is not None and type(cur) is type(value) and cur == value:
            return                              # already so: no write, no log line
        try:
            out.append(self.app._set(name, value))
        except Exception as e:
            out.append('%s: %s' % (name, e))

    def _own(self, actor, on, out, count=None):
        """IsGet.<actor> and its StockNum (1 for key items, count for ammo; 0 when taken)."""
        self._set('IsGet.' + actor, bool(on), out)
        sn = 'PouchData.%s.StockNum' % actor
        if self.has(sn):
            if not on:
                self._set(sn, 0, out)
            elif count is not None:
                self._set(sn, int(count), out)
            elif self.stock(actor) < 1:
                self._set(sn, 1, out)

    def _stage(self, name, value, out):
        """Life / MP: Link reads them at the next loading screen; hold them there when live (Player tab logic)."""
        h = H(name)
        a = self.app
        if a.live and a.live.host and h in a.live.by_hash:
            a.stage(h, value)
            out.append('%s = %s held for the next loading screen' % (name, value))
        else:
            self._set(name, value, out)

    def apply(self, row, value, out):
        """Bring one row to value (see DEFAULT_PRESETS['_doc'] for the value forms). Appends log text to out."""
        k = row.kind
        if k == 'flag':
            self._own(row.items[0], bool(value), out)
            for name, (give, take) in row.extra.items():
                if self.has(name):
                    self._set(name, give if value else take, out)
            if not value and row.slot and self.val(row.slot) == row.items[0]:
                self._set(row.slot, '', out)
        elif k == 'count':
            a, n = row.items[0], int(value or 0)
            if n > 0 and row.bag and row.cap:
                c = self.cap(row)
                if not c:   # no bag yet: give the smallest one so the count has room (bombs with no bag stayed invisible)
                    bag = ROWS[row.bag]
                    self.apply(bag, bag.levels[1][0], out)
                    out.append('(no %s yet: gave the %s one so the count has a cap)' % (bag.label, bag.levels[1][0]))
                    c = self.cap(row)
                if c is not None and n > c:
                    out.append('note: %d is above the cap %s: the game may cut it down on its next change' % (n, c))
            if n > 0:
                self._own(a, True, out, n)      # StockNum > 0 -> IsGet True (the v0.8.1 fix) + explicit here too
            else:
                self._own(a, False, out)
        elif k in ('tier', 'magic'):
            names = [lv[0] for lv in row.levels]
            if value not in names:
                out.append('%s: no level %r (%s)' % (row.label, value, ', '.join(names)))
                return
            i = names.index(value)
            owned = set(row.levels[i][1])
            for a in {x for lv in row.levels for x in lv[1]}:
                self._own(a, a in owned, out)
            ex = dict(row.levels[i][2])
            cap = ex.pop('cap', None)
            fill_max = ex.pop('stock_max_if_get', True)
            for name, v in ex.items():
                if self.has(name):
                    self._set(name, v, out)
            if row.cap:
                mh = self.max_hash(row.cap)
                c = cap if cap is not None else 0
                if mh is None:
                    out.append('%s cap: struct #F80F8077 not found in this GameData (cap not set)' % row.label)
                else:
                    self._set_hash(mh, c, out, '%s cap (#F80F8077.%s)' % (row.label, row.cap))
                if c and self.fill.get() and fill_max:
                    for a in getattr(row, 'fills', []):
                        self._own(a, True, out, c)
                    for a in getattr(row, 'fills_if_owned', []):
                        if self.isget(a):
                            self._own(a, True, out, c)
                elif not c:
                    for a in getattr(row, 'fills', []) + getattr(row, 'fills_if_owned', []):
                        if self.stock(a):
                            self._set('PouchData.%s.StockNum' % a, 0, out)
            if row.slot:
                self._set(row.slot, row.levels[i][1][-1] if row.levels[i][1] else '', out)
            if k == 'magic':
                mx = (0, 240, 480)[min(i, 2)]
                self._set('PlayerStatus.MPMax', mx, out)
                self._stage('PlayerStatus.MP', mx, out)
        elif k == 'bottle':
            b = row.items[0]
            base = 'PlayerStatus.AssignableItem.BottleInfo.%s.' % b
            if value is None or value is False:
                self._set(base + 'IsValid', False, out)
                self._set(base + 'BottleItem', 'Empty', out)
            else:
                self._set(base + 'BottleItem', value if isinstance(value, str) else 'Empty', out)
                self._set(base + 'IsValid', True, out)
                if not self.isget('Item_Bottle'):
                    self._set('IsGet.Item_Bottle', True, out)
        elif k == 'hearts':
            n = max(1, min(20, int(value or 3)))
            self._set('PlayerStatus.MaxLife', n * 4, out)
            self._stage('PlayerStatus.Life', n * 4, out)

    def _set_hash(self, h, value, out, label):
        a = self.app
        try:
            if a.live and a.live.host and h in a.live.by_hash:
                before = a.live.vals.get(h)
                if before == value:
                    return
                a.live.write(h, value)
                a.flog_tool(a.live.by_hash[h], before, a.live.vals.get(h), 'set by you (Loadout)')
                out.append('%s: %s -> %s (live)' % (label, before, value))
            elif a.save and h in a.save.by_hash:
                a.save.set(a.save.by_hash[h], value)
                try:
                    a.edited.add(h)
                except Exception:
                    pass
                out.append('%s = %s (save file; click Save changes on World state)' % (label, value))
            else:
                out.append('%s: not in the live game or the save' % label)
        except Exception as e:
            out.append('%s: %s' % (label, e))

    def off_value(self, row):
        if row.kind == 'flag':
            return False
        if row.kind == 'count':
            return 0
        if row.kind in ('tier', 'magic'):
            return row.levels[0][0]
        if row.kind == 'bottle':
            return None
        if row.kind == 'hearts':
            return 3

    def max_value(self, row):
        if row.kind == 'flag':
            return True
        if row.kind == 'count':
            return row.fixed or {'Bomb': 40, 'Arrow': 50, 'DekuSeed': 50, 'DekuStick': 30, 'DekuNut': 40}.get(row.cap, 1) \
                if row.id != 'heart_pieces' else 0
        if row.kind in ('tier', 'magic'):
            return row.levels[-1][0]
        if row.kind == 'bottle':
            return 'Empty'
        if row.kind == 'hearts':
            return 20

    def picked(self, row):
        w = self.ui[row.id]
        if row.kind == 'flag':
            return True
        if row.kind in ('count', 'hearts'):
            try:
                return int(w['pick'].get())
            except ValueError:
                return None
        return w['pick'].get()

    def _ready(self):
        if not self.src():
            self.app.status('Loadout: Connect (with write access) or load a save on World state first.', 'red')
            return False
        return True

    def _report(self, what, out):
        bad = [t for t in out if ': ' in t and ('not a live' in t or 'not in the live' in t or 'no value' in t or 'failed' in t)]
        self.app.log('[loadout] %s: %s' % (what, '; '.join(out)))
        self.msg.configure(text='%s: %d writes%s' % (what, len(out), (' (%d skipped: %s)' % (len(bad), bad[0])) if bad else ''))
        self.app.status('Loadout: %s (%s)' % (what, 'live' if self.src() == 'live' else 'save file: Save changes on World state'),
                        'darkgreen' if not bad else '#b06000')
        self.refresh()

    def give(self, row):
        if not self._ready():
            return
        v = self.picked(row)
        if v is None:
            return self.app.status('Not a number.', 'red')
        if self.scene.get() and self.src() == 'live' and row.kind in ('flag', 'count', 'tier', 'magic'):
            actor = row.items[0] if row.kind in ('flag', 'count') else (row.levels[[lv[0] for lv in row.levels].index(v)][1] or [None])[-1]
            items = [i[0] for i in (self.app._core_lists().get('items') or [])]
            if actor in items:
                def done(ok):
                    out = []
                    if row.kind != 'flag':
                        self.apply(row, v, out)
                    self._report('give %s with the scene (Core %s)' % (row.label, 'Ack' if ok else 'no Ack'), out)
                return self.app.core_send('item_scene', items.index(actor), '%s with the item-get scene' % actor, done=done)
            self.app.log('[loadout] %s is not in the Core item list: raw give' % actor)
        out = []
        self.apply(row, v if row.kind != 'bottle' else (v or 'Empty'), out)
        self._report('give %s (%s)' % (row.label, v), out)

    def take(self, row):
        if not self._ready():
            return
        out = []
        self.apply(row, self.off_value(row), out)
        self._report('take %s' % row.label, out)

    def equip(self, row):
        if not self._ready():
            return
        out = []
        if not self.isget(row.items[0]):
            self._own(row.items[0], True, out)
        self._set(row.slot, row.items[0], out)
        self._report('equip %s (unconfirmed: Link may pick it up only at the next load)' % row.label, out)

    def set_slot(self, name, value):
        if not self._ready():
            return
        out = []
        self._set(name, value, out)
        self._report('set %s = %s' % (name, value), out)

    def apply_preset(self, name):
        p = self.presets.get(name)
        if p is None:
            raise KeyError(name)
        out = []
        if p.get('mode') == 'replace':
            for row in all_rows():
                if row.kind != 'tier' or row.id not in (p.get('set') or {}):
                    self.apply(row, self.off_value(row), out)
        sets = p.get('set')
        if sets == 'ALL':
            sets = {r.id: self.max_value(r) for r in all_rows()}
        order = sorted((sets or {}).items(), key=lambda kv: 0 if ROWS.get(kv[0]) and ROWS[kv[0]].kind == 'tier' else 1)
        for rid, v in order:      # upgrades first, so the counts after them have their caps
            row = ROWS.get(rid)
            if row is None:
                out.append('unknown row id %r in data\\loadouts.json' % rid)
                continue
            self.apply(row, v, out)
        return out

    def apply_preset_ui(self):
        if not self._ready():
            return
        name = self.preset.get()
        try:
            out = self.apply_preset(name)
        except KeyError:
            return self.app.status('No preset %r.' % name, 'red')
        self._report('preset "%s" (raw)' % name, out)


BOTTLE_CONTENTS = ['Empty', 'RedPotion', 'GreenPotion', 'BluePotion', 'BlueFire', 'Fish', 'Letter', 'BigPoe', 'Poe', 'Bug',
                   'Fairy', 'MaxMilk', 'HalfMilk']


def _bg(app):
    try:
        return ttk.Style().lookup('TFrame', 'background') or '#1b1d22'
    except Exception:
        return '#1b1d22'


class Tip:
    """A small hover tooltip."""

    def __init__(self, widget, text):
        self.w, self.text, self.top = widget, text, None
        widget.bind('<Enter>', self.show, add='+')
        widget.bind('<Leave>', self.hide, add='+')

    def show(self, e=None):
        if not self.text or self.top:
            return
        self.top = tk.Toplevel(self.w)
        self.top.wm_overrideredirect(True)
        self.top.geometry('+%d+%d' % (self.w.winfo_rootx() + 10, self.w.winfo_rooty() + self.w.winfo_height() + 4))
        tk.Label(self.top, text=self.text, justify='left', wraplength=420, background='#2b2f37', foreground='#e3e6eb',
                 relief='solid', borderwidth=1, padx=6, pady=3).pack()

    def hide(self, e=None):
        if self.top:
            self.top.destroy()
            self.top = None
