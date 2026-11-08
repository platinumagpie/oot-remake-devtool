"""OoT Dev Tool - a build-agnostic developer panel for the Ocarina of Time remake on Lichii.

Tabs
  Session      Lichii build (Frizti default + dropdown, v0.5.9), build status read from the Lichii Builds
               tray app (v0.8.6, read-only), launch with
               the debug server, connect, self-test
  World state  edit any GameData value (flags, items, counters, time...) in a save slot or,
               since v0.5, live in the running game; snapshot + diff to discover flags
  Flag log     (v0.5) every GameData value the game changes while you play, with marks
  Warp         (v0.5.1) pick a map + start point, then walk through any door; Farore's Wind point
               editor and position bookmarks (v0.5.3)
  Player       (v0.5.3) rupees, hearts, magic and ammo, with freeze
  Live memory  read / search / write guest memory, scan-and-refine, watches with freeze,
               find a flag's hash in RAM
  Camera       FOV per camera mode, camera distance, stick sensitivity (built as a RomFS mod)
  World        (v0.7) map select (415 start points), reload / void out, age swap, time of day, weather,
               HUD, event player, BGM / SE / message test; raw GameData route and DevTool Core route
  Items        (v0.7) give any item, bottles, songs, equipment, dungeon items, bosses, medallions,
               quest-flag viewer
  Player tools (v0.8) the player bridge (god mode, moon jump, gravity, debug move, teleport), speed and
               no-fall-damage params, save-state substitute, split timer, FPS
  Mod manager  (v0.6) every RomFS mod folder: enable/disable, conflicts, the merged RESTBL, the
               fog/perf builders and DevTool Core (the command channel; "hello" = +1 rupee)
  Cheats       edit this game build's Atmosphere-format cheat file
  (v0.4)       screenshots, Ghidra-ready main, game catalog, GameData probe (Session tab)

Run:  pythonw devtool.pyw   (needs: pip install zstandard; optional: pip install mmh3)
"""
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gamedata  # noqa: E402
import lichii  # noqa: E402
import savefile  # noqa: E402
import probes  # noqa: E402
import mainimg  # noqa: E402
import gdlive  # noqa: E402
import checker  # noqa: E402
import core  # noqa: E402
import mods  # noqa: E402
import restbl  # noqa: E402
import player  # noqa: E402
import loadout  # noqa: E402
import fog  # noqa: E402

VERSION = '0.8.7'
USER = os.path.join(HERE, 'user')
SETTINGS = os.path.join(USER, 'settings.json')
LOGFILE = os.path.join(USER, 'devtool.log')
MAX_ROWS = 3000

CHEAT_TEMPLATE = """\
[OoT Dev Tool - read me]
; Atmosphere cheat format, one cheat per [name] block. Lichii reads this file when the game
; boots, so restart the game after editing. Toggle cheats in Lichii: right-click the game ->
; Properties -> Add-Ons ("DevTool" entry) or the cheat list.
; Syntax: https://github.com/Atmosphere-NX/Atmosphere/blob/master/docs/features/cheats.md
; Example - write 999 (0x3E7) as u32 to main+0x123456 (offset is a placeholder!):
;   04000000 00123456 000003E7
; Find real addresses in the Live memory tab first, then turn them into codes here.

"""

TYPES = ['u8', 'u16', 'u32', 'i32', 'f32', 'u64', 'f64', 'hex', 'string']
FMT = {'u8': '<B', 'u16': '<H', 'u32': '<I', 'i32': '<i', 'f32': '<f', 'u64': '<Q', 'f64': '<d'}


def encode(typ, text):
    if typ == 'hex':
        return bytes.fromhex(text.replace(' ', ''))
    if typ == 'string':
        return text.encode('utf-8')
    v = float(text) if typ in ('f32', 'f64') else int(text, 0)
    return struct.pack(FMT[typ], v)


def decode(typ, b):
    if typ == 'hex':
        return b.hex(' ')
    if typ == 'string':
        return b.split(b'\0')[0].decode('utf-8', 'replace')
    v = struct.unpack(FMT[typ], b[:struct.calcsize(FMT[typ])])[0]
    return ('%.6g' % v) if typ.startswith('f') else str(v)


def size_of(typ, n=16):
    return struct.calcsize(FMT[typ]) if typ in FMT else n


def fmt_val(v, limit=80):
    if isinstance(v, float):
        s = '%.6g' % v
    elif isinstance(v, bytes):
        s = v[:24].hex(' ') + (' ...' if len(v) > 24 else '')
    elif isinstance(v, (list, tuple)):
        if v and isinstance(v[0], float):
            s = '(' + ', '.join('%.4g' % x for x in v) + ')'
        else:
            s = str(list(v) if isinstance(v, list) else v)
    else:
        s = str(v)
    return s if len(s) <= limit else s[:limit] + '...'


class App:
    def __init__(self, root):
        self.root = root
        root.title('OoT Dev Tool v%s' % VERSION)
        root.geometry('1320x800')
        os.makedirs(USER, exist_ok=True)
        self.settings = self._load_settings()
        self.q = queue.Queue()
        self.dbg = None
        self.host = None
        self.modules = {}
        self.save = None
        self.snapshot = None
        self.defs, self.meta, self.names = {}, {}, gamedata.Names()
        self.scan_hits = []
        self.watches = []
        self.live = None           # gdlive.LiveGameData once attached
        self.live_snap = None      # live values at "Snapshot" (World state, live mode)
        self.live_busy = False
        self.live_fail = 0
        self.flog_rows = 0
        self.flog_file = None
        self.flog_mark = 0
        self.flog_recent = {}      # hash -> [times] for auto-mute
        self.flog_muted = set()
        self.tool_touched = {}     # hash -> last time the tool wrote it (never auto-muted for 2 minutes after)
        self.frozen = {}           # hash -> value the poller keeps writing back (World state, live)
        self.freeze_hits = {}      # hash -> recent rewrite times
        self.freeze_hinted = set()
        self.staged = {}           # hash -> value held through the next loading screen (Player tab)
        self.stage_state = {}
        self.stage_watch = {}      # hash -> (value, since): after a load, watch whether the game keeps it
        self.magic_busy = False
        self.warp = None           # armed warp state (Warp tab)
        self.warp_shot = None      # (banc, time) -> screenshot once the warp's loading ends
        self.catalog = None
        self._discover()

        root.report_callback_exception = self._tk_error
        self._place_window()
        top = ttk.Frame(root, padding=(8, 4))
        top.pack(fill='x')
        self.conn_lbl = ttk.Label(top, text='Not connected', foreground='#a00000', font=('Segoe UI', 10, 'bold'))
        self.conn_lbl.pack(side='left')
        self.status_lbl = ttk.Label(top, text='', font=('Segoe UI', 10))
        self.on_top = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='Keep this window on top', variable=self.on_top,
                        command=lambda: root.attributes('-topmost', self.on_top.get())).pack(side='right')
        ttk.Button(top, text='Screenshot', command=lambda: self.do_screenshot('manual')).pack(side='right', padx=8)
        self.builds_ind = ttk.Label(top, text='\u25cf builds', foreground=C['dim'], cursor='hand2')   # tray-style dot
        self.builds_ind.pack(side='right', padx=8)
        self.builds_ind.bind('<Button-1>', lambda e: self.nb.select(0))
        self.status_lbl.pack(side='left', padx=16)   # packed last so it never squeezes the buttons
        infobar = ttk.Frame(root, padding=(8, 0, 8, 2))
        infobar.pack(fill='x')
        self.info_lbl = ttk.Label(infobar, text='Live game info shows here once connected', foreground=C['dim'],
                                  font=('Segoe UI', 10))
        self.info_lbl.pack(side='left')
        self._shot_img = None
        nb = ttk.Notebook(root)
        nb.pack(fill='both', expand=True)
        self.nb = nb
        self._tab_session(nb)
        self._tab_world(nb)
        self._tab_flaglog(nb)
        self._tab_warp(nb)
        self._tab_player(nb)
        self._tab_worldctl(nb)
        self._tab_items(nb)
        self._tab_ptools(nb)
        self._tab_live(nb)
        self._tab_camera(nb)
        self._tab_mods(nb)
        self._tab_cheats(nb)
        self.loadout = loadout.LoadoutTab(self, nb)   # v0.8.1, loadout.py
        self.logbox = tk.Text(root, height=7, wrap='word', font=('Consolas', 9))
        self.logbox.pack(fill='x', side='bottom', before=nb)   # always keeps its 7 lines
        self.log('---- OoT Dev Tool %s started (Python %s). Lichii build: %s' % (
            VERSION, sys.version.split()[0], self.build or 'none found'))
        try:
            import zstandard  # noqa: F401
        except ImportError:
            self.status('Missing Python package zstandard: click "Install missing packages" on the Session tab, '
                        'then restart the tool', 'red')
        root.after(100, self._pump)
        root.after(500, self._watch_tick)
        root.after(1000, self._live_tick)
        root.after(4000, self._builds_tick)
        self.bg(lambda: lichii.log_excerpt(self.data),
                lambda lines: [self.log('[lichii log, last boot] ' + ln) for ln in lines])
        self.bg(self._load_gamedata, lambda r: self._after_gamedata(r))

    # ------------------------------------------------------------ plumbing
    def _load_settings(self):
        try:
            return json.load(open(SETTINGS))
        except Exception:
            return {'port': lichii.DEFAULT_PORT}

    def _save_settings(self):
        json.dump(self.settings, open(SETTINGS, 'w'), indent=1)

    def log(self, msg):
        line = time.strftime('%H:%M:%S ') + msg
        self.logbox.insert('end', line + '\n')
        self.logbox.see('end')
        try:  # persistent copy: user\\devtool.log (helps diagnose problems after the fact)
            with open(LOGFILE, 'a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d ') + line + '\n')
        except Exception:
            pass

    def status(self, text, color='black'):
        """Big status line under the tabs - results show here instead of popups that can
        end up behind a fullscreen game."""
        self.status_lbl.configure(text=text, foreground=color)
        self.log(text)

    def _place_window(self):
        """settings.json "monitor": 2 opens the tool on the second monitor (1 = primary)."""
        want = self.settings.get('monitor')
        if not want or not lichii.IS_WIN:
            return
        try:
            import ctypes
            from ctypes import wintypes
            mons = []
            cb_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

            def cb(h, dc, r, lp):
                mons.append((r.contents.left, r.contents.top, r.contents.right, r.contents.bottom))
                return 1
            ctypes.windll.user32.EnumDisplayMonitors(0, 0, cb_t(cb), 0)
            prim = [m for m in mons if m[0] == 0 and m[1] == 0]
            ordered = prim + [m for m in mons if m not in prim]
            m = ordered[min(int(want), len(ordered)) - 1]
            self.root.geometry('1320x800+%d+%d' % (m[0] + 40, m[1] + 40))
        except Exception as e:
            print('window placement failed:', e)

    def _tk_error(self, exc, val, tb):
        self.log('INTERNAL ERROR: ' + ''.join(traceback.format_exception(exc, val, tb))[-1500:])

    def bg(self, fn, done=None):
        def run():
            try:
                r = fn()
                self.q.put((done, r, None))
            except Exception as e:
                self.q.put((done, None, e))
        threading.Thread(target=run, daemon=True).start()

    def _pump(self):
        try:
            while True:
                done, r, e = self.q.get_nowait()
                try:
                    if e is not None:
                        self.status('ERROR: %s' % e, 'red')
                        if isinstance(e, lichii.DebugError):
                            self.conn_lbl.configure(text='Not connected', foreground='#a00000')
                            self.dbg = None
                        if not isinstance(e, (lichii.DebugError, OSError, ValueError)):
                            self.log(''.join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:])
                    elif done:
                        done(r)
                except Exception:
                    self.log('INTERNAL ERROR: ' + traceback.format_exc()[-1500:])
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._pump)

    def _discover(self):
        st = self.settings
        self.builds = lichii.all_builds()          # installed builds under ZELDAOOT only (ARCHIVE not scanned)
        self.build = lichii.resolve_lichii(st.get('lichii_dir')) or lichii.default_build(self.builds)
        self.dump = lichii.resolve_dump(st.get('dump_dir')) or lichii.find_dump()
        if not self.build or not self.dump:
            self._ask_folders(not self.build, not self.dump)
        self.data = lichii.data_dir(self.build)
        self.romfs = os.path.join(self.dump, 'romfs') if self.dump else lichii.find_romfs()
        self.main = os.path.join(self.dump, 'exefs', 'main') if self.dump else None
        try:
            self.bid = lichii.build_id(self.main)
        except Exception:
            self.bid = '?' * 40
        self.tid = lichii.title_id(os.path.join(self.dump, 'exefs', 'main.npdm')) if self.dump else lichii.TITLE_ID
        self.saves = lichii.save_dirs(self.data, self.tid)

    def _ask_folders(self, need_build=True, need_dump=True):
        """First run on a new PC: ask where Lichii and the unpacked game are, remember it."""
        if need_build:
            messagebox.showinfo('OoT Dev Tool - setup', 'Pick your Lichii folder\n(the folder that has lichii.exe in it).')
            p = filedialog.askdirectory(title='Pick your Lichii folder (the one with lichii.exe)')
            b = lichii.resolve_lichii(p)
            if b:
                self.build = b
                self.settings['lichii_dir'] = b
            elif p:
                messagebox.showerror('OoT Dev Tool', 'No lichii.exe in that folder. You can set it later with '
                                     '"Change folders" on the Session tab.')
        if need_dump:
            messagebox.showinfo('OoT Dev Tool - setup', 'Pick your UNPACKED game folder\n(the OoT_dump folder '
                                'you made with "Extract NUP" in Lichii).')
            p = filedialog.askdirectory(title='Pick your unpacked game folder (OoT_dump)')
            d = lichii.resolve_dump(p)
            if d:
                self.dump = d
                self.settings['dump_dir'] = d
            elif p:
                messagebox.showerror('OoT Dev Tool', 'That folder does not contain an unpacked game '
                                     '(no exefs\\main inside). You can set it later with "Change folders".')
        os.makedirs(USER, exist_ok=True)
        self._save_settings()

    def do_change_folders(self):
        self._ask_folders(True, True)
        self.log('Folders saved. Close and reopen the tool to use them.')
        self.status('Folders saved - close and reopen the tool to use them.', 'blue')

    def _core(self):
        return core.Core(self.romfs, os.path.join(self.data, 'load'), self.tid, self.log)

    def _load_gamedata(self):
        alt = None
        try:   # v0.6: with the Core mod's GameDataList part built and enabled, the game has DevTool.* records too
            if core.MOD_NAME not in mods.disabled(self.data):
                alt = self._core().gamedatalist_path()
        except Exception:
            alt = None
        defs, meta = gamedata.load_definitions(self.romfs, alt)
        meta['core'] = bool(alt)
        nm = gamedata.Names(defs)
        if alt:   # v0.8.3: name the DevTool.* entries (the flag log showed DevTool.Ack as #D875DCEF)
            for full in [core.STRUCT] + list(core.names(False).values()):
                nm.map.setdefault(gamedata.mmh(full), full)
        return defs, meta, nm

    def _after_gamedata(self, r):
        self.defs, self.meta, self.names = r
        self.log('GameData: %d definitions (%s%s), %d names known'
                 % (len(self.defs), self.meta.get('file'), ' + DevTool Core entries' if self.meta.get('core') else '',
                    self.names.count()))
        self._reload_slots()
        self.warp_fill()
        self.root.after(800, self._first_run_builds)

    def _first_run_builds(self):
        """(v0.8.7) A fresh install has no catalog and no names.tsv: both are generated from the user's own dump
        and never shipped. Without the catalog the Warp, Items and Loadout lists are empty, which looked like a bug
        to the first person who downloaded the tool. So: build the catalog by itself on first start, and say what
        to click for the names."""
        if not self.romfs or not os.path.isdir(self.romfs) or not self.bid or self.bid.startswith('?'):
            return
        cat = os.path.join(HERE, 'data', 'catalog_%s.json' % self.bid[:16])
        names = os.path.join(HERE, 'names.tsv')
        if not os.path.exists(cat):
            self.log('First run: no game catalog for build %s yet, building it from your dump.' % self.bid[:16])
            self.status('First run: building the game catalog from your dump (about a minute). The Warp, Items and '
                        'Loadout lists fill in when it finishes.', 'blue')
            self.do_catalog()
        if not os.path.exists(names):
            self.log('First run: names.tsv is missing, so flags show as hashes. Session tab -> "Rebuild flag names" '
                     'generates it from your dump (a few minutes, in a console window).')
            if os.path.exists(cat):
                self.status('No names.tsv yet: flags show as hashes until you click "Rebuild flag names" on the Session '
                            'tab (a few minutes, console window).', '#b06000')

    # ============================================================ Session tab
    def _tab_session(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text='Session')
        info = ttk.LabelFrame(f, text='Detected', padding=8)
        info.pack(fill='x')
        ttk.Label(info, text='Lichii build:', width=14).grid(row=0, column=0, sticky='w')
        self.build_box = ttk.Combobox(info, width=107, state='readonly')
        self.build_box.grid(row=0, column=1, sticky='w')
        self.build_box.bind('<<ComboboxSelected>>', self._pick_build)
        rows = [('Lichii data', self.data),
                ('Game dump', self.dump or 'NOT FOUND'),
                ('Title ID', self.tid),
                ('Game build ID', self.bid),
                ('Save folder', self.saves[0] if self.saves else 'not found yet')]
        self.info_rows = {}
        for i, (k, v) in enumerate(rows, 1):
            ttk.Label(info, text=k + ':', width=14).grid(row=i, column=0, sticky='w')
            e = ttk.Entry(info, width=110)
            e.insert(0, v)
            e.configure(state='readonly')
            e.grid(row=i, column=1, sticky='w')
            self.info_rows[k] = e
        self._fill_builds()
        ctl = ttk.Frame(f, padding=(0, 10))
        ctl.pack(fill='x')
        ttk.Label(ctl, text='Debug server port').pack(side='left')
        self.port = tk.StringVar(value=str(self.settings.get('port', lichii.DEFAULT_PORT)))
        ttk.Entry(ctl, textvariable=self.port, width=7).pack(side='left', padx=4)
        ttk.Button(ctl, text='Launch Lichii (debug server on)', command=self.do_launch).pack(side='left', padx=4)
        ttk.Button(ctl, text='Connect', command=self.do_connect).pack(side='left', padx=4)
        ttk.Button(ctl, text='Self-test this build', command=self.do_selftest).pack(side='left', padx=4)
        ttk.Button(ctl, text='Test command (Core: +1 rupee)', command=self.core_test).pack(side='left', padx=(16, 4))
        self.core_lbl = ttk.Label(ctl, text='')
        self.core_lbl.pack(side='left', padx=4)
        setup = ttk.Frame(f, padding=(0, 0, 0, 8))
        setup.pack(fill='x')
        ttk.Label(setup, text='Setup').pack(side='left')
        ttk.Button(setup, text='Install missing packages', command=self.do_pip).pack(side='left', padx=4)
        ttk.Button(setup, text='Rebuild flag names', command=self.do_names).pack(side='left', padx=4)
        ttk.Button(setup, text='Change folders', command=self.do_change_folders).pack(side='left', padx=4)
        ttk.Button(setup, text='Open save folder', command=lambda: self._open(self.saves[0] if self.saves else self.data)).pack(side='left', padx=4)
        ttk.Button(setup, text='Open user folder', command=lambda: self._open(USER)).pack(side='left', padx=4)
        pr = ttk.LabelFrame(f, text='Research tools (v0.4)', padding=6)
        pr.pack(fill='x', pady=(0, 6))
        r1 = ttk.Frame(pr)
        r1.pack(fill='x')
        ttk.Label(r1, text='GameData probe: save slot').pack(side='left')
        self.probe_slot = ttk.Combobox(r1, width=12, state='readonly', values=self._slot_names())
        sl = self.probe_slot['values']
        if sl:
            self.probe_slot.set(sl[-1])
        self.probe_slot.pack(side='left', padx=4)
        ttk.Button(r1, text='1. Prepare probe save (game closed)', command=self.do_probe_prepare).pack(side='left', padx=4)
        ttk.Button(r1, text='2. Run v0.4 probes (in-game, after Connect)', command=self.do_probes).pack(side='left', padx=4)
        self.probe_lbl = ttk.Label(r1, text='')
        self.probe_lbl.pack(side='left', padx=8)
        r2 = ttk.Frame(pr)
        r2.pack(fill='x', pady=(4, 0))
        ttk.Button(r2, text='Ghidra ELF of main (offline)', command=self.do_elf).pack(side='left')
        ttk.Button(r2, text='Dump main from RAM (in-game)', command=self.do_ramdump).pack(side='left', padx=4)
        ttk.Button(r2, text='Rebuild game catalog', command=self.do_catalog).pack(side='left', padx=4)
        bc = ttk.LabelFrame(f, text='Lichii builds (read from the Lichii Builds tray app; it is the only installer)', padding=6)
        bc.pack(fill='x', pady=(0, 6))
        self.chk_rows = {}
        for i, src in enumerate(checker.SOURCES):
            ttk.Label(bc, text=src['label'], width=10).grid(row=i, column=0, sticky='w')
            lbl = ttk.Label(bc, text='not read yet', width=96)
            lbl.grid(row=i, column=1, sticky='w')
            ttk.Button(bc, text='Release page', command=lambda s=src: self.chk_page(s)).grid(row=i, column=2, padx=2)
            self.chk_rows[src['name']] = lbl
        r = ttk.Frame(bc)
        r.grid(row=len(checker.SOURCES), column=0, columnspan=3, sticky='w', pady=(4, 0))
        ttk.Button(r, text='Re-read now', command=lambda: self._builds_tick(force=True)).pack(side='left')
        ttk.Button(r, text='Open Lichii Builds folder', command=lambda: self._open(checker.TRAY_DIR)).pack(side='left', padx=4)
        self.chk_lbl = ttk.Label(r, text='')
        self.chk_lbl.pack(side='left', padx=8)
        body = ttk.Frame(f)
        body.pack(fill='both', expand=True)
        self.shot_lbl = ttk.Label(body, text='(screenshots show here)', anchor='center', width=48)
        self.shot_lbl.pack(side='right', fill='y', padx=(6, 0))
        self.sess = tk.Text(body, height=10, font=('Consolas', 9))
        self.sess.pack(side='left', fill='both', expand=True)
        self.sess.insert('end', (
            'How it connects\n'
            '  1. "Launch Lichii" starts the build picked above (Frizti by default) on the loose dump with the debug server\n'
            '     enabled (env EDEN_NX2_DEBUG_SERVER=127.0.0.1:<port>). The server starts once the\n'
            '     game has loaded. Already running Lichii without it? Close it from the in-game menu\n'
            '     first, then launch from here.\n'
            '  2. "Connect" talks to the debug server (reads), reads module bases from eden_log.txt,\n'
            '     and finds the fastmem arena inside lichii.exe (writes). Then it attaches to the\n'
            '     game\'s GameData: the status line, World state (Live game) and the Flag log go live.\n'
            '  3. New Lichii build? The row above says so (read from the Lichii Builds tray app). Install it\n'
            '     from the tray icon, then Launch from here and Self-test. The dev tool never downloads builds.\n'
            '\n'
            'v0.4 probes (research; only needed again after a game update)\n'
            '  1. With the game AND Lichii closed, pick a slot and click "Prepare probe save". It backs the\n'
            '     slot up, writes unique values into 16 records, and sets Deku seeds to 17, rupees to 77.\n'
            '  2. Launch, load THAT slot (the HUD should show 17 seeds / 77 rupees), wait until you can\n'
            '     move Link, Connect, then "Run v0.4 probes". Takes a few minutes: searches, a live write\n'
            '     test on the seed count (watch it drop by 3 for 2 s), a copy of main from RAM and\n'
            '     screenshots. Then it puts the values back. Reports: user\\probe_v04.txt, user\\dump\\.\n'))

    def _open(self, path):
        try:
            os.startfile(path)
        except Exception as e:
            self.log('cannot open %s: %s' % (path, e))

    # ---------------------------------------------------------------- builds (v0.5.9)
    def _build_label(self, b):
        tag = checker.tag_for(b['path'], getattr(self, 'chk_state', None))
        tag = ' [%s]' % tag if tag else ''
        return '%s%s  (%s%s)' % (b['label'], tag, time.strftime('%Y-%m-%d', time.localtime(b['mtime'])),
                                 ', default' if b['default'] else '')

    def _fill_builds(self):
        self.builds = lichii.all_builds()
        if self.build and os.path.normcase(os.path.normpath(self.build)).startswith(os.path.normcase(lichii.ARCHIVE)):
            # v0.8.6: archived builds are not launch choices any more; go back to the default
            self.log('Remembered build %s is archived; using the default build instead.' % self.build)
            self.build = lichii.default_build(self.builds)
            self.settings['lichii_dir'] = self.build
            self._save_settings()
        if self.build and not any(os.path.normcase(b['path']) == os.path.normcase(self.build) for b in self.builds):
            self.builds.insert(0, {'path': self.build, 'label': self.build, 'mtime': 0, 'default': False, 'archived': False})
        vals = [self._build_label(b) for b in self.builds]
        self.build_box['values'] = vals or ['NOT FOUND (put a Lichii folder in ZELDAOOT)']
        for b, v in zip(self.builds, vals):
            if self.build and os.path.normcase(b['path']) == os.path.normcase(self.build):
                self.build_box.set(v)
                break
        else:
            self.build_box.set(vals[0] if vals else self.build_box['values'][0])

    def _pick_build(self, _ev=None):
        i = self.build_box.current()
        if i < 0 or i >= len(self.builds):
            return
        self.build = self.builds[i]['path']
        self.settings['lichii_dir'] = self.build
        self._save_settings()
        self.data = lichii.data_dir(self.build)
        self.saves = lichii.save_dirs(self.data, self.tid)
        for k, v in (('Lichii data', self.data), ('Save folder', self.saves[0] if self.saves else 'not found yet')):
            e = self.info_rows[k]
            e.configure(state='normal')
            e.delete(0, 'end')
            e.insert(0, v)
            e.configure(state='readonly')
        self.status('Launch build: %s (remembered in user\\settings.json)' % self.build, 'blue')

    def _builds_tick(self, force=False):
        """Every minute: re-read the Lichii Builds tray app's state.json (v0.8.6; no GitHub, no downloads)."""
        if force or not getattr(self, '_builds_busy', False):
            self._builds_busy = True
            self.bg(lambda: checker.read_status(log=self.log), self._builds_done)
        if not force:
            self.root.after(checker.REREAD_EVERY * 1000, self._builds_tick)

    def _builds_done(self, st):
        self._builds_busy = False
        first = not hasattr(self, 'chk_state')
        self.chk_state = st
        colors = {'new': '#ff9f1a', 'ok': '#4caf50', 'error': '#e53935', 'busy': '#2196f3', 'unknown': C['dim']}
        kind, text = checker.summary(st)
        self.builds_ind.configure(text='\u25cf builds: ' + text, foreground=colors.get(kind, C['dim']))
        for src in checker.SOURCES:
            r = st.get(src['name'])
            self.chk_rows[src['name']].configure(text=checker.row_text(src, r),
                                                 foreground='#ff9f1a' if r and r['kind'] == 'new' else C['fg'])
        self.chk_lbl.configure(text=('read %s from %s' % (st['_read'], st['_file'])) +
                               ('   ERROR: ' + st['_error'] if st['_error'] else ''))
        if first:
            self._fill_builds()      # the dropdown labels carry the tray app's tags

    def chk_page(self, src):
        r = (getattr(self, 'chk_state', None) or {}).get(src['name']) or {}
        if not r.get('latest_url'):
            return self.status('No release page in the Lichii Builds state for %s yet.' % src['label'], 'blue')
        import webbrowser
        webbrowser.open(r['latest_url'])
        self.status('Opened %s' % r['latest_url'], 'blue')

    def do_launch(self):
        if not self.build or not self.main:
            return messagebox.showerror('Dev Tool', 'No Lichii build or game dump found.')
        if lichii.lichii_pids():
            return messagebox.showinfo('Dev Tool', 'Lichii is already running. Close the game from its '
                                       'in-game menu and stop Lichii first, then launch from here '
                                       '(a second Lichii instance overwrites its config).')
        self.settings['port'] = int(self.port.get())
        self._save_settings()
        lichii.launch(self.build, self.main, int(self.port.get()))
        self.log('Launched %s with debug server on port %s. Click Connect once in-game.'
                 % (os.path.basename(self.build), self.port.get()))

    def _connect_reads(self):
        dbg = lichii.DebugClient(int(self.port.get()))
        st = dbg.status()
        mods = lichii.read_modules(self.data)
        return dbg, st, mods

    def _connect_writes(self, dbg, mods):
        if not lichii.IS_WIN:
            return None, 'needs Windows'
        if 'main' not in mods:
            return None, 'no "Module main loaded at" line in %s' % lichii.log_path(self.data)
        note = 'lichii.exe is not running'
        for pid in lichii.lichii_pids():
            try:
                h = lichii.HostMemory(pid)
                base, size = mods['main']
                h.calibrate(dbg, [base, base + 0x1000, base + size // 2 & ~0xFFF])
                return h, ''
            except Exception as e:
                note = str(e)
        return None, note

    def do_connect(self):
        self.status('Connecting to the Lichii debug server on port %s...' % self.port.get(), 'blue')
        self.conn_lbl.configure(text='Connecting...', foreground='blue')

        def done_reads(r):
            self.dbg, st, self.modules = r
            self.sess.delete('1.0', 'end')
            self.sess.insert('end', 'Debug server: %s\n\n' % json.dumps(st, indent=1))
            for k, (b, s) in sorted(self.modules.items(), key=lambda x: x[1][0]):
                self.sess.insert('end', 'module %-8s %#014x  size %#x\n' % (k, b, s))
            self.conn_lbl.configure(text='Connected (reads) - finding write access...', foreground='#b06000')
            self.status('Connected to %s (%s fps). Looking for write access...' % (
                st.get('title'), st.get('fps')), 'darkgreen')
            for line in lichii.log_excerpt(self.data):
                self.log('[lichii log] ' + line)
            self._load_watches()
            self.bg(lambda: self._connect_writes(self.dbg, self.modules), done_writes)

        def done_writes(r):
            self.host, note = r
            if self.host:
                self.sess.insert('end', '\nWrites: OK (fastmem arena at host %#x)\n' % self.host.arena)
                self.conn_lbl.configure(text='Connected: reads + writes', foreground='darkgreen')
                self.status('Connected: reads and writes work.', 'darkgreen')
            else:
                self.sess.insert('end', '\nWrites: unavailable - %s\n' % note)
                self.conn_lbl.configure(text='Connected (reads only)', foreground='#b06000')
                self.status('Connected for reads; writes unavailable: %s' % note, '#b06000')
            self.attach_live()

        self.bg(self._connect_reads, done_reads)

    def do_selftest(self):
        def test():
            rep = ['OoT Dev Tool self-test  %s' % time.strftime('%Y-%m-%d %H:%M'),
                   'Lichii build: %s' % self.build, 'Game build ID: %s' % self.bid]

            def ok(name, fn):
                try:
                    r = fn()
                    rep.append('PASS  %s %s' % (name, r if r is not None else ''))
                    return True
                except Exception as e:
                    rep.append('FAIL  %s: %s' % (name, e))
                    return False
            dbg = lichii.DebugClient(int(self.port.get()))
            if not ok('debug server status', lambda: '(%s, build %s)' % (dbg.status().get('title'), dbg.status().get('build'))):
                rep.append('      -> launch Lichii from this tool (debug server env) and load into the game')
                return '\n'.join(rep)
            mods = lichii.read_modules(self.data)
            ok('module bases in log', lambda: ', '.join('%s=%#x' % (k, v[0]) for k, v in mods.items()) or 1 / 0)
            if 'main' in mods:
                base, size = mods['main']
                ok('read_memory main', lambda: dbg.read(base, 16).hex())
                ok('screenshot', lambda: '%dx%d' % dbg.screenshot(height=360)[1:])
                ok('search_memory heap', lambda: '%d hits' % len(dbg.search(b'\0\0\x80\x3f', max_hits=4)[0]))
                if lichii.IS_WIN:
                    def wr():
                        h = lichii.HostMemory(lichii.lichii_pids()[0])
                        h.calibrate(dbg, [base, base + 0x1000])
                        tgt = base + size - 0x100   # end of main's .bss
                        cur = dbg.read(tgt, 8)
                        h.write(tgt, cur)           # same bytes back: proves the path, changes nothing
                        assert dbg.read(tgt, 8) == cur
                        return 'arena %#x' % h.arena
                    ok('host write path (fastmem)', wr)
            if 'main' in mods and self.defs:
                def lg():
                    L = gdlive.LiveGameData(dbg, None, mods['main'][0], self.bid, self.defs).attach()
                    return '%d values in %d types%s' % (len(L.entries), len(L.tables), ''.join(
                        '; %s: %s' % kv for kv in L.problems.items()))
                ok('live GameData', lg)
            ok('save files', lambda: '%d slot folder(s)' % len(lichii.save_dirs(self.data, self.tid)) if lichii.save_dirs(self.data, self.tid) else 1 / 0)
            return '\n'.join(rep)

        def done(rep):
            self.sess.delete('1.0', 'end')
            self.sess.insert('end', rep)
            p = os.path.join(USER, 'selftest_%s.txt' % os.path.basename(self.build or 'none'))
            open(p, 'w').write(rep)
            self.log('Self-test written to ' + p)
        self.bg(test, done)

    def do_pip(self):
        py = sys.executable.replace('pythonw.exe', 'python.exe')
        pkgs = ['zstandard', 'mmh3', 'zbic', 'evfl', 'ainb']
        self.log('Installing %s with pip in a console window; restart the tool afterwards.' % ', '.join(pkgs))
        if lichii.IS_WIN:
            subprocess.Popen(['cmd', '/c', py, '-m', 'pip', 'install', '--upgrade'] + pkgs +
                             ['&', 'echo.', '&', 'echo Done - close this window and restart the dev tool.', '&', 'pause'],
                             creationflags=0x10)
        else:
            subprocess.Popen([py, '-m', 'pip', 'install'] + pkgs)

    def do_names(self):
        py = sys.executable.replace('pythonw.exe', 'python.exe')
        self.log('Rebuilding names.tsv from the romfs in a console window (takes a few minutes)...')
        subprocess.Popen([py, os.path.join(HERE, 'names_build.py'), self.romfs, '--merge'],
                         creationflags=0x10 if lichii.IS_WIN else 0)

    # ============================================================ v0.4 research tools
    def _slot_names(self):
        if not self.saves:
            return []
        return sorted(d for d in os.listdir(self.saves[0])
                      if os.path.isfile(os.path.join(self.saves[0], d, 'progress.sav')))

    def _slot_path(self):
        sl = self.probe_slot.get()
        return os.path.join(self.saves[0], sl, 'progress.sav') if sl and self.saves else None

    def _say(self, text):
        """Progress text from a worker thread: shows in the status line and next to the probe buttons."""
        def show(t):
            self.probe_lbl.configure(text=t)
            self.status_lbl.configure(text=t, foreground='blue')
        self.q.put((show, text, None))

    def _shot_worker(self, label):
        """Takes a screenshot from a worker thread, saves it to user/shots, previews it. Returns the path."""
        png, w, h = self.dbg.screenshot(height=720)
        d = os.path.join(USER, 'shots')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, '%s_%s.png' % (time.strftime('%Y%m%d_%H%M%S'), label))
        open(p, 'wb').write(png)
        self.q.put((self._show_shot, png, None))
        return p

    def _show_shot(self, png):
        import base64
        try:
            img = tk.PhotoImage(data=base64.b64encode(png).decode())
            f = max(1, -(-img.width() // 400))
            if f > 1:
                img = img.subsample(f, f)
            self._shot_img = img
            self.shot_lbl.configure(image=img, text='')
        except Exception as e:
            self.log('screenshot preview failed: %s' % e)

    def do_screenshot(self, label='manual'):
        if not self._need():
            return
        self.bg(lambda: self._shot_worker(label), lambda p: self.status('Screenshot saved: %s' % p, 'darkgreen'))

    def do_probe_prepare(self):
        p = self._slot_path()
        if not p:
            return self.status('No save slot found to prepare.', 'red')
        if not self.defs:
            return self.status('GameData definitions are still loading (or zstandard is missing). Try again in a moment.', 'red')
        if lichii.lichii_pids():
            return messagebox.showwarning(
                'Dev Tool', 'Close the game and Lichii first.\n\nThe game writes its save slots when you quit, so a '
                'slot prepared while Lichii is open gets saved over (that is what happened to the first v0.4 run).\n\n'
                'Quit from the in-game options menu, stop Lichii, then press Prepare again.')
        slot = self.probe_slot.get()
        if not messagebox.askyesno('Dev Tool', 'Prepare %s for the GameData probe?\n\nThis writes test values into '
                                   '18 entries of that slot (minigame records, Deku seeds 17, rupees 77). A backup '
                                   'goes to user\\backups first.' % slot):
            return

        def done(plan):
            self.log('Probe save ready: %s (%d values marked, backup in user\\backups). Plan: user\\%s'
                     % (p, len(plan['entries']), probes.PLAN))
            self.probe_lbl.configure(text='%s prepared' % slot)
            self.status('Probe save ready in %s. Next: Launch, load %s, wait until you can move Link, Connect, '
                        'then "Run v0.4 probes".' % (slot, slot), 'darkgreen')
            if self.save and os.path.normcase(self.save.path) == os.path.normcase(p):
                self.load_slot()
        self.bg(lambda: probes.prepare(p, self.defs, self.names, USER), done)

    def _ram_dump(self, rep=None):
        base, size = self.modules['main']
        d = os.path.join(USER, 'dump')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'main_%s_ram.bin' % self.bid[:16])
        t = time.time()
        missing = mainimg.dump_from_ram(self.dbg, base, size, p,
                                        progress=lambda o, n: self._say('copying main from RAM: %d%%' % (o * 100 // n)))
        json.dump({'base': hex(base), 'size': hex(size), 'build_id': self.bid, 'unreadable_bytes': missing,
                   'taken': time.strftime('%Y-%m-%d %H:%M:%S')},
                  open(p[:-4] + '.json', 'w'), indent=1)
        return p, missing, time.time() - t

    def do_ramdump(self):
        if not self._need():
            return
        if 'main' not in self.modules:
            return self.status('Module base of main unknown (no "Module main loaded at" line in the Lichii log).', 'red')

        def done(r):
            p, missing, secs = r
            self.status('Copied main from RAM to %s in %.0f s%s' % (
                p, secs, (' (%d bytes unreadable)' % missing) if missing else ''), 'darkgreen')
        self.bg(self._ram_dump, done)

    def do_elf(self):
        if not self.main:
            return self.status('No game dump found.', 'red')
        self.status('Decompressing main and writing the Ghidra ELF (about 10 s)...', 'blue')

        def done(r):
            elf, info = r
            self.log('Ghidra ELF: %s (base %s). Load it in Ghidra as ELF; addresses = base + main offset.'
                     % (elf, info['base']))
            self.status('Ghidra ELF written: %s' % elf, 'darkgreen')
        self.bg(lambda: mainimg.build_offline(self.main, HERE, log=lambda m: self.q.put((self.log, m, None))), done)

    def do_catalog(self):
        import catalog
        self.status('Rebuilding the game catalog from your dump (about a minute)...', 'blue')

        def done(r):
            p, summary = r
            self.catalog = None
            self.warp_fill()
            self.probe_lbl.configure(text='')
            self.status(summary, 'darkgreen')
            self.log('Catalog written to ' + p)
        self.bg(lambda: catalog.build(self.romfs, HERE, self.bid, progress=self._say), done)

    def do_probes(self):
        if not self._need():
            return
        plan = probes.load_plan(USER)
        self.status('Running the v0.4 probes. This takes a few minutes; keep the game running and do not pause it.', 'blue')

        def work():
            rep = ['OoT Dev Tool %s probes  %s' % (VERSION, time.strftime('%Y-%m-%d %H:%M:%S')),
                   'Lichii build: %s' % self.build, 'Game build ID: %s' % self.bid,
                   'Modules: %s' % ', '.join('%s=%#x/%#x' % (k, b, n) for k, (b, n) in sorted(self.modules.items())),
                   'Writes: %s' % ('available (fastmem arena %#x)' % self.host.arena if self.host else 'NOT available'), '']

            def step(name, fn):
                try:
                    r = fn()
                    rep.append('PASS  %s %s' % (name, '' if r is None else r))
                    return r
                except Exception as e:
                    rep.append('FAIL  %s: %s' % (name, e))
                    self.q.put((self.log, 'probe step "%s" failed: %s' % (name, traceback.format_exc()[-800:]), None))
                    return None
            self._say('status and screenshot')
            step('status', lambda: json.dumps(self.dbg.status()))
            step('screenshot (start)', lambda: self._shot_worker('probe_start'))
            out = None
            if not plan:
                rep.append('SKIP  GameData probe: no probe save prepared (step 1)')
            elif not self.defs:
                rep.append('SKIP  GameData probe: GameData definitions not loaded (zstandard missing?)')
            else:
                r = step('GameData probe', lambda: probes.run(self.dbg, self.host, plan, self.defs, self.modules,
                                                              USER, say=self._say, shot=self._shot_worker)[0]
                         and 'see user\\probe_gamedata.txt')
                out = json.load(open(os.path.join(USER, 'probe_gamedata.json'))) if r else None
                if out:
                    lay = out.get('layout', {})
                    rep.append('      working copies found for %d of %d entries; layout: %s' % (
                        sum(1 for e in out['entries'] if any(h['kind'] == 'heap' for h in e['hits'])),
                        len(out['entries']), json.dumps(lay)))
                    if 'slot_rewritten' in out:
                        rep.append('      slot file %s since Prepare' % (
                            'CHANGED (saved over)' if out['slot_rewritten'] else 'unchanged'))
                    for im in out.get('images', []):
                        rep.append('      save image in RAM at %#x: %d prepared, %d original, %d other values' % (
                            im['addr'], im['prepared'], im['original'], im['other']))
                    lw = out.get('live_write')
                    if lw:
                        rep.append('      live write (Deku seeds) at %#x: %d -> wrote %d -> %d after 2 s (%s)' % (
                            lw['addr'], lw['was'], lw['wrote'], lw['after_2s'],
                            'STUCK' if lw['after_2s'] == lw['wrote'] else 'reverted'))
                    rep.append('      restored: %d stuck, %d reverted' % (
                        sum(1 for x in out['restore'] if x['stuck']), sum(1 for x in out['restore'] if not x['stuck'])))
            ram = None
            if 'main' in self.modules:
                r = step('copy of main from RAM', lambda: '%s, %d bytes unreadable, %.0f s' % self._ram_dump())
                if r:
                    ram = os.path.join(USER, 'dump', 'main_%s_ram.bin' % self.bid[:16])
            else:
                rep.append('SKIP  copy of main from RAM: module base unknown')
            if ram and out and out.get('pointers'):
                self._say('looking for static pointers in main')
                base = self.modules['main'][0]
                segs = mainimg.header_ranges(self.main)
                blob = open(ram, 'rb').read()
                for typ, d in out['pointers'].items():
                    for target, holders in d.items():
                        for h in holders:
                            h = int(h, 16)
                            if base <= h < base + len(blob):
                                rep.append('      %s table pointer is itself in main: main+%#x' % (typ, h - base))
                                continue
                            for off, v in probes.scan_pointers(blob, base, h - 0x800, h + 8, segs)[:8]:
                                rep.append('      static anchor: main+%#x -> %#x (holder of the %s table pointer at %+#x)'
                                           % (off, v, typ, h - v))
            step('screenshot (end)', lambda: self._shot_worker('probe_end'))
            p = os.path.join(USER, 'probe_v04.txt')
            open(p, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
            return p, rep

        def done(r):
            p, rep = r
            fails = [x for x in rep if x.startswith('FAIL')]
            self.sess.delete('1.0', 'end')
            self.sess.insert('end', '\n'.join(rep))
            self.probe_lbl.configure(text='done')
            self.log('v0.4 probes finished: %s' % p)
            self.status('Probes done (%d failed step(s)). Report: %s' % (len(fails), p), '#b06000' if fails else 'darkgreen')
        self.bg(work, done)

    # ============================================================ live GameData (v0.5)
    INFO = {'banc': 'Sequence_CurrentBanc', 'child': 'Sequence_IsChildhood', 'time': 'World_CurrentDateTime',
            'life': 'PlayerStatus.Life', 'maxlife': 'PlayerStatus.MaxLife', 'rupee': 'PlayerStatus.CurrentRupee',
            'loading': 'Sequence_IsLoadingBanc', 'pos': 'Player_Pos_For_Map'}
    # values the game changes all the time: muted in the flag log from the start (unmute in the tab)
    NOISY = ['World_CurrentDateTime', 'PlayerStatus.CurrentPos', 'PlayerStatus.CurrentDir',
             'PlayerStatus.ReticleAnimationRate', 'MiniGame_TimerValue', 'MiniGame_HorseRace_CrrentTime',
             'Player_Pos_For_Map', 'Player_Rot_For_Map', 'PlayerStatus.SavePos', 'PlayerStatus.SavePosRadY',
             'TimeDayLookWorld']
    # unnamed counters seen ticking every second in the Nov 7 test (play-time style statistics)
    NOISY_HASHES = [0xE573F564, 0xE53EB6D1, 0xCE97845E]

    def attach_live(self, quiet=False):
        """Finds the game's GameData tables (main .bss -> gmd::GameDataMgr -> one table per type)."""
        if not self.dbg or 'main' not in self.modules:
            return None if quiet else self.status('Connect first (and load a save).', 'red')
        if not self.defs:
            self.root.after(1000, lambda: self.attach_live(quiet))
            return

        def work():
            return gdlive.LiveGameData(self.dbg, self.host, self.modules['main'][0], self.bid, self.defs).attach()

        def done(live):
            self.live = loadout.isget_fix(self, live)   # v0.8.1: a StockNum write > 0 also sets IsGet
            self.live_fail = 0
            self.live_snap = None
            if self.frozen:
                self.log('Freezes cleared (new attach): %d' % len(self.frozen))
                self.frozen = {}
                self._freeze_note()
            n = len(live.entries)
            bad = '; '.join('%s: %s' % kv for kv in live.problems.items())
            self.sess.insert('end', '\nLive GameData: manager %#x, %d values in %d types%s\n' % (
                live.mgr, n, len(live.tables), (' (not live: %s)' % bad) if bad else ''))
            self.log('Live GameData attached: %d values (%s). %s' % (
                n, ', '.join('%s %d' % (t, v['count']) for t, v in live.tables.items()),
                'Writes OK.' if self.host else 'Read-only (no write access).'))
            if bad:
                self.log('Live GameData: not live: ' + bad)
            self.flog_start(auto=True)
            self._update_info()
            self.warp_fill()
            if self.src_mode.get() == 'live':
                self.refresh_rows()

        def fail(e):
            self.live = None
            msg = 'Live GameData not available: %s' % e
            self.info_lbl.configure(text=msg, foreground='#ff7b72')
            if not quiet:
                self.log(msg)

        def run():
            try:
                self.q.put((done, work(), None))
            except Exception as e:
                self.q.put((fail, e, None))
        threading.Thread(target=run, daemon=True).start()

    def _live_tick(self):
        """Every 0.5 s (1 s without write access): re-read the tables in the background, then show changes."""
        live = self.live
        if live and not self.live_busy:
            self.live_busy = True

            def work():
                ch = live.snapshot()
                same_mgr = True
                if time.time() - getattr(self, '_mgr_checked', 0) > 5:
                    self._mgr_checked = time.time()
                    same_mgr = live._q(live.main_base + live.build['sinstance']) == live.mgr
                return ch, same_mgr

            def done(r):
                self.live_busy = False
                ch, same_mgr = r
                if live is not self.live:
                    return
                self.live_fail = 0
                if not same_mgr:
                    self.log('GameData manager moved (game restarted?) - attaching again.')
                    self.live = None
                    return self.attach_live(quiet=True)
                if ch:
                    self.flog_add(ch)
                    if self.frozen:
                        self._apply_freezes(ch)
                self._apply_staged()
                self._update_info()
                self._warp_shot_check()
                self._player_refresh()
                self._fw_show()
                if self.src_mode.get() == 'live' and self.auto_ref.get():
                    self._refresh_values()

            def run():
                try:
                    self.q.put((done, work(), None))
                except Exception as e:
                    self.q.put((self._live_error, e, None))
            threading.Thread(target=run, daemon=True).start()
        self.root.after(500 if self.host else 1000, self._live_tick)

    def _apply_freezes(self, changes):
        now = time.time()
        for e, _old, new in changes:
            want = self.frozen.get(e.hash)
            if want is not None and new != want:
                try:
                    self.live.write(e.hash, want)
                    hits = [t for t in self.freeze_hits.get(e.hash, []) if now - t < 10] + [now]
                    self.freeze_hits[e.hash] = hits
                    if len(hits) == 1:       # one row per value per 10 s, so a tug-of-war doesn't flood the log
                        self.flog_tool(e, new, want, 'freeze put it back')
                    if len(hits) >= 6 and e.hash not in self.freeze_hinted:
                        self.freeze_hinted.add(e.hash)
                        if self.names.label(e.hash) in self.LOAD_SYNCED:
                            self.stage(e.hash, want)
                        hint = self.CAPS.get(self.names.label(e.hash), '')
                        self.status('The game resets %s to %s every moment, so the freeze only holds it briefly.%s'
                                    % (self.names.label(e.hash), fmt_val(new), hint), '#b06000')
                except Exception as ex:
                    self.log('freeze %s: %s' % (self.names.label(e.hash), ex))

    def gd_toggle_freeze(self):
        if self.src() is not self.live or not self.live:
            return self.status('Freezing works on the live game: World state -> Live game.', 'red')
        if self.host is None:
            return self.status('No write access, so nothing can be frozen.', 'red')
        for iid in self.tv.selection():
            h = int(iid)
            if h in self.frozen:
                del self.frozen[h]
                self.freeze_hinted.discard(h)
                self.log('unfrozen %s' % self.names.label(h))
            else:
                self.frozen[h] = self.live.read(h)
                self.log('frozen %s at %s (written back within half a second whenever the game changes it)'
                         % (self.names.label(h), self._show(self.live.by_hash[h], self.frozen[h])))
        self._freeze_note()
        self.refresh_rows()

    def gd_unfreeze_all(self):
        self.frozen = {}
        self._freeze_note()
        self.refresh_rows()

    def _freeze_note(self):
        self.freeze_lbl.configure(text=('%d frozen' % len(self.frozen)) if self.frozen else '')

    def _live_error(self, e):
        self.live_busy = False
        self.live_fail += 1
        if self.live_fail >= 4:
            self.live = None
            self.info_lbl.configure(text='Live GameData lost (%s). Connect again once the game is back.' % e,
                                    foreground='#ff7b72')
            self.log('Live GameData lost: %s' % e)

    def _info_val(self, key):
        h = gamedata.mmh(self.INFO[key])
        return self.live.vals.get(h) if self.live else None

    def _update_info(self):
        if not self.live:
            return
        banc = self._info_val('banc')
        child = self._info_val('child')
        life, mx = self._info_val('life'), self._info_val('maxlife')
        parts = ['Map: %s' % (banc or '?'),
                 'Child' if child else ('Adult' if child is not None else 'age ?'),
                 gdlive.fmt_datetime(self._info_val('time'))]
        if isinstance(life, int) and isinstance(mx, int):
            parts.append('Hearts %g/%g' % (life / 4, mx / 4))
        parts.append('Rupees %s' % self._info_val('rupee'))
        pos = self._info_val('pos')
        if isinstance(pos, tuple):
            parts.append('Pos %.1f, %.1f, %.1f' % pos)
        if self._info_val('loading'):
            parts.append('(loading)')
        if getattr(self, 'fps_text', ''):
            parts.append(self.fps_text)
        self.info_lbl.configure(text='   |   '.join(parts), foreground='#8ab4ff')

    # ============================================================ Flag log tab (v0.5)
    def _tab_flaglog(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text='Flag log')
        top = ttk.Frame(f)
        top.pack(fill='x')
        self.flog_btn = ttk.Button(top, text='Stop recording', command=self.flog_toggle)
        self.flog_btn.pack(side='left')
        ttk.Button(top, text='Mark', command=self.flog_add_mark).pack(side='left', padx=4)
        ttk.Button(top, text='Clear', command=self.flog_clear).pack(side='left')
        ttk.Button(top, text='Open log folder', command=lambda: self._open(os.path.join(USER, 'flaglog'))).pack(
            side='left', padx=4)
        ttk.Label(top, text='Show only').pack(side='left', padx=(12, 2))
        self.flog_filter = tk.StringVar()
        ttk.Entry(top, textvariable=self.flog_filter, width=26).pack(side='left')
        self.flog_noisy = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text='mute values that change all the time', variable=self.flog_noisy).pack(
            side='left', padx=8)
        ttk.Button(top, text='Mute selected', command=self.flog_mute_sel).pack(side='left')
        ttk.Button(top, text='Unmute all', command=self.flog_unmute).pack(side='left', padx=4)
        self.flog_lbl = ttk.Label(f, text='Starts by itself once connected. Use Mark before and after an action '
                                  'to bracket what it changes; double-click a row to open it in World state.')
        self.flog_lbl.pack(fill='x', pady=(4, 2))
        cols = ('time', 'name', 'type', 'old', 'new', 'hash')
        tv = ttk.Treeview(f, columns=cols, show='headings')
        for c, w in zip(cols, (80, 460, 80, 200, 200, 80)):
            tv.heading(c, text=c.title())
            tv.column(c, width=w, anchor='w')
        sb = ttk.Scrollbar(f, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tv.pack(fill='both', expand=True)
        tv.tag_configure('mark', background='#5a4a12', foreground='#ffe9a0')
        tv.tag_configure('tool', foreground='#7fd4ff')
        tv.bind('<Double-1>', lambda ev: self.flog_goto())
        self.flog_tv = tv
        self.flog_on = False

    def flog_start(self, auto=False):
        if self.flog_on:
            return
        self.flog_on = True
        d = os.path.join(USER, 'flaglog')
        os.makedirs(d, exist_ok=True)
        self.flog_path = os.path.join(d, 'flaglog_%s.tsv' % time.strftime('%Y%m%d_%H%M%S'))
        try:
            self.flog_file = open(self.flog_path, 'a', encoding='utf-8')
            self.flog_file.write('time\tname\ttype\told\tnew\thash\n')
        except Exception as e:
            self.flog_file = None
            self.log('flag log file: %s' % e)
        self.flog_muted |= {gamedata.mmh(n) for n in self.NOISY} | set(self.NOISY_HASHES)
        self.flog_btn.configure(text='Stop recording')
        self.flog_lbl.configure(text='Recording to user\\flaglog\\%s. Mark before and after an action to bracket '
                                     'what it changes; double-click a row to open it in World state.'
                                     % os.path.basename(self.flog_path))

    def flog_stop(self):
        self.flog_on = False
        if self.flog_file:
            self.flog_file.close()
            self.flog_file = None
        self.flog_btn.configure(text='Start recording')
        self.flog_lbl.configure(text='Stopped.')

    def flog_toggle(self):
        if self.flog_on:
            self.flog_stop()
        elif not self.live:
            self.status('Connect first: the flag log reads the live game.', 'red')
        else:
            self.flog_start()

    def _flog_row(self, vals, tags=()):
        flt = self.flog_filter.get().strip().lower()
        if self.flog_file:
            self.flog_file.write('\t'.join(str(v) for v in vals) + '\n')
            self.flog_file.flush()
        if flt and flt not in str(vals[1]).lower() and 'mark' not in tags:
            return
        self.flog_tv.insert('', 'end', values=vals, tags=tags)
        self.flog_rows += 1
        if self.flog_rows > 5000:
            kids = self.flog_tv.get_children()
            self.flog_tv.delete(*kids[:1000])
            self.flog_rows -= 1000
        self.flog_tv.yview_moveto(1.0)

    def flog_add(self, changes):
        if not self.flog_on:
            return
        now = time.time()
        ts = time.strftime('%H:%M:%S')
        for e, old, new in changes:
            h = e.hash
            watched = h in self.frozen or h in self.staged or now - self.tool_touched.get(h, 0) < 120
            if self.flog_noisy.get() and not watched:
                if h in self.flog_muted:
                    continue
                rec = [t for t in self.flog_recent.get(h, []) if now - t < 60] + [now]
                self.flog_recent[h] = rec
                if sum(1 for t in rec if now - t < 10) >= 6 or len(rec) >= 10:
                    self.flog_muted.add(h)
                    self._flog_row((ts, '(muted from now on: %s changes all the time)' % self.names.label(h),
                                    e.type, '', '', '%08X' % h), ('mark',))
                    continue
            if e.type == 'Enum':
                old, new = self.names.label(old) if old is not None else old, self.names.label(new)
            self._flog_row((ts, self.names.label(h), e.type, fmt_val(old, 60), fmt_val(new, 60), '%08X' % h))

    def flog_tool(self, e, old, new, how):
        """A change the tool made (edit or freeze), shown in its own colour so it isn't mistaken for the game."""
        self.tool_touched[e.hash] = time.time()
        self.flog_muted.discard(e.hash)     # show what the game does with it next
        if not self.flog_on:
            return
        if e.type == 'Enum':
            old, new = self.names.label(old) if old is not None else old, self.names.label(new)
        self._flog_row((time.strftime('%H:%M:%S'), '%s  (%s)' % (self.names.label(e.hash), how), e.type,
                        fmt_val(old, 60), fmt_val(new, 60), '%08X' % e.hash), ('tool',))

    def flog_mute_sel(self):
        n = 0
        for iid in self.flog_tv.selection():
            h = self.flog_tv.item(iid, 'values')[5]
            if h:
                self.flog_muted.add(int(h, 16))
                n += 1
        self.log('Flag log: muted %d value(s). "Unmute all" brings them back.' % n)

    def flog_add_mark(self):
        self.flog_mark += 1
        self._flog_row((time.strftime('%H:%M:%S'), '---------- mark %d ----------' % self.flog_mark, '', '', '', ''),
                       ('mark',))

    def flog_clear(self):
        self.flog_tv.delete(*self.flog_tv.get_children())
        self.flog_rows = 0

    def flog_unmute(self):
        self.flog_muted = set()
        self.flog_recent = {}
        self.log('Flag log: everything unmuted (including the clock).')

    def flog_goto(self):
        """Double-click a logged value: show it in World state (live)."""
        sel = self.flog_tv.selection()
        if not sel:
            return
        h = self.flog_tv.item(sel[0], 'values')[5]
        if not h:
            return
        self.src_mode.set('live')
        self.q_text.set('#' + h)
        self.nb.select(1)
        self.on_source()

    # ============================================================ Warp tab (v0.5.1, experimental)
    WARP_KEYS = ('Sequence_ReservedNextBanc', 'Sequence_ReservedNextStartPosName',
                 'Sequence_ReservedEventStarterName', 'Sequence_ReservedBootEventName')

    def _tab_warp(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='Warp')
        ttk.Label(f, justify='left', wraplength=1250, text=(
            'Arm a warp, then go through any door or exit (or cast Farore\'s Wind): the tool keeps the game\'s '
            '"next map" values (Sequence_ReservedNext*) overwritten, so your map loads instead. It stops when the '
            'map changes and takes a screenshot once it is on screen. A map of the other age switches Link\'s age.'
            )).pack(fill='x')
        body = ttk.Frame(f)
        body.pack(fill='both', expand=True, pady=8)
        left = ttk.Frame(body)
        left.pack(side='left', fill='y')
        ttk.Label(left, text='Map').pack(anchor='w')
        self.warp_filter = tk.StringVar()
        e = ttk.Entry(left, textvariable=self.warp_filter, width=36)
        e.pack(fill='x', pady=2)
        e.bind('<KeyRelease>', lambda ev: self.warp_fill())
        self.warp_age = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="hide the other age's maps", variable=self.warp_age,
                        command=self.warp_fill).pack(anchor='w')
        self.warp_maps = tk.Listbox(left, width=40, height=22, exportselection=False, font=('Consolas', 10))
        self.warp_maps.pack(fill='y', expand=True)
        self.warp_maps.bind('<<ListboxSelect>>', lambda ev: self.warp_pick_map())
        mid = ttk.Frame(body)
        mid.pack(side='left', fill='y', padx=10)
        ttk.Label(mid, text='Start point').pack(anchor='w')
        self.warp_pts = tk.Listbox(mid, width=46, height=24, exportselection=False, font=('Consolas', 10))
        self.warp_pts.pack(fill='y', expand=True)
        self.warp_pts.bind('<<ListboxSelect>>', lambda ev: self.warp_pick_pt())
        right = ttk.Frame(body)
        right.pack(side='left', fill='both', expand=True)
        self.warp_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text='Skip the arrival cutscene (clears ReservedBootEventName)',
                        variable=self.warp_skip).pack(anchor='w')
        wr = ttk.Frame(right)
        wr.pack(anchor='w', fill='x', pady=6)
        self.warp_btn = ttk.Button(wr, text='Arm warp', command=self.warp_toggle)
        self.warp_btn.pack(side='left')
        self.warp_info = ttk.Label(wr, text='', justify='left', foreground=C['dim'], wraplength=400)
        self.warp_info.pack(side='left', padx=10)
        self.warp_lbl = ttk.Label(right, text='Pick a map and a start point.', justify='left', wraplength=560)
        self.warp_lbl.pack(anchor='w', fill='x')
        self.warp_names = []
        fw = ttk.LabelFrame(right, text="Farore's Wind point", padding=6)
        fw.pack(fill='x', pady=(8, 0))
        self.fw_lbl = ttk.Label(fw, text='(connect to see it)', justify='left', wraplength=520)
        self.fw_lbl.pack(anchor='w')
        r1 = ttk.Frame(fw)
        r1.pack(anchor='w', pady=4)
        ttk.Button(r1, text="Set to Link's position", command=self.fw_from_link).pack(side='left')
        ttk.Button(r1, text='Set to the start point picked above', command=self.fw_from_startpos).pack(
            side='left', padx=4)
        ttk.Button(r1, text='Clear', command=self.fw_clear).pack(side='left')
        r2 = ttk.Frame(fw)
        r2.pack(anchor='w')
        ttk.Button(r2, text="Give Farore's Wind", command=self.fw_give).pack(side='left')
        ttk.Label(r2, justify='left', foreground=C['dim'], wraplength=380, text=(
            "Cast it, choose Return: Link goes to the point. Needs magic (Player tab).")).pack(side='left', padx=8)
        bm = ttk.LabelFrame(right, text='Position bookmarks', padding=6)
        bm.pack(fill='both', expand=True, pady=(8, 0))
        r3 = ttk.Frame(bm)
        r3.pack(side='bottom', anchor='w', pady=(4, 0))
        self.bm_list = tk.Listbox(bm, height=4, exportselection=False, font=('Consolas', 9))
        self.bm_list.pack(fill='both', expand=True)
        ttk.Button(r3, text="Bookmark Link's position", command=self.bm_add).pack(side='left')
        ttk.Button(r3, text="Put in Farore's Wind", command=self.bm_to_fw).pack(side='left', padx=4)
        ttk.Button(r3, text='Delete', command=self.bm_del).pack(side='left')
        self.bookmarks = []
        self._bm_load()

    # ---- Farore's Wind point and bookmarks (v0.5.3)
    FW = {'banc': 'WarpPoint_FaroreWind.BancName', 'loc': 'WarpPoint_FaroreWind.LocationName',
          'pos': 'WarpPoint_FaroreWind.Pos', 'yrad': 'WarpPoint_FaroreWind.YRad'}

    def _fw_ok(self):
        if not self.live or self.host is None:
            self.status("Farore's Wind editing needs the live game with write access (Connect).", 'red')
            return False
        if any(gamedata.mmh(n) not in self.live.by_hash for n in self.FW.values()):
            self.status("The WarpPoint_FaroreWind values are not live in this build.", 'red')
            return False
        return True

    def _fw_set(self, banc, loc, pos, yrad):
        old = '%s %s' % (self.live.vals.get(gamedata.mmh(self.FW['banc'])),
                         fmt_val(self.live.vals.get(gamedata.mmh(self.FW['pos']))))
        for key, v in (('banc', banc), ('loc', loc), ('pos', tuple(pos)), ('yrad', float(yrad))):
            h = gamedata.mmh(self.FW[key])
            before = self.live.vals.get(h)
            self.live.write(h, v)
            self.flog_tool(self.live.by_hash[h], before, self.live.vals.get(h), 'set by you')
        self.log("Farore's Wind point: %s -> %s %s (facing %.2f rad)" % (old, banc, fmt_val(tuple(pos)), yrad))
        self._fw_show()

    def _fw_show(self):
        if not self.live or gamedata.mmh(self.FW['banc']) not in self.live.by_hash:
            return
        v = {k: self.live.vals.get(gamedata.mmh(n)) for k, n in self.FW.items()}
        if not v['banc']:
            txt = 'No point set.'
        else:
            txt = '%s  (%s)\nposition %s, facing %.2f rad' % (v['banc'], v['loc'] or '-', fmt_val(v['pos']),
                                                              v['yrad'] or 0)
        have = self.live.vals.get(gamedata.mmh('IsGet.Item_FaroreWind'))
        txt += '\nSpell: %s' % ('owned' if have else 'not owned')
        if self.fw_lbl.cget('text') != txt:
            self.fw_lbl.configure(text=txt)

    def _link_place(self):
        g = lambda n: self.live.vals.get(gamedata.mmh(n))
        return (g('Sequence_CurrentBanc'), g('CaptionData.LocationName') or '', g('Player_Pos_For_Map'),
                g('PlayerStatus.SavePosRadY') or 0.0)

    def fw_from_link(self):
        if not self._fw_ok():
            return
        banc, loc, pos, yrad = self._link_place()
        if not banc or not isinstance(pos, tuple):
            return self.status("Link's map or position is not known yet.", 'red')
        self._fw_set(banc, loc, pos, yrad)

    def fw_from_startpos(self):
        if not self._fw_ok():
            return
        c = self._warp_choice()
        if not c:
            return self.status('Pick a map and a start point in the lists first.', 'red')
        sp = self._load_catalog()['bancs'][c[0]]['startpos'].get(c[1]) or {}
        if not sp.get('pos'):
            return self.status('That start point has no position in the catalog.', 'red')
        rot = sp.get('rot') or [0, 0, 0]
        self._fw_set(c[0], c[0].rsplit('_', 1)[0], sp['pos'], rot[1])

    def fw_clear(self):
        if not self._fw_ok():
            return
        self._fw_set('', '', (0.0, 0.0, 0.0), 0.0)

    def fw_give(self):
        if not self._fw_ok():
            return
        for name, v in (('IsGet.Item_FaroreWind', True), ('PouchData.Item_FaroreWind.StockNum', 1)):
            h = gamedata.mmh(name)
            if h in self.live.by_hash and not self.live.vals.get(h):
                before = self.live.vals.get(h)
                self.live.write(h, v)
                self.flog_tool(self.live.by_hash[h], before, v, 'set by you')
        mp = self.live.vals.get(gamedata.mmh('PlayerStatus.MPMax'))
        self.status("Farore's Wind given (IsGet + pouch). %s Open the item menu to check it is there."
                    % ('No magic meter yet (Max magic is 0): click Give magic meter on the Player tab.'
                       if not mp else ''), 'darkgreen')
        self._fw_show()

    def _bm_path(self):
        return os.path.join(USER, 'bookmarks_%s.json' % self.bid[:16])

    def _bm_load(self):
        try:
            self.bookmarks = json.load(open(self._bm_path(), encoding='utf-8'))
        except Exception:
            self.bookmarks = []
        self.bm_list.delete(0, 'end')
        for b in self.bookmarks:
            self.bm_list.insert('end', '%-22s %s  (%.1f, %.1f, %.1f)' % (b['name'][:22], b['banc'], *b['pos']))

    def bm_add(self):
        if not self.live:
            return self.status('Connect first.', 'red')
        banc, loc, pos, yrad = self._link_place()
        if not banc or not isinstance(pos, tuple):
            return self.status("Link's map or position is not known yet.", 'red')
        name = simpledialog.askstring('Bookmark', 'Name for this spot in %s:' % banc, parent=self.root)
        if name is None:
            return
        self.bookmarks.append({'name': name or banc, 'banc': banc, 'loc': loc, 'pos': list(pos), 'yrad': yrad,
                               'child': bool(self._info_val('child')), 'time': time.strftime('%Y-%m-%d %H:%M')})
        json.dump(self.bookmarks, open(self._bm_path(), 'w', encoding='utf-8'), indent=1)
        self._bm_load()

    def bm_to_fw(self):
        sel = self.bm_list.curselection()
        if not sel or not self._fw_ok():
            return
        b = self.bookmarks[sel[0]]
        self._fw_set(b['banc'], b.get('loc', ''), b['pos'], b.get('yrad', 0.0))

    def bm_del(self):
        sel = self.bm_list.curselection()
        if not sel:
            return
        del self.bookmarks[sel[0]]
        json.dump(self.bookmarks, open(self._bm_path(), 'w', encoding='utf-8'), indent=1)
        self._bm_load()

    def _load_catalog(self):
        if self.catalog is None:
            p = os.path.join(HERE, 'data', 'catalog_%s.json' % self.bid[:16])
            try:
                self.catalog = json.load(open(p, encoding='utf-8'))
            except Exception:
                self.catalog = {}
        return self.catalog

    def warp_fill(self):
        cat = self._load_catalog()
        bancs = cat.get('bancs') or {}
        if not bancs:
            self.warp_lbl.configure(text='No game catalog yet: Session tab -> "Rebuild game catalog" (about a minute).')
        q = self.warp_filter.get().strip().lower()
        child = self._info_val('child') if self.live else None
        other = None if child is None or not self.warp_age.get() else ('_Adult' if child else '_Child')
        self.warp_names = [b for b in sorted(bancs)
                           if (not q or q in b.lower()) and not (other and other in b)]
        self.warp_maps.delete(0, 'end')
        for b in self.warp_names:
            self.warp_maps.insert('end', b)

    def warp_pick_map(self):
        sel = self.warp_maps.curselection()
        self.warp_pts.delete(0, 'end')
        if not sel:
            return
        b = self.warp_names[sel[0]]
        pts = [p for p in sorted((self._load_catalog()['bancs'][b].get('startpos') or {})) if p]
        self.warp_pt_names = pts
        for p in pts:
            self.warp_pts.insert('end', p)
        if pts:
            self.warp_pts.selection_set(0)
            self.warp_pick_pt()

    def _warp_choice(self):
        ms, ps = self.warp_maps.curselection(), self.warp_pts.curselection()
        if not ms or not ps:
            return None
        return self.warp_names[ms[0]], self.warp_pt_names[ps[0]]

    def warp_pick_pt(self):
        c = self._warp_choice()
        if not c:
            return
        sp = self._load_catalog()['bancs'][c[0]]['startpos'].get(c[1]) or {}
        pos = sp.get('pos')
        self.warp_info.configure(text='%s / %s%s' % (c[0], c[1], ('\nposition %.1f, %.1f, %.1f' % tuple(pos))
                                                     if pos else ''))

    def warp_toggle(self):
        if self.warp and self.warp.get('armed'):
            self.warp['armed'] = False
            return
        c = self._warp_choice()
        if not c:
            return self.status('Pick a map and a start point first.', 'red')
        if not self.live or self.host is None:
            return self.status('Warp needs the live game with write access: Session tab -> Connect.', 'red')
        hs = [gamedata.mmh(k) for k in self.WARP_KEYS]
        cur_h = gamedata.mmh('Sequence_CurrentBanc')
        if any(h not in self.live.by_hash for h in hs + [cur_h]):
            return self.status('The Sequence_Reserved* values are not live in this build.', 'red')
        banc, pt = c
        vals = [banc, pt, pt, '' if self.warp_skip.get() else None]
        targets = [(self.live.addr(h), gdlive.encode('String64', v)) for h, v in zip(hs, vals) if v is not None]
        st = {'armed': True, 'banc': banc, 'pt': pt, 'from': self.live.read(cur_h), 't0': time.time(),
              'writes': 0, 'arrived': None, 'error': None}
        self.warp = st
        cur_a = self.live.addr(cur_h)
        host = self.host

        def hammer():
            n = 0
            try:
                while st['armed'] and time.time() - st['t0'] < 300:
                    for a, d in targets:
                        host.write(a, d)
                    n += 1
                    if n % 10 == 0:
                        cur = gdlive.decode('String64', host.read(cur_a, 64))
                        if cur != st['from']:
                            st['arrived'] = cur
                            break
                    time.sleep(0.001)
            except Exception as e:
                st['error'] = str(e)
            st['armed'] = False
            st['writes'] = n
            self.q.put((self._warp_done, st, None))
        threading.Thread(target=hammer, daemon=True).start()
        self.warp_btn.configure(text='Disarm')
        self.warp_lbl.configure(text='ARMED: %s / %s.\nWalk through any door, exit or loading zone. '
                                     '(Disarms by itself after 5 minutes.)' % (banc, pt), foreground=C['warn'])
        self._flog_row((time.strftime('%H:%M:%S'), '---------- warp armed: %s / %s ----------' % (banc, pt),
                        '', '', '', ''), ('mark',))
        self.log('Warp armed: %s / %s (from %s)' % (banc, pt, st['from']))

    def _warp_done(self, st):
        self.warp_btn.configure(text='Arm warp')
        secs = time.time() - st['t0']
        if st['error']:
            msg, col = 'Warp stopped: %s' % st['error'], '#ff7b72'
        elif st['arrived'] is None:
            msg, col = 'Disarmed after %.0f s (%d write rounds), no map change.' % (secs, st['writes']), C['dim']
        elif st['arrived'] == st['banc']:
            msg, col = 'Warped to %s (start point %s) after %.0f s.' % (st['banc'], st['pt'], secs), '#7ee787'
            self.warp_shot = (st['banc'], time.time())
        else:
            msg, col = ('The game loaded %s, not %s: the door\'s own target won. (%d write rounds)'
                        % (st['arrived'], st['banc'], st['writes'])), '#ff7b72'
            self.warp_shot = (st['arrived'], time.time())
        self.warp_lbl.configure(text=msg, foreground=col)
        self._flog_row((time.strftime('%H:%M:%S'), '---------- %s ----------' % msg, '', '', '', ''), ('mark',))
        self.log(msg)

    def _warp_shot_check(self):
        """After a warp: one screenshot once the new map is on screen, so the result can be checked later.
        The screen fades in 2-3 s after loading ends; the game clears Sequence_ReservedNextBanc around then."""
        if not self.warp_shot or not self.dbg:
            return
        banc, t = self.warp_shot[:2]
        now = time.time()
        if now - t > 180:
            self.warp_shot = None
            return
        if self._info_val('loading') or self.live.vals.get(gamedata.mmh('Sequence_ReservedNextBanc')):
            self.warp_shot = (banc, t, None)
            return
        ready = self.warp_shot[2] if len(self.warp_shot) > 2 else None
        if ready is None:
            self.warp_shot = (banc, t, now)
        elif now - ready >= 3:
            self.warp_shot = None
            self.bg(lambda: self._shot_worker('warp_%s' % banc),
                    lambda p: self.log('Warp screenshot: %s' % p))

    # ============================================================ Player tab (v0.5.3)
    PLAYER_VALUES = [('Rupees', 'PlayerStatus.CurrentRupee', 9999),
                     ('Hearts (4 = one heart)', 'PlayerStatus.Life', 80),
                     ('Max hearts (4 = one heart)', 'PlayerStatus.MaxLife', 80),
                     ('Magic (240 = one meter)', 'PlayerStatus.MP', 9999),
                     ('Max magic (240 = one meter)', 'PlayerStatus.MPMax', 9999),
                     ('Heart pieces', 'PouchData.Obj_Heart_Piece.StockNum', 99),
                     ('Deku Sticks', 'PouchData.Item_DekuStick.StockNum', 99),
                     ('Deku Nuts', 'PouchData.Item_DekuNut.StockNum', 99),
                     ('Deku Seeds', 'PouchData.Item_DekuSeed.StockNum', 99),
                     ('Arrows', 'PouchData.Item_Arrow_Normal.StockNum', 99),
                     ('Bombs', 'PouchData.Item_Bomb.StockNum', 99),
                     ('Bombchus', 'PouchData.Item_Bombchu.StockNum', 99),
                     ('Magic Beans', 'PouchData.Item_MagicBean.StockNum', 99)]

    # values the player actor owns: it copies them into GameData every frame and only reads GameData when a map
    # loads (found Nov 7: Magic set during a loading screen stuck, Magic set in play snapped back every frame)
    LOAD_SYNCED = ('PlayerStatus.MP', 'PlayerStatus.Life')

    # The magic meter's size (v0.5.7). Found Nov 7 in the dump, confirmed in-game the same night (v0.5.8):
    #   RSDB/PouchActorInfo: Item_FairyBless_MP.MPMax = 24, Item_FairyBless_MPDouble.MPMax = 48,
    #   Obj_MagicJar_Small.MPRecover = 60, Obj_MagicJar_Large.MPRecover = 120, Obj_FairySpirit.MPRecover = 480.
    #   main+0x11103C8 (the item-get code, ExpansionAvility == MPMaxUP): PlayerStatus.MPMax = MPMax * 10, and
    #   PlayerStatus.MP = the same. So one meter is 240, double is 480; a small jar is a quarter meter and a large
    #   one a half, the classic 12/24-of-48 ratios. The 48/96 the tool used before were classic OoT guesses.
    #   In-game: the bar is 8 segments at 240 and 16 at 480; Magic 60 fills 2 segments; Farore's Wind costs 60.
    MAGIC_SINGLE = 240
    MAGIC_DOUBLE = 480
    MAGIC_NOTE = 'confirmed in-game Nov 7'
    CAPS = {'PlayerStatus.MP': ' Link keeps his own copy of magic and only reads it from GameData when a map loads: '
                               'the tool now holds it for the next loading screen, so go through any door.',
            'PlayerStatus.Life': ' Link keeps his own copy of hearts and only reads it from GameData when a map '
                                 'loads: the tool now holds it for the next loading screen, so go through any door.'}

    def _tab_player(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text='Player')
        ttk.Label(f, justify='left', wraplength=1250, text=(
            'Live values from the running game (Connect first). Type a number and press Enter or Set. Freeze '
            'keeps it there: the tool writes it back within half a second whenever the game changes it. The game '
            'still applies its own limits on its next change (rupees above the wallet size get capped when you buy '
            'something), so freeze anything you want to keep above the normal maximum.')).pack(fill='x')
        g = ttk.Frame(f)
        g.pack(anchor='w', pady=10)
        for col, txt in enumerate(('', 'In game', 'New value', '', 'Freeze')):
            ttk.Label(g, text=txt, foreground=C['dim']).grid(row=0, column=col, sticky='w', padx=6)
        self.pl_rows = {}
        for r, (label, name, top) in enumerate(self.PLAYER_VALUES, start=1):
            h = gamedata.mmh(name)
            ttk.Label(g, text=label).grid(row=r, column=0, sticky='w', padx=6, pady=2)
            cur = ttk.Label(g, text='-', width=18)
            cur.grid(row=r, column=1, sticky='w', padx=6)
            var = tk.StringVar()
            sb = ttk.Spinbox(g, from_=0, to=top, textvariable=var, width=10)
            sb.grid(row=r, column=2, padx=6)
            sb.bind('<Return>', lambda ev, h=h, v=var: self.pl_set(h, v))
            ttk.Button(g, text='Set', width=6, command=lambda h=h, v=var: self.pl_set(h, v)).grid(row=r, column=3)
            fz = tk.BooleanVar(value=False)
            ttk.Checkbutton(g, variable=fz, command=lambda h=h, fz=fz, v=var: self.pl_freeze(h, fz, v)).grid(
                row=r, column=4)
            self.pl_rows[h] = (cur, var, sb, fz, name)
        b = ttk.Frame(f)
        b.pack(anchor='w', pady=6)
        ttk.Button(b, text='Full hearts', command=lambda: self.pl_fill('PlayerStatus.Life', 'PlayerStatus.MaxLife')).pack(
            side='left')
        ttk.Button(b, text='Full magic', command=lambda: self.pl_fill('PlayerStatus.MP', 'PlayerStatus.MPMax')).pack(
            side='left', padx=6)
        ttk.Separator(b, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Button(b, text='Give magic meter', command=lambda: self.pl_give_magic(False)).pack(side='left')
        ttk.Button(b, text='Give double magic', command=lambda: self.pl_give_magic(True)).pack(side='left', padx=6)
        self.pl_lbl = ttk.Label(f, text='', foreground=C['dim'])
        self.pl_lbl.pack(anchor='w')
        self.pl_stage_lbl = ttk.Label(f, text='', justify='left', wraplength=1200)
        self.pl_stage_lbl.pack(anchor='w', pady=(4, 0))

    def _pl_live(self, h):
        if not self.live or h not in self.live.by_hash:
            self.status('Connect first: the Player tab works on the running game.', 'red')
            return False
        if self.host is None:
            self.status('No write access (Connect found no fastmem arena).', 'red')
            return False
        return True

    def pl_set(self, h, var):
        if not self._pl_live(h):
            return
        try:
            v = int(var.get().strip(), 0)
        except ValueError:
            return self.status('Not a number: %r' % var.get(), 'red')
        e = self.live.by_hash[h]
        old = self.live.vals.get(h)
        got = self.live.write(h, v)
        if h in self.frozen:
            self.frozen[h] = got
        self.flog_tool(e, old, got, 'set by you')
        self.pl_lbl.configure(text='%s: %s -> %s' % (self.pl_rows[h][4], old, got))
        self._player_refresh(force=True)

        def check():
            if not self.live or h in self.frozen:
                return
            now = self.live.read(h)
            if now != got:
                if self.pl_rows[h][4] in self.LOAD_SYNCED:
                    self.stage(h, got)
                self.status('%s went back to %s right after the write: the game overrides it.%s'
                            % (self.pl_rows[h][4], now, self.CAPS.get(self.pl_rows[h][4], '')), '#b06000')
                self._player_refresh(force=True)
        self.root.after(1000, check)

    def pl_freeze(self, h, fz, var):
        if not self._pl_live(h):
            fz.set(False)
            return
        if fz.get():
            try:
                v = int(var.get().strip(), 0)
                self.live.write(h, v)
            except ValueError:
                pass
            self.frozen[h] = self.live.read(h)
            self.log('frozen %s at %s' % (self.pl_rows[h][4], self.frozen[h]))
        else:
            self.frozen.pop(h, None)
            self.freeze_hinted.discard(h)
            self.log('unfrozen %s' % self.pl_rows[h][4])
        self._freeze_note()

    def pl_give_magic(self, double):
        """The magic meter is the Great Fairy's blessing: Item_FairyBless_MP (double: ..._MPDouble)."""
        mp, mx = gamedata.mmh('PlayerStatus.MP'), gamedata.mmh('PlayerStatus.MPMax')
        if not self._pl_live(mx) or self.magic_busy:
            return
        self.magic_busy = True
        item = 'Item_FairyBless_MPDouble' if double else 'Item_FairyBless_MP'
        names = ['IsGet.Item_FairyBless_MP', 'PouchData.Item_FairyBless_MP.StockNum']
        if double:
            names += ['IsGet.Item_FairyBless_MPDouble', 'PouchData.Item_FairyBless_MPDouble.StockNum']
        for n in names:
            h = gamedata.mmh(n)
            if h in self.live.by_hash and not self.live.vals.get(h):
                before = self.live.vals.get(h)
                self.live.write(h, True if n.startswith('IsGet') else 1)
                self.flog_tool(self.live.by_hash[h], before, self.live.vals.get(h), 'set by you')
        want = self.MAGIC_DOUBLE if double else self.MAGIC_SINGLE

        def after():
            try:
                after2()
            finally:
                self.magic_busy = False

        def after2():
            got = self.live.read(mx)
            note = 'the game set Max magic to %d itself' % got
            if got < want:
                before = got
                got = self.live.write(mx, want)
                self.flog_tool(self.live.by_hash[mx], before, got, 'set by you')
                note = 'Max magic set to %d (%s)' % (got, self.MAGIC_NOTE)
            self.stage(mp, got)
            self.status('%s given: %s. Now go through any door: Link reads his magic during the loading screen, '
                        'and the tool holds it at %d until then.' % (item, note, got), 'darkgreen')
            self._player_refresh(force=True)
        self.root.after(1500, after)      # give the game a moment to react to the item flags first

    def stage(self, h, value):
        """Hold a value through the next loading screen: Link reads magic and hearts from GameData only then."""
        self.staged[h] = value
        self.stage_state = {}
        self.frozen.pop(h, None)          # no tug-of-war with the player in the meantime
        self._stage_note()

    def _stage_note(self):
        if getattr(self, 'pl_stage_lbl', None) is None:
            return
        if self.staged:
            self.pl_stage_lbl.configure(text='Waiting for a loading screen (go through any door) to apply: %s' % ', '.join(
                '%s = %s' % (self.names.label(h), v) for h, v in self.staged.items()), foreground=C['warn'])
        else:
            self.pl_stage_lbl.configure(text='')

    def _watch_staged(self):
        now = time.time()
        for h, (v, t0) in list(self.stage_watch.items()):
            cur = self.live.vals.get(h)
            if cur != v:
                del self.stage_watch[h]
                msg = '%s held %s for %.0f s after the load, then the game changed it to %s.' % (
                    self.names.label(h), v, now - t0, cur)
                self.status(msg, '#b06000')
                self._flog_row((time.strftime('%H:%M:%S'), '---------- %s ----------' % msg, '', '', '', ''), ('mark',))
            elif now - t0 > 30:
                del self.stage_watch[h]
                self.log('%s still %s 30 s after the load.' % (self.names.label(h), v))

    def _apply_staged(self):
        if self.stage_watch and self.live:
            self._watch_staged()
        if not self.staged or not self.live:
            return
        st = self.stage_state
        if self._info_val('loading'):
            st['seen'], st['end'] = True, None
            for h, v in self.staged.items():
                if self.live.vals.get(h) != v:
                    before = self.live.vals.get(h)
                    self.live.write(h, v)
                    if not st.get('logged_%x' % h):
                        st['logged_%x' % h] = True
                        self.flog_tool(self.live.by_hash[h], before, v, 'held through the loading screen')
        elif st.get('seen'):
            if st.get('end') is None:
                st['end'] = time.time()
            elif time.time() - st['end'] > 2:
                res = [(h, v, self.live.read(h)) for h, v in self.staged.items()]
                self.staged, self.stage_state = {}, {}
                self._stage_note()
                for h, v, now in res:
                    if now == v:
                        self.stage_watch[h] = (v, time.time())
                ok = [self.names.label(h) for h, v, now in res if now == v]
                bad = ['%s (%s, wanted %s)' % (self.names.label(h), now, v) for h, v, now in res if now != v]
                msg = 'After the loading screen: %s%s' % (
                    ('stuck: ' + ', '.join(ok) + ' (watching it for 30 s)') if ok else '',
                    ('; did not stick: ' + ', '.join(bad)) if bad else '')
                self.status(msg, 'darkgreen' if not bad else '#b06000')
                self._flog_row((time.strftime('%H:%M:%S'), '---------- %s ----------' % msg, '', '', '', ''), ('mark',))
                self._player_refresh(force=True)

    def pl_fill(self, name, max_name):
        h, hm = gamedata.mmh(name), gamedata.mmh(max_name)
        if not self._pl_live(h):
            return
        self.pl_rows[h][1].set(str(self.live.read(hm)))
        self.pl_set(h, self.pl_rows[h][1])

    def _player_refresh(self, force=False):
        if not self.live or not getattr(self, 'pl_rows', None):
            return
        try:
            focus = self.root.focus_get()
        except Exception:
            focus = None
        for h, (cur, var, sb, fz, name) in self.pl_rows.items():
            v = self.live.vals.get(h)
            if v is None:
                cur.configure(text='not live')
                continue
            extra = ''
            if name in ('PlayerStatus.Life', 'PlayerStatus.MaxLife'):
                extra = '  (%g hearts)' % (v / 4)
            cur.configure(text='%s%s%s' % (v, extra, '  frozen' if h in self.frozen else ''))
            fz.set(h in self.frozen)
            if force or focus is not sb:
                var.set(str(v))

    # ============================================================ World state tab
    def _tab_world(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text='World state')
        top = ttk.Frame(f)
        top.pack(fill='x')
        self.src_mode = tk.StringVar(value='save')
        ttk.Radiobutton(top, text='Save file', value='save', variable=self.src_mode,
                        command=self.on_source).pack(side='left')
        ttk.Radiobutton(top, text='Live game', value='live', variable=self.src_mode,
                        command=self.on_source).pack(side='left', padx=(4, 10))
        self.auto_ref = tk.BooleanVar(value=True)
        self.auto_ref_cb = ttk.Checkbutton(top, text='auto-refresh', variable=self.auto_ref)
        ttk.Label(top, text='Save').pack(side='left')
        self.slot = ttk.Combobox(top, width=34, state='readonly')
        self.slot.pack(side='left', padx=4)
        self.slot.bind('<<ComboboxSelected>>', lambda e: self.load_slot())
        ttk.Button(top, text='Open file...', command=self.open_save_file).pack(side='left')
        ttk.Button(top, text='Reload', command=self.do_reload).pack(side='left', padx=4)
        self.save_btn = ttk.Button(top, text='Save changes', command=self.save_slot)
        self.save_btn.pack(side='left', padx=4)
        tools = ttk.Frame(f)
        tools.pack(fill='x', pady=(4, 0))
        ttk.Button(tools, text='Snapshot', command=self.take_snapshot).pack(side='left')
        ttk.Button(tools, text='Diff vs snapshot', command=self.show_diff).pack(side='left', padx=4)
        ttk.Separator(tools, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Button(tools, text='Label hash...', command=self.label_hash).pack(side='left', padx=4)
        ttk.Button(tools, text='Try names...', command=self.try_names).pack(side='left')
        ttk.Separator(tools, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Button(tools, text='Slot snapshots...', command=self.snap_dialog).pack(side='left')
        ttk.Separator(tools, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Button(tools, text='Freeze / unfreeze selected', command=self.gd_toggle_freeze).pack(side='left')
        ttk.Button(tools, text='Unfreeze all', command=self.gd_unfreeze_all).pack(side='left', padx=4)
        self.freeze_lbl = ttk.Label(tools, text='')
        self.freeze_lbl.pack(side='left', padx=6)
        flt = ttk.Frame(f)
        flt.pack(fill='x', pady=4)
        ttk.Label(flt, text='Search').pack(side='left')
        self.q_text = tk.StringVar()
        e = ttk.Entry(flt, textvariable=self.q_text, width=40)
        e.pack(side='left', padx=4)
        e.bind('<Return>', lambda ev: self.refresh_rows())
        ttk.Label(flt, text='Type').pack(side='left')
        self.q_type = ttk.Combobox(flt, width=14, state='readonly', values=['(all)'] + gamedata.SAVE_TYPES)
        self.q_type.set('(all)')
        self.q_type.pack(side='left', padx=4)
        self.q_type.bind('<<ComboboxSelected>>', lambda ev: self.refresh_rows())
        self.q_named = tk.BooleanVar(value=False)
        self.q_changed = tk.BooleanVar(value=False)
        self.q_diff = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt, text='named only', variable=self.q_named, command=self.refresh_rows).pack(side='left')
        ttk.Checkbutton(flt, text='not default', variable=self.q_changed, command=self.refresh_rows).pack(side='left')
        ttk.Checkbutton(flt, text='changed since snapshot', variable=self.q_diff, command=self.refresh_rows).pack(side='left')
        ttk.Button(flt, text='Filter', command=self.refresh_rows).pack(side='left', padx=6)
        self.count_lbl = ttk.Label(flt, text='')
        self.count_lbl.pack(side='left', padx=8)
        cols = ('name', 'type', 'value', 'default', 'hash')
        tv = ttk.Treeview(f, columns=cols, show='headings')
        for c, w in zip(cols, (420, 110, 300, 140, 90)):
            tv.heading(c, text=c.title())
            tv.column(c, width=w, anchor='w')
        sb = ttk.Scrollbar(f, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tv.pack(fill='both', expand=True)
        tv.bind('<Double-1>', lambda ev: self.edit_row())
        tv.tag_configure('dirty', foreground=C['warn'])
        tv.tag_configure('frozen', foreground='#7fd4ff')
        self.tv = tv
        self.edited = set()

    def src(self):
        """What the World state tab shows: the save file, or the live game (same interface)."""
        return self.live if self.src_mode.get() == 'live' else self.save

    def on_source(self):
        live = self.src_mode.get() == 'live'
        if live and not self.live:
            self.src_mode.set('save')
            return self.status('Live game needs a connection: Session tab -> Connect (with a save loaded).', 'red')
        if live:
            self.auto_ref_cb.pack(side='left', after=self.save_btn, padx=4)
            self.save_btn.state(['disabled'])
            self.slot.state(['disabled'])
            self.status('World state shows the running game. Double-click a value to change it in the game '
                        'right away (Bool, numbers, Enum, Vector3, text).', 'darkgreen')
        else:
            self.auto_ref_cb.pack_forget()
            self.save_btn.state(['!disabled'])
            self.slot.state(['!disabled'])
        self.refresh_rows()

    def do_reload(self):
        if self.src_mode.get() == 'live':
            if self.live:
                self.bg(self.live.snapshot, lambda ch: self.refresh_rows())
        else:
            self.load_slot()

    def _refresh_values(self):
        """Live mode: update the Value column of the rows on screen, keeping scroll position and selection."""
        if not self.live:
            return
        for iid in self.tv.get_children():
            e = self.live.by_hash.get(int(iid))
            if e is None:
                continue
            new = self._show(e, self.live.vals.get(e.hash))
            if self.tv.set(iid, 'value') != new:
                self.tv.set(iid, 'value', new)

    # ---- slot snapshots (v0.5.3)
    def _snap_root(self):
        d = os.path.join(USER, 'slot_snapshots')
        os.makedirs(d, exist_ok=True)
        return d

    def _snap_list(self):
        out = []
        for n in sorted(os.listdir(self._snap_root()), reverse=True):
            p = os.path.join(self._snap_root(), n)
            if os.path.isfile(os.path.join(p, 'progress.sav')):
                try:
                    meta = json.load(open(os.path.join(p, 'snapshot.json'), encoding='utf-8'))
                except Exception:
                    meta = {}
                out.append((n, p, meta))
        return out

    def snap_dialog(self):
        import shutil
        if not self.saves:
            return self.status('No save folder found.', 'red')
        win = tk.Toplevel(self.root)
        win.title('Slot snapshots')
        win.transient(self.root)
        ttk.Label(win, justify='left', wraplength=760, text=(
            'Keep named copies of a save slot (progress.sav + caption.sav) and put them back later. The game only '
            'reads a slot when you load it and writes it when it saves or you quit, so restore with the game '
            'closed or at the title screen, then load the slot. Restoring backs the slot up to user\\backups '
            'first.')).pack(fill='x', padx=10, pady=8)
        top = ttk.Frame(win)
        top.pack(fill='x', padx=10)
        ttk.Label(top, text='Slot').pack(side='left')
        slot = ttk.Combobox(top, values=self._slot_names(), width=12, state='readonly')
        slot.pack(side='left', padx=4)
        if self._slot_names():
            slot.set(self._slot_names()[0])
        name = tk.StringVar()
        ttk.Label(top, text='Name').pack(side='left', padx=(10, 2))
        ttk.Entry(top, textvariable=name, width=30).pack(side='left')
        lb = tk.Listbox(win, width=100, height=14, exportselection=False, font=('Consolas', 9))
        lb.pack(fill='both', expand=True, padx=10, pady=8)
        items = []

        def fill():
            items[:] = self._snap_list()
            lb.delete(0, 'end')
            for n, p, m in items:
                lb.insert('end', '%-40s from %-8s %s' % (m.get('name', n)[:40], m.get('slot', '?'), m.get('time', '')))

        def slot_dir():
            return os.path.join(self.saves[0], slot.get())

        def keep():
            src = slot_dir()
            nm = name.get().strip() or slot.get()
            safe = ''.join(ch if ch.isalnum() or ch in '-_ ' else '_' for ch in nm).strip() or 'snapshot'
            dst = os.path.join(self._snap_root(), '%s_%s' % (time.strftime('%Y%m%d_%H%M%S'), safe))
            shutil.copytree(src, dst)
            json.dump({'name': nm, 'slot': slot.get(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'build_id': self.bid}, open(os.path.join(dst, 'snapshot.json'), 'w', encoding='utf-8'),
                      indent=1)
            self.log('Slot snapshot "%s" kept from %s.' % (nm, slot.get()))
            fill()

        def chosen():
            sel = lb.curselection()
            return items[sel[0]] if sel else None

        def restore():
            c = chosen()
            if not c:
                return
            n, p, m = c
            msg = 'Put snapshot "%s" into %s?' % (m.get('name', n), slot.get())
            if lichii.lichii_pids():
                msg += ('\n\nLichii is running: do this at the title screen, then load the slot. If you are '
                        'in-game, quitting will save over it.')
            if not messagebox.askyesno('Slot snapshots', msg, parent=win):
                return
            dst = slot_dir()
            bak = os.path.join(USER, 'backups', '%s_%s_before_restore' % (time.strftime('%Y%m%d_%H%M%S'), slot.get()))
            if os.path.isdir(dst):
                shutil.copytree(dst, bak)
            os.makedirs(dst, exist_ok=True)
            for fn in os.listdir(p):
                if fn.endswith('.sav'):
                    shutil.copy2(os.path.join(p, fn), os.path.join(dst, fn))
            self.log('Restored snapshot "%s" into %s (previous files in %s).' % (m.get('name', n), slot.get(), bak))
            if self.save and os.path.normcase(os.path.dirname(self.save.path)) == os.path.normcase(dst):
                self.load_slot()

        def compare():
            c = chosen()
            if not c:
                return
            n, p, m = c
            a = savefile.SaveFile(os.path.join(p, 'progress.sav'))
            b = savefile.SaveFile(os.path.join(slot_dir(), 'progress.sav'))
            ch = savefile.diff(a, b)
            w2 = tk.Toplevel(win)
            w2.title('%d differences: snapshot "%s" -> %s now' % (len(ch), m.get('name', n), slot.get()))
            t = tk.Text(w2, width=130, height=40, font=('Consolas', 9))
            t.pack(fill='both', expand=True)
            for h, typ, x, y in sorted(ch, key=lambda r: self.names.label(r[0]).lower()):
                if typ == 'Enum':
                    x, y = self.names.label(x), self.names.label(y)
                t.insert('end', '%-60s %-12s %s -> %s\n' % (self.names.label(h), typ, fmt_val(x, 60), fmt_val(y, 60)))

        def delete():
            c = chosen()
            if not c or not messagebox.askyesno('Slot snapshots', 'Delete snapshot "%s"?' % c[2].get('name', c[0]),
                                                parent=win):
                return
            shutil.rmtree(c[1])
            fill()

        bb = ttk.Frame(win)
        bb.pack(fill='x', padx=10, pady=(0, 10))
        ttk.Button(bb, text='Keep a copy of the slot', command=keep).pack(side='left')
        ttk.Button(bb, text='Restore into the slot', command=restore).pack(side='left', padx=4)
        ttk.Button(bb, text='Compare with the slot', command=compare).pack(side='left')
        ttk.Button(bb, text='Delete', command=delete).pack(side='left', padx=4)
        fill()
        self._snap_win = (win, slot, name, lb, keep, restore, compare)

    def _reload_slots(self):
        self.saves = lichii.save_dirs(self.data, self.tid)
        items = []
        for d in self.saves:
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if fn.endswith('.sav'):
                        p = os.path.join(root, fn)
                        try:
                            with open(p, 'rb') as fh:
                                if struct.unpack('<I', fh.read(4))[0] == savefile.MAGIC:
                                    items.append(os.path.relpath(p, d))
                        except Exception:
                            pass
        self.slot_paths = {i: os.path.join(self.saves[0], i) for i in sorted(items)} if self.saves else {}
        self.slot['values'] = list(self.slot_paths)
        if items and not self.slot.get():
            pref = [i for i in self.slot_paths if 'slot_00' in i and 'progress' in i]
            self.slot.set(pref[0] if pref else list(self.slot_paths)[0])
            self.load_slot()

    def open_save_file(self):
        p = filedialog.askopenfilename(filetypes=[('GameData save', '*.sav'), ('All', '*.*')])
        if p:
            self.slot_paths[p] = p
            self.slot['values'] = list(self.slot_paths)
            self.slot.set(p)
            self.load_slot()

    def load_slot(self):
        p = self.slot_paths.get(self.slot.get())
        if not p:
            return
        try:
            self.save = loadout.isget_fix(self, savefile.SaveFile(p))   # v0.8.1 IsGet fix
        except Exception as e:
            return self.log('cannot read %s: %s' % (p, e))
        self.edited = set()
        if self.save.revision != self.meta.get('revision', self.save.revision):
            self.log('note: save revision %d' % self.save.revision)
        self.log('Loaded %s: %d entries (revision %d)' % (p, len(self.save.entries), self.save.revision))
        self.refresh_rows()

    def _rows(self):
        s = self.src()
        q = self.q_text.get().strip().lower()
        qh = None
        if q.startswith('#') or q.startswith('0x'):
            try:
                qh = int(q.lstrip('#'), 16)
            except ValueError:
                pass
        typ = self.q_type.get()
        diffset = None
        if self.q_diff.get() and s is self.live and self.live_snap is not None:
            diffset = {h for h, v in s.vals.items() if self.live_snap.get(h) != v}
        elif self.q_diff.get() and s is self.save and self.snapshot is not None:
            diffset = {h for h, *_ in savefile.diff(self.snapshot, s)}
        for e in s.entries:
            if typ != '(all)' and e.type != typ:
                continue
            if diffset is not None and e.hash not in diffset:
                continue
            name = self.names.get(e.hash)
            if self.q_named.get() and not name:
                continue
            if qh is not None:
                if e.hash != qh:
                    continue
            elif q and (not name or q not in name.lower()):
                continue
            v = s.get(e)
            d = self.defs.get(e.hash)
            dv = d.default if d else None
            if self.q_changed.get() and d is not None:
                if e.type == 'Vector3':
                    if s is self.live and v == s.default(e.hash):
                        continue
                elif e.type in ('Int64', 'UInt64'):
                    if v == dv:
                        continue
                elif e.type.startswith(('String', 'WString')):
                    if (v or '') == (dv or ''):
                        continue
            if self.q_changed.get() and e.type in savefile.INLINE and d is not None:
                if e.type == 'Enum' or e.type == 'Bool' or e.type == 'Int' or e.type == 'UInt':
                    if v == dv or (e.type == 'Bool' and bool(v) == bool(dv)):
                        continue
                elif e.type == 'Float' and abs(v - (dv or 0)) < 1e-6:
                    continue
            yield e, name, v, dv

    def _show(self, e, v):
        if e.type == 'Enum' or e.type == 'EnumArray':
            if isinstance(v, list):
                return fmt_val([self.names.label(x) for x in v])
            return self.names.label(v)
        return fmt_val(v)

    def refresh_rows(self):
        if not self.src():
            return
        self.tv.delete(*self.tv.get_children())
        n = 0
        s_is_live = self.src() is self.live
        for e, name, v, dv in self._rows():
            n += 1
            if n <= MAX_ROWS:
                dshow = self.names.label(dv) if e.type == 'Enum' and isinstance(dv, int) else fmt_val(dv, 30)
                self.tv.insert('', 'end', iid=str(e.hash), values=(
                    name or '#%08X' % e.hash, e.type, self._show(e, v), dshow, '%08X' % e.hash),
                    tags=('frozen',) if (e.hash in self.frozen and s_is_live) else
                    (('dirty',) if e.hash in self.edited else ()))
        self.count_lbl.configure(text='%d match%s%s' % (n, '' if n == 1 else 'es',
                                                         ' (showing %d)' % MAX_ROWS if n > MAX_ROWS else ''))

    def edit_row(self):
        sel = self.tv.selection()
        src = self.src()
        if not sel or not src:
            return
        e = src.by_hash[int(sel[0])]
        if e.type in savefile.READ_ONLY:
            return messagebox.showinfo('Dev Tool', '%s values are read-only in v%s.' % (e.type, VERSION))
        if src is self.live:
            v = src.read(e.hash)     # fresh value, not the last poll
            before = v
            if self.host is None:
                return messagebox.showinfo('Dev Tool', 'No write access (Connect found no fastmem arena), '
                                           'so live values are read-only.')
        else:
            v = src.get(e)
        d = self.defs.get(e.hash)
        name = self.names.label(e.hash)
        win = tk.Toplevel(self.root)
        win.title('Edit ' + name)
        win.transient(self.root)
        ttk.Label(win, text='%s  (%s, hash %08X)' % (name, e.type, e.hash)).pack(padx=10, pady=6, anchor='w')
        apply = None
        if e.type == 'Bool':
            var = tk.BooleanVar(value=v)
            ttk.Checkbutton(win, text='true', variable=var).pack(padx=10)
            apply = lambda: src.set(e, var.get())
        elif e.type == 'Enum' and d and d.raw_values:
            cb = ttk.Combobox(win, values=d.raw_values, width=50, state='readonly')
            cur = d.values.index(v) if v in d.values else 0
            cb.current(cur)
            cb.pack(padx=10)
            apply = lambda: src.set(e, d.values[cb.current()])
        elif e.is_array:
            ttk.Label(win, text='One element per line (%d elements). Vectors as x, y, z.' % len(v)).pack(padx=10, anchor='w')
            txt = tk.Text(win, width=70, height=min(25, max(5, len(v))), font=('Consolas', 9))
            enum_vals = d.raw_values if (e.base == 'Enum' and d and d.raw_values) else None
            for x in v:
                if enum_vals and x in d.values:
                    x = d.raw_values[d.values.index(x)]
                elif isinstance(x, tuple):
                    x = ', '.join('%.6g' % c for c in x)
                elif isinstance(x, float):
                    x = '%.6g' % x
                txt.insert('end', '%s\n' % x)
            txt.pack(padx=10)

            def apply():
                lines = txt.get('1.0', 'end').rstrip('\n').split('\n')
                if len(lines) != len(v):
                    raise ValueError('need exactly %d lines, got %d' % (len(v), len(lines)))
                new = [self._parse(e.base, ln, d) for ln in lines]
                src.set(e, new)
        else:
            ent = ttk.Entry(win, width=60)
            ent.insert(0, ', '.join('%.6g' % c for c in v) if isinstance(v, tuple) else
                       ('%.6g' % v if isinstance(v, float) else str(v)))
            ent.pack(padx=10)
            apply = lambda: src.set(e, self._parse(e.base, ent.get(), d))

        def ok():
            try:
                apply()
            except Exception as ex:
                return messagebox.showerror('Dev Tool', str(ex), parent=win)
            self.edited.add(e.hash)
            win.destroy()
            if src is self.live:
                if e.hash in self.frozen:
                    self.frozen[e.hash] = src.vals.get(e.hash)
                self.flog_tool(e, before, src.vals.get(e.hash), 'set by you')
                self.log('set %s = %s in the game (read back: %s)' % (
                    name, self._show(e, src.vals.get(e.hash)), self._show(e, src.read(e.hash))))
                self._refresh_values()
            else:
                self.refresh_rows()
                self.log('edited %s (not written yet - click Save changes)' % name)
        ttk.Button(win, text='OK', command=ok).pack(pady=8)

    @staticmethod
    def _parse(base, text, d=None):
        t = text.strip()
        if base == 'Bool':
            return t.lower() in ('1', 'true', 'yes', 'on')
        if base in ('Int', 'Int64'):
            return int(t, 0)
        if base in ('UInt', 'UInt64'):
            return int(t, 0)
        if base == 'Float':
            return float(t)
        if base == 'Enum':
            if d and d.raw_values and t in d.raw_values:
                return d.values[d.raw_values.index(t)]
            return int(t, 0) if t[:1].isdigit() else gamedata.mmh(t)
        if base in ('Vector2', 'Vector3'):
            return tuple(float(x) for x in t.replace('(', '').replace(')', '').split(','))
        return t  # strings

    def save_slot(self):
        if not self.save or not self.save.dirty:
            return self.log('nothing to save')
        if lichii.lichii_pids():
            if not messagebox.askyesno('Dev Tool', 'Lichii is running. The game overwrites this file '
                                       'whenever it saves, and only reads it when you load the slot.\n\n'
                                       'Best: return to the title screen, write, then load the file.\n\nWrite now?'):
                return
        self.save.save(backup_root=os.path.join(USER, 'backups'))
        self.log('Wrote %s (%d edits). Backup in user\\backups.' % (self.save.path, len(self.edited)))
        self.edited = set()
        self.refresh_rows()

    def take_snapshot(self):
        if self.src_mode.get() == 'live':
            if self.live:
                self.live_snap = dict(self.live.vals)
                self.log('Live snapshot taken (%d values). Do something in the game, then "Diff vs snapshot" '
                         'or tick "changed since snapshot".' % len(self.live_snap))
            return
        if not self.save:
            return
        self.snapshot = savefile.SaveFile(self.save.path)
        d = os.path.join(USER, 'snapshots')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, '%s_%s.sav' % (time.strftime('%Y%m%d_%H%M%S'),
                                         os.path.basename(os.path.dirname(self.save.path))))
        open(p, 'wb').write(self.snapshot.data)
        self.log('Snapshot taken. Play, let the game save, click Reload, then "Diff vs snapshot".')

    def show_diff(self):
        if self.src_mode.get() == 'live':
            return self._show_live_diff()
        if not self.save or self.snapshot is None:
            return self.log('take a snapshot first')
        cur = savefile.SaveFile(self.save.path)
        ch = savefile.diff(self.snapshot, cur)
        win = tk.Toplevel(self.root)
        win.title('%d values changed since snapshot' % len(ch))
        t = tk.Text(win, width=130, height=40, font=('Consolas', 9))
        t.pack(fill='both', expand=True)
        for h, typ, a, b in sorted(ch, key=lambda x: self.names.label(x[0]).lower()):
            if typ in ('Enum',):
                a, b = self.names.label(a), self.names.label(b)
            elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                idx = [i for i in range(len(a)) if a[i] != b[i]]
                a = {i: a[i] for i in idx[:12]}
                b = {i: b[i] for i in idx[:12]}
            t.insert('end', '%-60s %-12s %s -> %s\n' % (self.names.label(h), typ, fmt_val(a, 60), fmt_val(b, 60)))
        self.save = cur
        self.refresh_rows()

    def _show_live_diff(self):
        if not self.live or self.live_snap is None:
            return self.log('take a snapshot first')
        ch = [(h, v) for h, v in self.live.vals.items() if self.live_snap.get(h) != v]
        win = tk.Toplevel(self.root)
        win.title('%d live values changed since snapshot' % len(ch))
        t = tk.Text(win, width=130, height=40, font=('Consolas', 9))
        t.pack(fill='both', expand=True)
        for h, v in sorted(ch, key=lambda x: self.names.label(x[0]).lower()):
            e = self.live.by_hash[h]
            t.insert('end', '%-60s %-10s %s -> %s\n' % (self.names.label(h), e.type,
                     self._show(e, self.live_snap.get(h)), self._show(e, v)))
        self.refresh_rows()

    def label_hash(self):
        sel = self.tv.selection()
        h = int(sel[0]) if sel else None
        if h is None:
            s = simpledialog.askstring('Label hash', 'Hash (hex):')
            if not s:
                return
            h = int(s.lstrip('#'), 16)
        name = simpledialog.askstring('Label hash', 'Label for %08X (your own note, e.g. "got slingshot?"):' % h)
        if name:
            self.names.add_user(name, h)
            self.refresh_rows()

    def try_names(self):
        win = tk.Toplevel(self.root)
        win.title('Try names - paste candidate flag names, one per line')
        t = tk.Text(win, width=80, height=25)
        t.pack(fill='both', expand=True)

        def go():
            known = set(self.defs)
            hits = self.names.try_names(t.get('1.0', 'end').splitlines(), known)
            messagebox.showinfo('Dev Tool', '%d new names matched:\n%s' % (len(hits), '\n'.join(list(hits.values())[:40])), parent=win)
            self.refresh_rows()
        ttk.Button(win, text='Hash and match', command=go).pack(pady=6)

    # ============================================================ Live tab
    def _tab_live(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text='Live memory')
        right = ttk.Frame(f)
        right.pack(side='right', fill='y', padx=(8, 0))
        left = ttk.Frame(f)
        left.pack(side='left', fill='both', expand=True)

        rd = ttk.LabelFrame(left, text='Read / write  (addresses: 0x..., main+0x..., heap hits)', padding=6)
        rd.pack(fill='x')
        self.addr = tk.StringVar(value='main+0x0')
        self.rsize = tk.StringVar(value='256')
        ttk.Entry(rd, textvariable=self.addr, width=28).grid(row=0, column=0)
        ttk.Entry(rd, textvariable=self.rsize, width=8).grid(row=0, column=1, padx=4)
        ttk.Button(rd, text='Read', command=self.do_read).grid(row=0, column=2)
        wrow = ttk.Frame(rd)
        wrow.grid(row=1, column=0, columnspan=4, sticky='w', pady=(4, 0))
        ttk.Label(wrow, text='Write').pack(side='left')
        self.wtype = ttk.Combobox(wrow, values=TYPES, width=7, state='readonly')
        self.wtype.set('u32')
        self.wtype.pack(side='left', padx=4)
        self.wval = tk.StringVar()
        ttk.Entry(wrow, textvariable=self.wval, width=24).pack(side='left')
        ttk.Button(wrow, text='Write', command=self.do_write).pack(side='left', padx=4)
        ttk.Button(rd, text='+ Watch', command=self.add_watch_here).grid(row=0, column=3, padx=4)
        self.hexv = tk.Text(left, height=10, font=('Consolas', 9))
        self.hexv.pack(fill='both', expand=True, pady=4)

        sc = ttk.LabelFrame(left, text='Scan (heap)', padding=6)
        sc.pack(fill='x')
        self.stype = ttk.Combobox(sc, values=['u32', 'i32', 'f32', 'u16', 'u8', 'u64', 'hex', 'string', 'flag name/hash'], width=13, state='readonly')
        self.stype.set('u32')
        self.stype.grid(row=0, column=0)
        self.sval = tk.StringVar()
        ttk.Entry(sc, textvariable=self.sval, width=22).grid(row=0, column=1, padx=4)
        self.sregion = ttk.Combobox(sc, values=['heap', 'all'], width=5, state='readonly')
        self.sregion.set('heap')
        self.sregion.grid(row=0, column=2)
        ttk.Button(sc, text='New scan', command=self.do_scan).grid(row=0, column=3, padx=4)
        ttk.Button(sc, text='Next scan', command=self.do_rescan).grid(row=0, column=4)
        self.hitlist = tk.Listbox(sc, height=8, font=('Consolas', 9), width=50)
        self.hitlist.grid(row=1, column=0, columnspan=5, sticky='we', pady=4)
        self.hitlist.bind('<<ListboxSelect>>', self._pick_hit)

        pad = ttk.LabelFrame(left, text='Controller (injected into player 1 via the debug server)', padding=6)
        pad.pack(fill='x', pady=(4, 0))
        for b in ('A', 'B', 'X', 'Y', 'L', 'R', 'ZL', 'ZR', 'Plus', 'Minus', 'Up', 'Down', 'Left', 'Right'):
            ttk.Button(pad, text=b, width=5, style='Pad.TButton', command=lambda b=b: self.press(b)).pack(side='left', padx=1)

        wf = ttk.LabelFrame(right, text='Watches (refresh 0.5 s; freeze rewrites)', padding=6)
        wf.pack(fill='both', expand=True)
        cols = ('label', 'addr', 'type', 'value', 'freeze')
        wt = ttk.Treeview(wf, columns=cols, show='headings', height=24)
        for c in cols:
            wt.column(c, stretch=False)
        for c, w in zip(cols, (95, 105, 40, 85, 45)):
            wt.heading(c, text=c.title())
            wt.column(c, width=w, anchor='w')
        wt.pack(fill='both', expand=True)
        wt.bind('<Double-1>', lambda ev: self.edit_watch())
        self.wt = wt
        b = ttk.Frame(wf)
        b.pack(fill='x')
        ttk.Button(b, text='Remove', command=self.del_watch).pack(side='left')
        ttk.Button(b, text='Toggle freeze', command=self.toggle_freeze).pack(side='left', padx=4)
        ttk.Button(b, text='Set value...', command=self.edit_watch).pack(side='left')

    def press(self, button, frames=6):
        if not self._need():
            return
        self.bg(lambda: self.dbg.input([{'buttons': [button], 'frames': frames}, {'frames': 4}]),
                lambda r: None)

    def _need(self, write=False):
        if not self.dbg:
            self.status('Not connected: load into the game, then click Connect on the Session tab.', 'red')
            return False
        if write and not self.host:
            self.status('Writes are unavailable (see the Session tab). Reads still work.', 'red')
            return False
        return True

    def _addr(self, s=None):
        return lichii.parse_addr(s if s is not None else self.addr.get(), self.modules)

    def do_read(self):
        if not self._need():
            return
        a, n = self._addr(), int(self.rsize.get(), 0)

        def done(b):
            self.hexv.delete('1.0', 'end')
            for i in range(0, len(b), 16):
                row = b[i:i + 16]
                asc = ''.join(chr(c) if 32 <= c < 127 else '.' for c in row)
                self.hexv.insert('end', '%012X  %-48s %s\n' % (a + i, row.hex(' '), asc))
            if len(b) >= 4:
                self.hexv.insert('end', '\n@%X  u32 %d  i32 %d  f32 %.6g' % (
                    a, *struct.unpack_from('<I', b), struct.unpack_from('<i', b)[0], struct.unpack_from('<f', b)[0]))
        self.bg(lambda: self.dbg.read(a, n), done)

    def do_write(self):
        if not self._need(True):
            return
        a = self._addr()
        data = encode(self.wtype.get(), self.wval.get())
        main = self.modules.get('main')
        try:
            text_end = lichii.nso_text_end(self.main)
        except Exception:
            text_end = 0
        if main and main[0] <= a < main[0] + text_end and not messagebox.askyesno(
                'Dev Tool', 'This address is in main\'s code. The JIT does not see code edits made from '
                'outside; use a .pchtxt patch for code. Write anyway?'):
            return
        self.bg(lambda: (self.host.write(a, data), self.dbg.read(a, len(data)))[1],
                lambda b: self.log('wrote %s at %#x, reads back %s' % (data.hex(), a, b.hex())))

    def _pattern(self):
        t, v = self.stype.get(), self.sval.get().strip()
        if t == 'flag name/hash':
            h = int(v.lstrip('#'), 16) if v.startswith(('#', '0x')) else gamedata.mmh(v)
            return struct.pack('<I', h), 'u32'
        return encode(t, v), t

    def do_scan(self):
        if not self._need():
            return
        pat, t = self._pattern()
        self.scan_type = t
        self.scan_pat = pat

        def done(r):
            hits, complete = r
            self.scan_hits = hits
            self._fill_hits()
            self.log('scan %s: %d hit(s)%s' % (pat.hex(), len(hits), '' if complete else ' (search budget hit; more may exist)'))
        self.bg(lambda: self.dbg.search(pat, regions=self.sregion.get(), max_hits=256, total_ms=15000), done)

    def do_rescan(self):
        if not self._need() or not self.scan_hits:
            return
        pat, t = self._pattern()

        def work():
            keep = []
            for a in self.scan_hits:
                try:
                    if self.dbg.read(a, len(pat)) == pat:
                        keep.append(a)
                except Exception:
                    pass
            return keep

        def done(keep):
            self.log('next scan: %d -> %d hit(s)' % (len(self.scan_hits), len(keep)))
            self.scan_hits = keep
            self._fill_hits()
        self.bg(work, done)

    def _fill_hits(self):
        self.hitlist.delete(0, 'end')
        for a in self.scan_hits:
            self.hitlist.insert('end', '0x%X' % a)

    def _pick_hit(self, _ev):
        s = self.hitlist.curselection()
        if s:
            self.addr.set(self.hitlist.get(s[0]))
            self.do_read()

    # ------------------------------------------------------------ watches
    def _watch_file(self):
        return os.path.join(USER, 'watches_%s.json' % self.bid[:16])

    def _load_watches(self):
        try:
            self.watches = json.load(open(self._watch_file()))
        except Exception:
            self.watches = []
        self._fill_watches()

    def _store_watches(self):
        json.dump(self.watches, open(self._watch_file(), 'w'), indent=1)

    def _fill_watches(self):
        self.wt.delete(*self.wt.get_children())
        for i, w in enumerate(self.watches):
            self.wt.insert('', 'end', iid=str(i), values=(w['label'], w['addr'], w['type'], w.get('value', ''),
                                                          'YES' if w.get('freeze') else ''))

    def add_watch_here(self):
        label = simpledialog.askstring('Watch', 'Label for %s:' % self.addr.get())
        if label is None:
            return
        self.watches.append({'label': label, 'addr': self.addr.get(), 'type': self.wtype.get(), 'freeze': None})
        self._store_watches()
        self._fill_watches()

    def del_watch(self):
        for s in sorted((int(x) for x in self.wt.selection()), reverse=True):
            del self.watches[s]
        self._store_watches()
        self._fill_watches()

    def toggle_freeze(self):
        if not self._need(True):
            return
        for s in self.wt.selection():
            w = self.watches[int(s)]
            w['freeze'] = None if w.get('freeze') else w.get('value')
        self._fill_watches()

    def edit_watch(self):
        sel = self.wt.selection()
        if not sel or not self._need(True):
            return
        w = self.watches[int(sel[0])]
        v = simpledialog.askstring('Set value', '%s (%s):' % (w['label'], w['type']), initialvalue=w.get('value', ''))
        if v is None:
            return
        a = self._addr(w['addr'])
        data = encode(w['type'], v)
        if w.get('freeze'):
            w['freeze'] = v
        self.bg(lambda: self.host.write(a, data), lambda r: self.log('%s = %s' % (w['label'], v)))

    def _watch_tick(self):
        if self.dbg and self.watches and self.nb.index(self.nb.select()) == 2:
            ws = list(self.watches)

            def work():
                out = []
                for w in ws:
                    try:
                        a = self._addr(w['addr'])
                        if w.get('freeze') not in (None, '') and self.host:
                            self.host.write(a, encode(w['type'], str(w['freeze'])))
                        out.append(decode(w['type'], self.dbg.read(a, size_of(w['type']))))
                    except Exception as e:
                        out.append('?')
                return out

            def done(vals):
                for i, v in enumerate(vals):
                    if i < len(self.watches):
                        self.watches[i]['value'] = v
                        if self.wt.exists(str(i)):
                            self.wt.set(str(i), 'value', v)
                self.root.after(500, self._watch_tick)
            self.bg(work, done)
        else:
            self.root.after(500, self._watch_tick)

    # ============================================================ Camera tab
    CAM_EXAMPLE = {'explore': 50.0, 'lockon': 50.0, 'aiming': 40.0, 'horse': 55.0, 'talk': 40.0}

    def _tab_camera(self, nb):
        import camera
        self.cam = camera
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='Camera')
        cfg_path = os.path.join(camera.mod_dir(self.data, self.tid), 'devtool_camera.json')
        try:
            s = json.load(open(cfg_path))
        except Exception:
            s = camera.default_settings()
        self.cam_vars = {}
        fv = ttk.LabelFrame(f, text='Field of view  (100%% = game default; values capped at %d degrees)' % camera.FOV_CAP, padding=8)
        fv.pack(fill='x')
        self.cam_all = tk.DoubleVar(value=100)

        def set_all(_=None):
            for c, _l in camera.CATEGORIES:
                if c != 'talk' or self.cam_talk_follow.get():
                    self.cam_vars[c].set(round(self.cam_all.get()))
            self._cam_labels()
        row = ttk.Frame(fv)
        row.pack(fill='x', pady=(0, 6))
        ttk.Label(row, text='All gameplay cameras', width=46).pack(side='left')
        ttk.Scale(row, from_=25, to=250, variable=self.cam_all, command=set_all, length=380).pack(side='left')
        self.cam_talk_follow = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text='include talk/event cameras', variable=self.cam_talk_follow).pack(side='left', padx=8)
        self.cam_lbls = {}
        for c, label in camera.CATEGORIES:
            r = ttk.Frame(fv)
            r.pack(fill='x')
            ttk.Label(r, text=label, width=46).pack(side='left')
            v = tk.DoubleVar(value=round(float(s['fov'].get(c, 1.0)) * 100))
            self.cam_vars[c] = v
            ttk.Scale(r, from_=25, to=250, variable=v, command=lambda _=None: (self._cam_labels(), self._cam_live_kick()), length=380).pack(side='left')
            lb = ttk.Label(r, width=34)
            lb.pack(side='left', padx=8)
            self.cam_lbls[c] = lb
        other = ttk.LabelFrame(f, text='Distance and controls', padding=8)
        other.pack(fill='x', pady=8)
        self.cam_dist = tk.DoubleVar(value=round(float(s.get('distance', 1.0)) * 100))
        self.cam_sens = tk.DoubleVar(value=round(float(s.get('sensitivity', 1.0)) * 100))
        for text, var, lo, hi in (('Camera distance (exploring, lock-on, horse)', self.cam_dist, 20, 500),
                                  ('Right-stick sensitivity (all 5 in-game levels)', self.cam_sens, 25, 400)):
            r = ttk.Frame(other)
            r.pack(fill='x')
            ttk.Label(r, text=text, width=46).pack(side='left')
            ttk.Scale(r, from_=lo, to=hi, variable=var, command=lambda _=None: (self._cam_labels(), self._cam_live_kick()), length=380).pack(side='left')
            lb = ttk.Label(r, width=34)
            lb.pack(side='left', padx=8)
            self.cam_lbls[id(var)] = lb
        btn = ttk.Frame(f)
        btn.pack(fill='x')
        ttk.Button(btn, text='Preview changes', command=self.cam_preview).pack(side='left')
        ttk.Button(btn, text='Build camera mod (applies next boot)', command=self.cam_build).pack(side='left', padx=6)
        ttk.Button(btn, text='Remove camera mod', command=self.cam_remove).pack(side='left')
        ttk.Button(btn, text='Reset sliders', command=self.cam_reset).pack(side='left', padx=6)
        ttk.Button(btn, text='Check mod loaded (in-game)', command=self.cam_check).pack(side='left')
        lv = ttk.LabelFrame(f, text='Live (no restart) - needs Connect', padding=6)
        lv.pack(fill='x', pady=(6, 0))
        ttk.Button(lv, text='Find live camera (in-game)', command=self.cam_find_live).pack(side='left')
        self.cam_live_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(lv, text='Apply slider changes live', variable=self.cam_live_on,
                        command=self._cam_live_kick).pack(side='left', padx=10)
        self.cam_live_lbl = ttk.Label(lv, text='not set up yet')
        self.cam_live_lbl.pack(side='left', padx=6)
        self.cam_live_obj = None
        self._cam_live_job = None
        self.cam_txt = tk.Text(f, height=12, font=('Consolas', 9))
        self.cam_txt.pack(fill='both', expand=True, pady=6)
        self.cam_txt.insert('end', (
            'The camera is pure data in this game (PlayerCamera.pack.zs), so these settings are\n'
            'baked into a RomFS mod folder "%s" next to your other mods. It loads when you\n'
            'boot the loose dump ("Launch Lichii" here does that), not the .nup. Restart the game\n'
            'after building.\n\n'
            'Live: in-game, Connect, click "Find live camera", tick "Apply slider changes live",\n'
            'then move the sliders. The values are written into the camera settings the game keeps\n'
            'in RAM. If the view does not change at once, switch camera mode (Z-target, enter a\n'
            'building, swim) so the game re-reads them. Live changes last until the game restarts;\n'
            'use "Build camera mod" to keep them.\n' % camera.MOD_NAME))
        self._cam_labels()

    def _cam_settings(self):
        return {'fov': {c: round(self.cam_vars[c].get()) / 100.0 for c, _ in self.cam.CATEGORIES},
                'distance': round(self.cam_dist.get()) / 100.0,
                'sensitivity': round(self.cam_sens.get()) / 100.0}

    def _cam_labels(self):
        s = self._cam_settings()
        for c, _ in self.cam.CATEGORIES:
            base = self.CAM_EXAMPLE[c]
            self.cam_lbls[c].configure(text='%d%%  (e.g. %.0f -> %.0f deg)' % (
                round(s['fov'][c] * 100), base, min(base * s['fov'][c], self.cam.FOV_CAP)))
        self.cam_lbls[id(self.cam_dist)].configure(text='%d%%  (e.g. 4.0 -> %.1f m)' % (s['distance'] * 100, 4 * s['distance']))
        self.cam_lbls[id(self.cam_sens)].configure(text='%d%%' % (s['sensitivity'] * 100))

    def _rf(self):
        if not hasattr(self, '_romfs_obj'):
            self._romfs_obj = gamedata.RomFS(self.romfs)
        return self._romfs_obj

    def _cam_show(self, title, changes):
        self.cam_txt.delete('1.0', 'end')
        self.cam_txt.insert('end', '%s: %d values\n' % (title, len(changes)))
        shown = 0
        for name, path, old, new in changes:
            if shown > 400:
                self.cam_txt.insert('end', '...\n')
                break
            self.cam_txt.insert('end', '%-44s %-58s %8.3f -> %8.3f\n' % (name.split('/')[-1][:44], path[:58], old, new))
            shown += 1

    def cam_preview(self):
        s = self._cam_settings()
        self.bg(lambda: self.cam.build(self._rf(), s)[1], lambda ch: self._cam_show('Would change', ch))

    def cam_build(self):
        s = self._cam_settings()
        others = self.cam.other_mods_touching(self.data, self.tid)
        if others and not messagebox.askyesno('Dev Tool', 'These mods also replace PlayerCamera.pack.zs and will '
                                              'conflict: %s\n\nBuild anyway?' % ', '.join(others)):
            return

        def done(r):
            changes, target = r
            self._cam_show('Built' if target else 'All sliders at 100% - mod removed', changes)
            if target:
                self.log('Camera mod written to %s (%d values). Restart the game (loose dump) to use it.'
                         % (target, len(changes)))
            if lichii.lichii_pids():
                self.log('Lichii is running: the camera mod loads on the next boot.')
        self.bg(lambda: self.cam.install(self._rf(), self.data, self.tid, s), done)

    def cam_remove(self):
        if self.cam.remove(self.data, self.tid):
            self.log('Camera mod removed; the game uses its own camera from the next boot.')
        else:
            self.log('No camera mod installed.')

    def cam_reset(self):
        for v in self.cam_vars.values():
            v.set(100)
        self.cam_all.set(100)
        self.cam_dist.set(100)
        self.cam_sens.set(100)
        self._cam_labels()

    def cam_check(self):
        if not self._need():
            return
        if self.cam_live_obj:
            return self.status('Mod check skipped: live camera edits have changed the values it looks for. '
                               'Restart the game to check the mod again.', '#b06000')
        self.status('Checking the camera mod: searching game RAM (up to a minute)...', 'blue')
        def rf_or_none():
            try:
                return self._rf()
            except ImportError:
                return None
        self.bg(lambda: self.cam.check_loaded(self.dbg, rf_or_none(), self.data, self.tid),
                lambda r: (self.status('Camera mod check: ' + r, 'darkgreen' if r.startswith('LOADED') else '#a00000'),
                           self.cam_txt.insert('1.0', 'Camera mod check: ' + r + '\n\n')))

    def cam_find_live(self):
        if not self._need(True):
            return
        self.status('Finding the camera settings in game RAM...', 'blue')
        self.cam_live_lbl.configure(text='searching...')

        def rf_or_none():
            try:
                return self._rf()
            except ImportError:
                return None

        def prog(text):
            self.q.put((lambda t: self.cam_live_lbl.configure(text=t), text, None))

        def work():
            cmap = self.cam.load_live_map(rf_or_none(), HERE, self.bid)
            lc = self.cam.LiveCamera(self.dbg, self.host, cmap)
            n = lc.find(progress=lambda i, total, name: prog(
                'finding the camera files (pass %d/%d)...' % (i + 1, total)))
            report = ['Live camera probe %s' % time.strftime('%Y-%m-%d %H:%M:%S'),
                      'param files: %s' % {k.split('/')[-1]: ['%#x' % a for a in v] for k, v in lc.bases.items()
                                            if 'AIParam' in k or 'PlayerCameraParam/' in k},
                      'packs: %s' % ['%#x' % p for p in lc.packs]]
            cfg = os.path.join(self.cam.mod_dir(self.data, self.tid), 'devtool_camera.json')
            try:
                installed = json.load(open(cfg))
            except Exception:
                installed = None
            parsed = 0
            ok_markers = bool(installed and installed.get('markers') == self.cam.MARKER_SCHEME)
            if ok_markers:
                parsed = lc.find_parsed(installed, progress=lambda i, total, path: prog(
                    'finding the game\'s working copies (%d/%d: %s)...' % (i + 1, total, path)), report=report,
                    exclude=[(b, b + sz) for b, sz in self.modules.values()])
            else:
                report.append('camera mod has no (current) markers: rebuild it with the game closed')
            report.append('parsed values found: %d (%d distinct)' % (parsed, len(lc.parsed) if hasattr(lc, 'parsed') else 0))
            if getattr(lc, 'parsed', None):
                for idx, addrs in sorted(lc.parsed.items()):
                    row = cmap['values'][idx]
                    report.append('  %-60s %s' % (row[5] if len(row) > 5 else idx, ' '.join('%#x' % a for a in addrs)))
            open(os.path.join(USER, 'camera_probe.txt'), 'w').write('\n'.join(report))
            return lc, n, parsed, ok_markers

        def done(r):
            lc, n, parsed, has_markers = r
            if not n and not parsed:
                self.cam_live_obj = None
                self.cam_live_lbl.configure(text='not found - load into the world and try again')
                return self.status('Live camera: settings not found in RAM yet. Load into the world and retry.', '#a00000')
            self.cam_live_obj = lc
            main = sum(len(v) for k, v in lc.bases.items() if 'AIParam' in k)
            self.cam_live_lbl.configure(text='ready: %d param-file copies, %d working values' % (main, parsed))
            self.log('Live camera: main file at %s; unpacked pack(s) at %s; %d working values (details in '
                     'user\\camera_probe.txt)' % (
                         ', '.join('%#x' % a for k, v in lc.bases.items() if 'AIParam' in k for a in v),
                         ', '.join('%#x' % p for p in lc.packs) or 'none', parsed))
            if not has_markers:
                self.status('Live camera: rebuild the camera mod once with the game closed, then restart, '
                            'so the tool can find the values the camera really uses.', '#b06000')
            else:
                self.status('Live camera ready (%d working values). Tick "Apply slider changes live".' % parsed,
                            'darkgreen' if parsed else '#b06000')
            self._cam_live_kick()
        self.bg(work, done)

    def _cam_live_kick(self):
        """Debounced: write the slider values into RAM shortly after the last slider move."""
        if not (self.cam_live_on.get() and self.cam_live_obj and self.host):
            return
        if self._cam_live_job:
            self.root.after_cancel(self._cam_live_job)
        self._cam_live_job = self.root.after(150, self._cam_live_apply)

    def _cam_live_apply(self):
        self._cam_live_job = None
        s = self._cam_settings()
        lc = self.cam_live_obj
        def done(n):
            msg = 'live: wrote %d values (FOV %s, distance %d%%, sensitivity %d%%)' % (
                n, ' '.join('%s %d%%' % (c, round(v * 100)) for c, v in s['fov'].items()),
                round(s['distance'] * 100), round(s['sensitivity'] * 100))
            self.cam_live_lbl.configure(text='live: wrote %d values' % n)
            self.log(msg)
        self.bg(lambda: lc.apply(s), done)

    # ============================================================ Cheats tab
    # ============================================================ World tab (v0.7): map select, reload, age, time, weather, HUD, events
    def _core_lists(self):
        st = self._core().status() or {}
        return st.get('lists') or {}

    def _picklist(self, parent, title, height=10, width=44):
        """A filter entry over a listbox. Returns (frame, listbox, items_holder) where items_holder[0]
        is the full list and listbox shows the filtered one; pick() gives the selected full-list index."""
        fr = ttk.LabelFrame(parent, text=title, padding=4)
        var = tk.StringVar()
        e = ttk.Entry(fr, textvariable=var, width=width)
        e.pack(fill='x')
        lb = tk.Listbox(fr, height=height, width=width, exportselection=False, font=('Consolas', 9))
        lb.pack(fill='both', expand=True)
        hold = {'items': [], 'shown': []}

        def fill(*_):
            q = var.get().lower()
            hold['shown'] = [i for i, t in enumerate(hold['items']) if q in t.lower()]
            lb.delete(0, 'end')
            for i in hold['shown']:
                lb.insert('end', hold['items'][i])

        def pick():
            sel = lb.curselection()
            return hold['shown'][sel[0]] if sel else None

        def setitems(items):
            hold['items'] = list(items)
            fill()
        e.bind('<KeyRelease>', fill)
        fr.pick, fr.setitems, fr.lb, fr.var, fr.fill = pick, setitems, lb, var, fill
        return fr

    def _tab_worldctl(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='World')
        top = ttk.Frame(f)
        top.pack(fill='x')
        ttk.Label(top, justify='left', wraplength=1250, text=(
            'v0.7, all unconfirmed. Every action has two routes: "raw" writes the GameData values the game itself uses '
            '(Sequence_Reserved*, World_CurrentDateTime...), "Core" sends a command through DevTool Core (needs the Core '
            'mod built and loaded; the lists below come from the build, so they match the generated event flow).')).pack(anchor='w')
        self.route = tk.StringVar(value='raw')
        rr = ttk.Frame(f)
        rr.pack(fill='x', pady=4)
        ttk.Label(rr, text='Route:').pack(side='left')
        ttk.Radiobutton(rr, text='raw GameData writes', variable=self.route, value='raw').pack(side='left', padx=4)
        ttk.Radiobutton(rr, text='DevTool Core event', variable=self.route, value='core').pack(side='left', padx=4)
        self.world_lbl = ttk.Label(rr, text='', foreground=C['dim'])
        self.world_lbl.pack(side='left', padx=12)
        body = ttk.Frame(f)
        body.pack(fill='both', expand=True)
        # --- B1 map select
        left = ttk.Frame(body)
        left.pack(side='left', fill='y')
        self.ms = self._picklist(left, 'Map select: every start point (map / point)', height=14, width=33)
        self.ms.pack(fill='both', expand=True)
        self.ms_mode = tk.StringVar(value='startpos')
        ttk.Radiobutton(left, text='go to the start point (name)', variable=self.ms_mode, value='startpos').pack(anchor='w')
        ttk.Radiobutton(left, text="coordinates (position route, like Farore's Wind)", variable=self.ms_mode, value='coords').pack(anchor='w')
        r = ttk.Frame(left)
        r.pack(anchor='w', pady=2)
        ttk.Button(r, text='Go', command=self.ms_go).pack(side='left')
        ttk.Button(r, text='Reload this map here', command=lambda: self.ms_reload('here')).pack(side='left', padx=4)
        ttk.Button(r, text='Respawn at last start point', command=lambda: self.ms_reload('entrance')).pack(side='left')
        r = ttk.Frame(left)
        r.pack(anchor='w', pady=2)
        ttk.Button(r, text='Void out (reload at the save position)', command=lambda: self.ms_reload('void')).pack(side='left')
        ttk.Button(r, text='Swap age in place (child <-> adult)', command=self.ms_age).pack(side='left', padx=4)
        # --- A4 time / weather, E1 HUD, dungeon
        mid = ttk.Frame(body)
        mid.pack(side='left', fill='y', padx=10)
        tw = ttk.LabelFrame(mid, text='Time of day and weather', padding=4)
        tw.pack(fill='x')
        r = ttk.Frame(tw)
        r.pack(anchor='w')
        for name, h in (('Morning', 6), ('Noon', 12), ('Dusk', 18), ('Night', 0)):
            ttk.Button(r, text=name, width=7, command=lambda h=h: self.world_time(h)).pack(side='left', padx=1)
        r = ttk.Frame(tw)
        r.pack(anchor='w', pady=2)
        ttk.Label(r, text='Hour').pack(side='left')
        self.time_hour = tk.Spinbox(r, from_=0, to=23, width=4)
        self.time_hour.pack(side='left', padx=2)
        ttk.Label(r, text='Min').pack(side='left')
        self.time_min = tk.Spinbox(r, from_=0, to=59, width=4)
        self.time_min.pack(side='left', padx=2)
        ttk.Button(r, text='Set time', width=9, command=lambda: self.world_time(int(self.time_hour.get()), int(self.time_min.get()))).pack(side='left', padx=4)
        r = ttk.Frame(tw)
        r.pack(anchor='w', pady=2)
        ttk.Label(r, text='Weather (Core):').pack(side='left')
        self.weather = ttk.Combobox(r, values=core.WEATHER, width=20, state='readonly')
        self.weather.set(core.WEATHER[0])
        self.weather.pack(side='left', padx=2)
        ttk.Button(r, text='Set', width=5, command=self.world_weather).pack(side='left')
        ttk.Label(tw, foreground=C['dim'], justify='left', wraplength=300, text=(
            'raw = World_CurrentDateTime (day kept, seconds replaced). Core = EventTrigger_SetTime(hour) / '
            'Event_SetWeatherForFlowchart (names seen in the flows; no raw weather value known).')).pack(anchor='w')
        hud = ttk.LabelFrame(mid, text='HUD (Core: EventTriggerSetDisplay*)', padding=4)
        hud.pack(fill='x', pady=(6, 0))
        self.hud_vars = []
        r = ttk.Frame(hud)
        r.pack(anchor='w')
        for i, name in enumerate(core.HUD_BITS):
            v = tk.BooleanVar(value=False)
            self.hud_vars.append(v)
            ttk.Checkbutton(r, text='hide ' + name, variable=v).grid(row=i // 2, column=i % 2, sticky='w')
        ttk.Button(hud, text='Apply HUD', width=12, command=self.world_hud).pack(anchor='w', pady=2)
        dg = ttk.LabelFrame(mid, text='This dungeon (Core: EventGetDungeon*)', padding=4)
        dg.pack(fill='x', pady=(6, 0))
        r = ttk.Frame(dg)
        r.pack(anchor='w')
        for i, name in enumerate(core.DUNGEON_ITEMS):
            ttk.Button(r, text='Get ' + name, width=11, command=lambda i=i: self.core_send('dungeon', i, 'the %s of this dungeon' % core.DUNGEON_ITEMS[i])).grid(row=i // 2, column=i % 2, sticky='w', padx=1, pady=1)
        ttk.Label(mid, foreground=C['dim'], justify='left', wraplength=300, text=(
            'Per-dungeon keys, maps, boss flags, medallions, stones: Items tab (raw).')).pack(anchor='w', pady=(4, 0))
        # --- E5 event player, E6 sound / message
        right = ttk.Frame(body)
        right.pack(side='left', fill='both', expand=True)
        self.evp = self._picklist(right, 'Event player (Core sub-flow; "event player" ticked at build)', height=5, width=40)
        self.evp.pack(fill='x')
        ttk.Button(self.evp, text='Play event', command=lambda: self._core_pick(self.evp, 'event', 'the event to play')).pack(anchor='w', pady=2)
        snd = ttk.Frame(right)
        snd.pack(fill='x', pady=(4, 0))
        self.bgm = self._picklist(snd, 'BGM (Core)', height=4, width=20)
        self.bgm.pack(side='left', fill='both', expand=True)
        r = ttk.Frame(self.bgm)
        r.pack(anchor='w')
        ttk.Button(r, text='Play', width=5, command=lambda: self._core_pick(self.bgm, 'bgm', 'the BGM to start')).pack(side='left')
        ttk.Button(r, text='Stop BGM', width=9, command=lambda: self.core_send('bgm_stop', 0, 'the music to stop')).pack(side='left', padx=2)
        self.se = self._picklist(snd, 'SE (Core)', height=4, width=18)
        self.se.pack(side='left', fill='both', expand=True, padx=4)
        ttk.Button(self.se, text='Play', width=5, command=lambda: self._core_pick(self.se, 'se', 'the sound')).pack(anchor='w')
        self.msg = self._picklist(right, 'Message (Core: SystemTextNPC EventTalk)', height=4, width=40)
        self.msg.pack(fill='x', pady=(4, 0))
        ttk.Button(self.msg, text='Show', width=5, command=lambda: self._core_pick(self.msg, 'message', 'the message box')).pack(anchor='w')
        self.world_fill()

    def world_fill(self):
        L = self._core_lists()
        self.world_lists = L
        self.ms.setitems(['%s / %s' % (b, p or '(default)') for b, p, _pos in L.get('warps', [])] or self._warps_from_catalog())
        self.evp.setitems(['%s   (%s : %s)' % tuple(e) for e in L.get('events', [])])
        self.bgm.setitems(L.get('bgm', []))
        self.se.setitems(L.get('se', []))
        self.msg.setitems(L.get('messages', []))
        self.world_lbl.configure(text=('Core lists from the build of %s' % (self._core().status() or {}).get('built')) if L else
                                 'DevTool Core not built: Core routes unavailable, raw routes work')

    def _warps_from_catalog(self):
        self._load_catalog()
        out = []
        for b, e in sorted((self.catalog or {}).get('bancs', {}).items()):
            for p in sorted(e.get('startpos', {})):
                out.append('%s / %s' % (b, p or '(default)'))
        return out

    def _core_pick(self, fr, cmd, what):
        i = fr.pick()
        if i is None:
            return self.status('Pick an entry in the list first.', 'red')
        self.core_send(cmd, i, '%s: %s' % (what, fr.lb.get(fr.lb.curselection()[0])))

    def _warp_entry(self):
        i = self.ms.pick()
        if i is None:
            return None
        L = self.world_lists.get('warps')
        if L:
            return i, L[i][0], L[i][1], L[i][2]
        b, p = self.ms.lb.get(self.ms.lb.curselection()[0]).split(' / ')
        p = '' if p == '(default)' else p
        pos = ((self.catalog or {}).get('bancs', {}).get(b, {}).get('startpos', {}).get(p) or {}).get('pos')
        return i, b, p, pos

    def _enum_value(self, name, label):
        d = self.defs.get(gamedata.mmh(name))
        if d and d.raw_values and label in d.raw_values:
            return d.values[d.raw_values.index(label)]
        return gamedata.mmh(label)

    def raw_warp(self, banc, startpos=None, pos=None, yaw=0.0, boot='', fade='LoadingFade', how='map select'):
        """Route 1: the values each door (name route) or Farore's Wind (position route) writes, then
        Sequence_IsLoadingBanc. Whether setting IsLoadingBanc from outside starts the load is unconfirmed."""
        if not self.live or not self.live.host:
            return self.status('Connect first (with write access).', 'red')
        H = gamedata.mmh

        def w(name, v):
            h = H(name)
            if h not in self.live.by_hash:
                raise ValueError(name + ' is not live')
            before = self.live.vals.get(h)
            self.live.write(h, v)
            self.flog_tool(self.live.by_hash[h], before, self.live.vals.get(h), 'set by you')
        try:
            w('Sequence_ReservedNextBanc', banc)
            w('Sequence_ReservedBootEventName', boot)
            w('Sequence_ReservedEventStarterName', '')
            w('Sequence_BancChangeFadePattern', self._enum_value('Sequence_BancChangeFadePattern', fade))
            if pos is not None:
                w('Sequence_ReservedNextStartPosName', '')
                w('Sequence_ReservedNextPosition', tuple(float(x) for x in pos))
                w('Sequence_ReservedNextCancelSleepRot', (0.0, float(yaw), 0.0))
                w('Sequence_IsWarpByFaroreWind', True)
            else:
                w('Sequence_IsWarpByFaroreWind', False)
                w('Sequence_ReservedNextStartPosName', startpos or '')
            w('Sequence_IsLoadingBanc', True)
        except Exception as e:
            return self.status('raw warp failed: %s' % e, 'red')
        self.warp_shot = (banc, time.time())
        self.status('raw %s: %s %s written + Sequence_IsLoadingBanc = True. If nothing happens within a few seconds, '
                    'the game does not start a load from these values alone: use Arm warp + a door, or the Core route.'
                    % (how, banc, (startpos or '(default start point)') if pos is None else fmt_val(tuple(pos))), 'blue')

    def ms_go(self):
        e = self._warp_entry()
        if not e:
            return self.status('Pick a start point first.', 'red')
        i, banc, sp, pos = e
        if self.route.get() == 'core':
            if not self.world_lists.get('warps'):
                return self.status('Core route needs DevTool Core built (its warp list).', 'red')
            return self.core_send('warp', i, 'a load into %s at %s' % (banc, sp))
        if self.ms_mode.get() == 'coords' and pos:
            self.raw_warp(banc, pos=pos, boot='FaroreWindArrive', fade='WhiteFade')
        else:
            self.raw_warp(banc, startpos=sp)

    def ms_reload(self, how):
        if not self.live:
            return self.status('Connect first.', 'red')
        banc = self.live.vals.get(gamedata.mmh('Sequence_CurrentBanc'))
        if not banc:
            return self.status('No current map known yet.', 'red')
        if self.route.get() == 'core':
            W = self.world_lists.get('warps') or []
            idx = [i for i, w in enumerate(W) if w[0] == banc]
            if not idx:
                return self.status('Core route: no start point of %s in the Core warp list.' % banc, 'red')
            return self.core_send('warp', idx[0], 'a reload of %s at %s' % (banc, W[idx[0]][1]))
        if how == 'here':
            pos = self.live.vals.get(gamedata.mmh('Player_Pos_For_Map'))
            yaw = self.live.vals.get(gamedata.mmh('PlayerStatus.SavePosRadY')) or 0.0
            return self.raw_warp(banc, pos=pos, yaw=yaw, boot='FaroreWindArrive', fade='WhiteFade', how='reload here')
        if how == 'void':
            pos = self.live.vals.get(gamedata.mmh('PlayerStatus.SavePos'))
            yaw = self.live.vals.get(gamedata.mmh('PlayerStatus.SavePosRadY')) or 0.0
            return self.raw_warp(banc, pos=pos, yaw=yaw, boot='FaroreWindArrive', fade='BlackFade', how='void out')
        sp = self.live.vals.get(gamedata.mmh('Sequence_ReservedNextStartPosName')) or ''
        if not sp:
            self._load_catalog()
            pts = sorted((self.catalog or {}).get('bancs', {}).get(banc, {}).get('startpos', {}))
            sp = pts[0] if pts else ''
        return self.raw_warp(banc, startpos=sp, how='respawn at %s' % (sp or 'the default start point'))

    def ms_age(self):
        if not self.live or not self.live.host:
            return self.status('Connect first (with write access).', 'red')
        h = gamedata.mmh('Sequence_IsChildhood')
        cur = bool(self.live.vals.get(h))
        before = cur
        self.live.write(h, not cur)
        self.flog_tool(self.live.by_hash[h], before, not cur, 'set by you')
        self.status('Sequence_IsChildhood %s -> %s; reloading the map here so Link\'s model follows (the warp tab showed the '
                    'age follows the map on load).' % (before, not cur), 'blue')
        self.ms_reload('here')

    def world_time(self, hour, minute=0):
        if self.route.get() == 'core':
            return self.core_send('time', hour, 'the clock at %02d:00 (Core sets whole hours only)' % hour)
        if not self.live or not self.live.host:
            return self.status('Connect first (with write access).', 'red')
        h = gamedata.mmh('World_CurrentDateTime')
        cur = self.live.vals.get(h)
        if not isinstance(cur, int):
            return self.status('World_CurrentDateTime is not live.', 'red')
        new = (cur >> 32 << 32) | (hour * 3600 + minute * 60)
        self.live.write(h, new)
        self.flog_tool(self.live.by_hash[h], cur, new, 'set by you')
        self.status('World_CurrentDateTime: %s -> %s (raw; if the sky does not follow, the clock is a copy and the Core route is next)'
                    % (gdlive.fmt_datetime(cur), gdlive.fmt_datetime(new)), 'blue')

    def world_weather(self):
        i = core.WEATHER.index(self.weather.get())
        self.core_send('weather', i, 'weather: %s' % self.weather.get())

    def world_hud(self):
        bits = sum(1 << i for i, v in enumerate(self.hud_vars) if v.get())
        self.core_send('hud', bits, 'hidden: %s' % (', '.join(n for n, v in zip(core.HUD_BITS, self.hud_vars) if v.get()) or 'nothing'))

    # ============================================================ Items tab (v0.7): give item, bottles, songs, dungeons, quest flags
    DUNGEONS = ['InsideTheDekuTree', 'DodongoCavern', 'InsideJabuJabuBelly', 'ForestTemple', 'FireTemple', 'WaterTemple',
                'ShadowTemple', 'SpiritTemple', 'BottomOfTheWell', 'IceCavern', 'GerudoTrainingGround', 'GerudoFortress',
                'InsideGanonCastle', 'FinalBattleField']
    BOSSES = ['Gohma', 'Dodongo', 'Barinade', 'PhantomGanon', 'Volvagia', 'Morpha', 'BongoBongo', 'Twinrova', 'Ganondorf', 'Ganon']
    QUEST_RX = ('_IsCompleted', '_IsAfter_', '_Step', 'IsGet.Item_Medal_', 'IsGet.Item_Stone_', 'HaveDefeatedEnemy_Enemy_DungeonBoss_',
                'DoorOfTime_', 'AllSage_')

    def _tab_items(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='Items')
        ttk.Label(f, justify='left', wraplength=1250, text=(
            'v0.7, unconfirmed. Give item raw = IsGet.<item> True + PouchData.<item>.StockNum (what the magic meter and '
            "Farore's Wind already use); Core = PouchSystemActor::EventTriggerAddOrRemovePouchContent, with or without "
            'the "you got" scene. The game re-applies its own limits on its next change (wallet size etc.).')).pack(anchor='w')
        body = ttk.Frame(f)
        body.pack(fill='both', expand=True, pady=4)
        left = ttk.Frame(body)
        left.pack(side='left', fill='y')
        self.gi = self._picklist(left, 'Give item (every PouchActorInfo row)', height=14, width=38)
        self.gi.pack(fill='both', expand=True)
        r = ttk.Frame(left)
        r.pack(anchor='w', pady=2)
        ttk.Label(r, text='Count').pack(side='left')
        self.gi_num = tk.Spinbox(r, from_=1, to=999, width=5)
        self.gi_num.pack(side='left', padx=2)
        ttk.Button(r, text='Give (raw)', command=lambda: self.gi_give('raw')).pack(side='left', padx=2)
        ttk.Button(r, text='Give (Core, silent)', command=lambda: self.gi_give('item')).pack(side='left', padx=2)
        ttk.Button(r, text='Give (Core, with scene)', command=lambda: self.gi_give('item_scene')).pack(side='left', padx=2)
        self.gi_lbl = ttk.Label(left, text='', foreground=C['dim'], wraplength=420, justify='left')
        self.gi_lbl.pack(anchor='w')
        bt = ttk.LabelFrame(left, text='Bottles (raw: PlayerStatus.AssignableItem.BottleInfo.<Bottle>.BottleItem)', padding=4)
        bt.pack(fill='x', pady=(6, 0))
        r = ttk.Frame(bt)
        r.pack(anchor='w')
        self.bottle = ttk.Combobox(r, values=['BottleA', 'BottleB', 'BottleC', 'BottleD', 'BottleE'], width=8, state='readonly')
        self.bottle.set('BottleA')
        self.bottle.pack(side='left')
        # v0.8.3: BottleItem is an Enum (values from the GameDataList), not an item name
        self.bottle_item = ttk.Combobox(r, values=list(loadout.BOTTLE_CONTENTS), width=14, state='readonly')
        self.bottle_item.set('Fairy')
        self.bottle_item.pack(side='left', padx=4)
        ttk.Button(r, text='Set bottle', command=self.gi_bottle).pack(side='left')
        mid = ttk.Frame(body)
        mid.pack(side='left', fill='y', padx=10)
        sg = ttk.LabelFrame(mid, text='Songs, equipment, flags (raw IsGet.* toggles)', padding=4)
        sg.pack(fill='x')
        self.flag_vars = {}
        grid = ttk.Frame(sg)
        grid.pack(anchor='w')
        names = (['Item_Melody_' + x for x in ('ZeldaLullaby', 'EponaSong', 'SariaSong', 'SunSong', 'TimeSong', 'StormSong', 'ForestMinuet',
                                                  'FireBolero', 'WaterSerenade', 'SpiritRequiem', 'ShadowNocturne', 'LightPrelude', 'ScarecrowSong')] +
                 ['Weapon_Sword_Kokiri', 'Weapon_Sword_Master', 'Weapon_Sword_Giant', 'Weapon_Sword_Biggoron', 'Weapon_Shield_Deku',
                  'Weapon_Shield_Hylian', 'Weapon_Shield_Mirror', 'Armor_Adult_Goron', 'Armor_Adult_Zora', 'Armor_Adult_HeavyBoot',
                  'Armor_Adult_HoverBoot', 'Armor_Adult_Gauntlet_Silver', 'Armor_Adult_Gauntlet_Gold', 'Weapon_Range_Bow', 'Weapon_Range_SlingShot',
                  'Item_FaroreWind', 'Item_FairyBless_MP', 'Item_FairyBless_MPDouble'])
        for i, n in enumerate(names):
            v = tk.BooleanVar(value=False)
            self.flag_vars['IsGet.' + n] = v
            ttk.Checkbutton(grid, text=n.replace('Item_Melody_', 'Song: '), variable=v,
                            command=lambda n='IsGet.' + n, v=v: self.flag_set(n, v.get())).grid(row=i % 8, column=i // 8, sticky='w')
        r = ttk.Frame(sg)
        r.pack(anchor='w', pady=(4, 0))
        for label, n in (('Master Quest (IsLastSelectedMQMode, unconfirmed)', 'IsLastSelectedMQMode'),
                         ('amiibo on (IsEnableUseAmiibo)', 'IsEnableUseAmiibo'), ('amiibo field', 'IsEnableUseAmiiboField'),
                         ('amiibo option', 'OptionData.IsUseAmiibo'), ('amiibo pouch item', 'IsGet.Pouch_Amiibo')):
            v = tk.BooleanVar(value=False)
            self.flag_vars[n] = v
            ttk.Checkbutton(r, text=label, variable=v, command=lambda n=n, v=v: self.flag_set(n, v.get())).pack(side='left', padx=2)
        ttk.Button(sg, text='Read current values', command=self.flags_refresh).pack(anchor='w', pady=2)
        dg = ttk.LabelFrame(mid, text='Dungeons (raw: DungeonInfo.<dungeon>.*, HaveDefeatedEnemy_*, IsGet.Item_Medal_* / Stone_*)', padding=4)
        dg.pack(fill='x', pady=(6, 0))
        r = ttk.Frame(dg)
        r.pack(anchor='w')
        self.dg_name = ttk.Combobox(r, values=self.DUNGEONS, width=22, state='readonly')
        self.dg_name.set(self.DUNGEONS[0])
        self.dg_name.pack(side='left')
        ttk.Label(r, text='small keys').pack(side='left', padx=(6, 0))
        self.dg_keys = tk.Spinbox(r, from_=0, to=99, width=4)
        self.dg_keys.pack(side='left', padx=2)
        ttk.Button(r, text='Set keys', command=lambda: self.dg_set('SmallKeyNum', int(self.dg_keys.get()))).pack(side='left')
        r = ttk.Frame(dg)
        r.pack(anchor='w', pady=2)
        for label, key in (('boss key', 'IsGetBossKey'), ('map', 'IsGetMap'), ('compass', 'IsGetCompass')):
            ttk.Button(r, text='Give ' + label, command=lambda k=key: self.dg_set(k, True)).pack(side='left', padx=2)
        r = ttk.Frame(dg)
        r.pack(anchor='w', pady=2)
        ttk.Label(r, text='Boss defeated:').pack(side='left')
        self.dg_boss = ttk.Combobox(r, values=self.BOSSES, width=14, state='readonly')
        self.dg_boss.set(self.BOSSES[0])
        self.dg_boss.pack(side='left', padx=2)
        ttk.Button(r, text='Set defeated', command=lambda: self.flag_set('HaveDefeatedEnemy_Enemy_DungeonBoss_' + self.dg_boss.get(), True)).pack(side='left')
        r = ttk.Frame(dg)
        r.pack(anchor='w', pady=2)
        for n in ('Stone_Forest', 'Stone_Fire', 'Stone_Water', 'Medal_Forest', 'Medal_Fire', 'Medal_Water', 'Medal_Spirit', 'Medal_Shadow', 'Medal_Light'):
            ttk.Button(r, text=n.replace('_', ' '), command=lambda n=n: self.gi_raw('Item_' + n, 1)).pack(side='left', padx=1)
        qv = ttk.LabelFrame(mid, text='Quest flags (read-only: main-quest step and completion flags, grouped by prefix)', padding=4)
        qv.pack(fill='both', expand=True, pady=(6, 0))
        self.qv_tv = ttk.Treeview(qv, columns=('value',), show='tree headings', height=5)
        self.qv_tv.heading('#0', text='flag')
        self.qv_tv.heading('value', text='value')
        self.qv_tv.column('#0', width=420)
        self.qv_tv.column('value', width=80)
        ttk.Button(qv, text='Refresh (game, or the save file when not connected)', command=self.quest_refresh).pack(side='bottom', anchor='w', pady=2)
        self.qv_tv.pack(fill='both', expand=True)
        self.items_fill()

    def items_fill(self):
        L = self._core_lists()
        items = L.get('items') or [[i['name'], i.get('category'), i.get('tab')] for i in (self._load_catalog() or {}).get('items', [])]
        self.item_names = [i[0] for i in items]
        self.gi.setitems(['%-34s %s / %s' % (n, c, t) for n, c, t in items])

    def _val(self, name):
        h = gamedata.mmh(name)
        if self.live and h in self.live.by_hash:
            return self.live.vals.get(h)
        if self.save and h in self.save.by_hash:
            return self.save.get(self.save.by_hash[h])
        return None

    def _set(self, name, value):
        """Live when connected, else the loaded save file (then Save changes on World state). Returns the text for the log."""
        h = gamedata.mmh(name)
        if self.live and self.live.host and h in self.live.by_hash:
            before = self.live.vals.get(h)
            self.live.write(h, value)
            self.flog_tool(self.live.by_hash[h], before, self.live.vals.get(h), 'set by you')
            return '%s: %s -> %s (live)' % (name, fmt_val(before), fmt_val(value))
        if self.save and h in self.save.by_hash:
            self.save.set(self.save.by_hash[h], value)
            return '%s = %s (save file; click Save changes on World state)' % (name, fmt_val(value))
        raise ValueError('%s is not a live or save-file value' % name)

    def gi_raw(self, name, n):
        out = []
        try:
            out.append(self._set('IsGet.' + name, True))
            h = gamedata.mmh('PouchData.%s.StockNum' % name)
            if (self.live and h in self.live.by_hash) or (self.save and h in self.save.by_hash):
                cur = self._val('PouchData.%s.StockNum' % name) or 0
                out.append(self._set('PouchData.%s.StockNum' % name, int(cur) + int(n)))
            else:
                out.append('(no PouchData.%s.StockNum entry: flag only)' % name)
        except Exception as e:
            return self.status('give %s failed: %s' % (name, e), 'red')
        self.status('raw give %s x%d: %s' % (name, n, '; '.join(out)), 'blue')
        self.gi_lbl.configure(text='; '.join(out))

    def gi_give(self, route):
        i = self.gi.pick()
        if i is None:
            return self.status('Pick an item first.', 'red')
        name, n = self.item_names[i], int(self.gi_num.get())
        if route == 'raw':
            return self.gi_raw(name, n)
        if not self._core_lists().get('items'):
            return self.status('Core route needs DevTool Core built.', 'red')
        self.core_send(route, i, '%s in the pouch%s' % (name, ' with the item-get scene' if route == 'item_scene' else ''))

    def gi_bottle(self):
        b, item = self.bottle.get(), self.bottle_item.get()
        base = 'PlayerStatus.AssignableItem.BottleInfo.%s.' % b
        d = (self.defs or {}).get(gamedata.mmh(base + 'BottleItem'))
        if d is None or not d.raw_values or item not in d.raw_values:
            return self.status('bottle: %s has no Enum value %r' % (base + 'BottleItem', item), 'red')
        try:     # v0.8.3: same writes as the Loadout tab (Enum value + IsValid + the bottle itself)
            out = [self._set(base + 'BottleItem', d.values[d.raw_values.index(item)]), self._set(base + 'IsValid', True)]
            if not self._val('IsGet.Item_Bottle'):
                out.append(self._set('IsGet.Item_Bottle', True))
        except Exception as e:
            return self.status('bottle: %s' % e, 'red')
        self.status('%s (unconfirmed in-game: the bottle shows this content)' % '; '.join(out), 'blue')

    def flag_set(self, name, value):
        try:
            self.status(self._set(name, bool(value)), 'blue')
        except Exception as e:
            self.status('%s: %s' % (name, e), 'red')

    def flags_refresh(self):
        for n, v in self.flag_vars.items():
            val = self._val(n)
            if val is not None:
                v.set(bool(val))
        self.status('Flags read from %s.' % ('the game' if self.live else 'the save file'), 'blue')

    def dg_set(self, key, value):
        self.flag_set('DungeonInfo.%s.%s' % (self.dg_name.get(), key), value) if isinstance(value, bool) else \
            self.status(self._set('DungeonInfo.%s.%s' % (self.dg_name.get(), key), int(value)), 'blue')

    def quest_refresh(self):
        tv = self.qv_tv
        for i in tv.get_children():
            tv.delete(i)
        groups = {}
        for h, d in self.defs.items():
            if d.type not in ('Bool', 'Int', 'BoolExp'):
                continue
            n = self.names.get(h)
            if not n:
                continue
            if not any(k in n for k in self.QUEST_RX):
                continue
            g = n.split('_')[0].split('.')[0]
            groups.setdefault(g, []).append(n)
        for g in sorted(groups):
            node = tv.insert('', 'end', text='%s (%d)' % (g, len(groups[g])), open=False)
            for n in sorted(groups[g]):
                tv.insert(node, 'end', text=n, values=(fmt_val(self._val(n)),))
        self.status('Quest flags: %d groups, %d flags.' % (len(groups), sum(len(v) for v in groups.values())), 'blue')

    # ============================================================ Player tools tab (v0.8)
    def _player(self):
        return player.Player(self.romfs, os.path.join(self.data, 'load'), self.tid, self.log)

    def _tab_ptools(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='Player tools')
        ttk.Label(f, justify='left', wraplength=1250, text=(
            'v0.8, all unconfirmed. The bridge lives in the player\'s AI graph (DevTool Player mod, built below, game closed): '
            'it reads the DevTool.* values every frame, so the toggles here are plain GameData writes. A bad graph edit can '
            'crash at boot: every part is a switch; rebuild with one off at a time if the game does not boot.')).pack(anchor='w')
        body = ttk.Frame(f)
        body.pack(fill='both', expand=True, pady=4)
        left = ttk.Frame(body)
        left.pack(side='left', fill='y')
        br = ttk.LabelFrame(left, text='Bridge toggles (DevTool.* values the player graph reads)', padding=4)
        br.pack(fill='x')
        self.pt_god = tk.BooleanVar(value=False)
        ttk.Checkbutton(br, text='God mode (DevTool.God)', variable=self.pt_god,
                        command=lambda: self._set_quiet('DevTool.God', self.pt_god.get())).pack(anchor='w')
        self.pt_dbg = tk.BooleanVar(value=False)
        ttk.Checkbutton(br, text='Debug move (DevTool.DebugMove; retail stub?)', variable=self.pt_dbg,
                        command=lambda: self._set_quiet('DevTool.DebugMove', self.pt_dbg.get())).pack(anchor='w')
        r = ttk.Frame(br)
        r.pack(anchor='w', pady=2)
        mj = ttk.Button(r, text='Hold: moon jump (DevTool.MoonJump)')
        mj.pack(side='left')
        mj.bind('<ButtonPress-1>', lambda e: self._set_quiet('DevTool.MoonJump', True))
        mj.bind('<ButtonRelease-1>', lambda e: self._set_quiet('DevTool.MoonJump', False))
        self.pt_moon = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='latch on', variable=self.pt_moon, command=lambda: self._set_quiet('DevTool.MoonJump', self.pt_moon.get())).pack(side='left', padx=6)
        r = ttk.Frame(br)
        r.pack(anchor='w', pady=2)
        ttk.Label(r, text='Gravity scale (DevTool.F0, 1 = normal)').pack(side='left')
        self.pt_grav = tk.Spinbox(r, from_=0.0, to=5.0, increment=0.1, width=5)
        self.pt_grav.delete(0, 'end')
        self.pt_grav.insert(0, '1.0')
        self.pt_grav.pack(side='left', padx=4)
        ttk.Button(r, text='Set', width=5, command=lambda: self._set_quiet('DevTool.F0', float(self.pt_grav.get()))).pack(side='left')
        ttk.Button(r, text='Normal', width=7, command=lambda: (self.pt_grav.delete(0, 'end'), self.pt_grav.insert(0, '1.0'), self._set_quiet('DevTool.F0', 1.0))).pack(side='left', padx=2)
        self.pt_inf = tk.BooleanVar(value=False)
        ttk.Checkbutton(br, text='Infinite magic and ammo (freezes at max / current counts)',
                        variable=self.pt_inf, command=self.pt_infinite).pack(anchor='w')
        tp = ttk.LabelFrame(left, text='Teleport (B2)', padding=4)
        tp.pack(fill='x', pady=(6, 0))
        r = ttk.Frame(tp)
        r.pack(anchor='w')
        self.tp_xyz = []
        for ax in 'XYZ':
            ttk.Label(r, text=ax).pack(side='left')
            e = ttk.Entry(r, width=9)
            e.insert(0, '0')
            e.pack(side='left', padx=(0, 6))
            self.tp_xyz.append(e)
        ttk.Button(r, text='Copy my position', command=self.tp_copy).pack(side='left')
        self.tp_route = tk.StringVar(value='bridge')
        r = ttk.Frame(tp)
        r.pack(anchor='w', pady=2)
        ttk.Radiobutton(r, text='(a) raw position map change (costs a load)', variable=self.tp_route, value='raw').pack(anchor='w')
        ttk.Radiobutton(r, text='(b) bridge ForceSetPos (instant, same map, Cmd 30)', variable=self.tp_route, value='bridge').pack(anchor='w')
        ttk.Radiobutton(r, text="(c) Farore's Wind point (then cast the spell)", variable=self.tp_route, value='fw').pack(anchor='w')
        r = ttk.Frame(tp)
        r.pack(anchor='w', pady=2)
        ttk.Button(r, text='Teleport', command=self.tp_go).pack(side='left')
        ttk.Button(r, text='Go to the selected bookmark', command=self.tp_bookmark).pack(side='left', padx=4)
        bm = ttk.LabelFrame(left, text='Position bookmarks (shared with the Warp tab)', padding=4)
        bm.pack(fill='both', expand=True, pady=(6, 0))
        self.pt_bm = tk.Listbox(bm, height=5, width=52, exportselection=False, font=('Consolas', 9))
        self.pt_bm.pack(fill='both', expand=True)
        r = ttk.Frame(bm)
        r.pack(anchor='w', pady=2)
        ttk.Button(r, text="Bookmark Link's position", command=lambda: (self.bm_add(), self.pt_bm_fill())).pack(side='left')
        ttk.Button(r, text='Delete', command=self.pt_bm_del).pack(side='left', padx=4)
        mid = ttk.Frame(body)
        mid.pack(side='left', fill='y', padx=10)
        pm = ttk.LabelFrame(mid, text='DevTool Player mod (game closed)', padding=4)
        pm.pack(fill='x')
        self.pt_parts = {}
        for key, text in player.PARTS:
            v = tk.BooleanVar(value=True)
            self.pt_parts[key] = v
            ttk.Checkbutton(pm, text='%s: %s' % (key, text), variable=v).pack(anchor='w')
        r = ttk.Frame(pm)
        r.pack(anchor='w', pady=2)
        ttk.Label(r, text='Speed x (GroundSpeedScale)').pack(side='left')
        self.pt_speed = tk.Spinbox(r, from_=0.1, to=10.0, increment=0.1, width=5)
        self.pt_speed.delete(0, 'end')
        self.pt_speed.insert(0, '1.0')
        self.pt_speed.pack(side='left', padx=4)
        self.pt_nofall = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='no fall damage (LargeLandHeight)', variable=self.pt_nofall).pack(side='left', padx=6)
        r = ttk.Frame(pm)
        r.pack(anchor='w', pady=2)
        ttk.Button(r, text='Build DevTool Player (+ merged RESTBL)', command=self.pt_build).pack(side='left')
        ttk.Button(r, text='Remove', command=self.pt_remove).pack(side='left', padx=4)
        self.pt_status = ttk.Label(pm, text='', wraplength=360, justify='left')
        self.pt_status.pack(anchor='w')
        ss = ttk.LabelFrame(mid, text='Save states (slot snapshot + map + position + time, max 10)', padding=4)
        ss.pack(fill='both', expand=True, pady=(6, 0))
        r = ttk.Frame(ss)
        r.pack(anchor='w')
        ttk.Label(r, text='Name').pack(side='left')
        self.st_name = ttk.Entry(r, width=18)
        self.st_name.pack(side='left', padx=4)
        ttk.Label(r, text='slot').pack(side='left')
        self.st_slot = ttk.Combobox(r, width=8, state='readonly', values=self._slot_names())
        if self.st_slot['values']:
            self.st_slot.set(self.st_slot['values'][0])
        self.st_slot.pack(side='left', padx=4)
        self.st_list = tk.Listbox(ss, height=6, exportselection=False, font=('Consolas', 9))
        self.st_list.pack(fill='both', expand=True, pady=2)
        r = ttk.Frame(ss)
        r.pack(anchor='w')
        ttk.Button(r, text='Snapshot state', command=self.st_snapshot).pack(side='left')
        ttk.Button(r, text='Restore state', command=self.st_restore).pack(side='left', padx=4)
        ttk.Button(r, text='Delete', command=self.st_delete).pack(side='left')
        ttk.Label(ss, foreground=C['dim'], justify='left', wraplength=360, text=(
            'Restore puts the .sav files back (backup first; the game reads a slot when you load it from the title) '
            'and, when connected, reloads the map at the saved position.')).pack(anchor='w')
        right = ttk.Frame(body)
        right.pack(side='left', fill='both', expand=True)
        ib = ttk.LabelFrame(right, text='Info bar extras (C5)', padding=4)
        ib.pack(fill='x')
        self.pt_fps = tk.BooleanVar(value=True)
        ttk.Checkbutton(ib, text='FPS in the info bar (debug server)', variable=self.pt_fps).pack(anchor='w')
        ttk.Label(ib, foreground=C['dim'], wraplength=300, justify='left', text=(
            'Actor count: neither the debug server nor GameData exposes one.')).pack(anchor='w')
        r = ttk.Frame(ib)
        r.pack(anchor='w', pady=2)
        self.split_lbl = ttk.Label(r, text='00:00.0', font=('Consolas', 14, 'bold'))
        self.split_lbl.pack(side='left', padx=(0, 8))
        ttk.Button(r, text='Split (F9)', width=10, command=self.split_hit).pack(side='left')
        ttk.Button(r, text='Reset (F10)', width=11, command=self.split_reset).pack(side='left', padx=4)
        self.split_box = tk.Listbox(ib, height=5, font=('Consolas', 9))
        self.split_box.pack(fill='x')
        self.split_t0 = None
        self.split_last = None
        self.root.bind('<F9>', lambda e: self.split_hit())
        self.root.bind('<F10>', lambda e: self.split_reset())
        ov = ttk.LabelFrame(right, text='Overlay test (E4)', padding=4)
        ov.pack(fill='x', pady=(6, 0))
        ttk.Button(ov, text='Show a text box (Core message 0)', command=lambda: self.core_send('message', 0, 'a system text box')).pack(anchor='w')
        ttk.Label(ov, foreground=C['dim'], wraplength=300, justify='left', text=(
            'The event API has no free-text action: SystemTextNPC::EventTalk shows an existing message ID (the Core '
            'message list). A debug HUD with our own text needs the code side (v1.2).')).pack(anchor='w')
        self.pt_bm_fill()
        self.st_fill()
        self.pt_refresh()
        self.root.after(2000, self._fps_tick)

    def _set_quiet(self, name, value):
        try:
            self.status(self._set(name, value), 'blue')
        except Exception as e:
            self.status('%s: %s (Core GameDataList part not loaded?)' % (name, e), 'red')

    def pt_refresh(self):
        st = self._player().status()
        self.pt_status.configure(text=('built %s: parts %s, speed x%g%s' % (st['built'], '+'.join(st['parts']), st.get('speed', 1),
                                                                            ', no fall damage' if st.get('no_fall') else '')) if st else 'not built')

    def pt_build(self):
        if lichii.lichii_pids():
            return self.status('Close the game and Lichii first (mod files are locked while it runs).', 'red')
        parts = [k for k, v in self.pt_parts.items() if v.get()]
        speed, nofall = float(self.pt_speed.get()), self.pt_nofall.get()
        pl = self._player()
        rf = gamedata.RomFS(self.romfs)
        research = os.path.join(HERE, 'docs', 'research')
        self.status('Building DevTool Player (%s, speed x%g%s)...' % ('+'.join(parts) or 'no bridge', speed, ', no fall' if nofall else ''), 'blue')

        def work():
            rep = pl.build(parts, speed, nofall, research)
            mods.rebuild_restbl(self.data, self.romfs, rf, self.tid, log=self.log)
            return rep

        def done(rep):
            self.status('DevTool Player built: %d packs, %d checks passed, %d graph nodes added (listed in docs\\research\\player_bridge_nodes.json); merged RESTBL updated.'
                        % (len(rep['files']), len(rep['checks']), len(rep['added_nodes'])), 'darkgreen')
            self.pt_refresh()
            self.mods_refresh()
        self.bg(work, done)

    def pt_remove(self):
        if lichii.lichii_pids():
            return self.status('Close the game and Lichii first.', 'red')
        if self._player().remove():
            self.status('DevTool Player removed.', 'blue')
        self.pt_refresh()
        self.mods_refresh()

    def pt_infinite(self):
        if not self.live or not self.live.host:
            self.pt_inf.set(False)
            return self.status('Connect first (with write access).', 'red')
        names = ['PlayerStatus.MP'] + ['PouchData.%s.StockNum' % n for n in ('Item_DekuStick', 'Item_DekuNut', 'Item_DekuSeed', 'Item_Arrow_Normal', 'Item_Bomb', 'Item_Bombchu')]   # v0.8.3: was Item_Arrow (no such entry)
        H = gamedata.mmh
        if self.pt_inf.get():
            n = 0
            for nm in names:
                h = H(nm)
                if h not in self.live.by_hash:
                    continue
                v = self.live.vals.get(H('PlayerStatus.MPMax')) if nm == 'PlayerStatus.MP' else self.live.vals.get(h)
                if v is None:
                    continue
                try:
                    self.live.write(h, v)
                except Exception:
                    continue
                self.frozen[h] = v
                self.tool_touched[h] = time.time()
                n += 1
            self.status('Infinite: %d values frozen (magic at max, ammo at the current counts). Note: during play Link overwrites '
                        'Magic every frame, so the magic part may only hold after the next loading screen.' % n, 'blue')
        else:
            for nm in names:
                self.frozen.pop(H(nm), None)
            self.status('Infinite magic/ammo off (freezes released).', 'blue')
        self._freeze_note()

    def tp_copy(self):
        pos = self._val('Player_Pos_For_Map') if self.live else None
        if not isinstance(pos, tuple):
            return self.status("Link's position is not known (connect first).", 'red')
        for e, v in zip(self.tp_xyz, pos):
            e.delete(0, 'end')
            e.insert(0, '%.3f' % v)
        self.status('Position copied: %.2f, %.2f, %.2f' % pos, 'blue')

    def _tp_target(self):
        try:
            return tuple(float(e.get()) for e in self.tp_xyz)
        except ValueError:
            return None

    def tp_go(self, pos=None, banc=None, yaw=None):
        pos = pos or self._tp_target()
        if pos is None:
            return self.status('X, Y, Z must be numbers.', 'red')
        if not self.live or not self.live.host:
            return self.status('Connect first (with write access).', 'red')
        route = self.tp_route.get()
        cur = self.live.vals.get(gamedata.mmh('Sequence_CurrentBanc'))
        banc = banc or cur
        if yaw is None:
            yaw = self.live.vals.get(gamedata.mmh('PlayerStatus.SavePosRadY')) or 0.0
        if route == 'raw':
            return self.raw_warp(banc, pos=pos, yaw=yaw, boot='FaroreWindArrive', fade='WhiteFade', how='teleport')
        if route == 'fw':
            if not self._fw_ok():
                return
            self._fw_set(banc, '', pos, yaw)
            return self.status("Farore's Wind point set to %s %s: cast the spell and choose Return." % (banc, fmt_val(tuple(pos))), 'blue')
        if banc != cur:
            return self.status('The bridge route works on the current map only (%s); the bookmark is in %s: use route (a) or (c).' % (cur, banc), 'red')
        try:
            self._set('DevTool.Pos', tuple(pos))
        except Exception as e:
            return self.status('DevTool.Pos: %s (Core GameDataList part not loaded?)' % e, 'red')
        self.core_send('setpos', 0, 'Link at %s (the bridge; Ack comes from the player graph, not the event)' % fmt_val(tuple(pos)))

    def pt_bm_fill(self):
        self.pt_bm.delete(0, 'end')
        for b in self.bookmarks:
            self.pt_bm.insert('end', '%-20s %-26s (%.1f, %.1f, %.1f)' % (b['name'][:20], b['banc'][:26], *b['pos']))

    def pt_bm_del(self):
        sel = self.pt_bm.curselection()
        if sel:
            self.bm_list.selection_clear(0, 'end')
            self.bm_list.selection_set(sel[0])
            self.bm_del()
            self.pt_bm_fill()

    def tp_bookmark(self):
        sel = self.pt_bm.curselection()
        if not sel:
            return self.status('Pick a bookmark first.', 'red')
        b = self.bookmarks[sel[0]]
        self.tp_go(tuple(b['pos']), b['banc'], b.get('yrad', 0.0))

    # ---- save states
    def _states_root(self):
        d = os.path.join(USER, 'states')
        os.makedirs(d, exist_ok=True)
        return d

    def st_fill(self):
        self.st_list.delete(0, 'end')
        self.states = []
        for d in sorted(os.listdir(self._states_root())):
            p = os.path.join(self._states_root(), d, 'state.json')
            if os.path.isfile(p):
                try:
                    m = json.load(open(p))
                except Exception:
                    continue
                self.states.append((d, m))
                self.st_list.insert('end', '%-18s %-22s %s  %s' % (m.get('name', d)[:18], (m.get('banc') or '?')[:22], m.get('clock', ''), m.get('time', '')))

    def st_snapshot(self):
        import shutil
        slot = self.st_slot.get()
        if not slot or not self.saves:
            return self.status('No save slot known.', 'red')
        if len(self.states) >= 10:
            return self.status('10 states kept already: delete one first.', 'red')
        nm = self.st_name.get().strip() or time.strftime('state %H:%M:%S')
        safe = ''.join(ch if ch.isalnum() or ch in '-_ ' else '_' for ch in nm).strip() or 'state'
        dst = os.path.join(self._states_root(), '%s_%s' % (time.strftime('%Y%m%d_%H%M%S'), safe))
        src = os.path.join(self.saves[0], slot)
        os.makedirs(dst)
        for fn in os.listdir(src):
            if fn.endswith('.sav'):
                shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
        meta = {'name': nm, 'slot': slot, 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'build_id': self.bid}
        if self.live:
            banc, loc, pos, yrad = self._link_place()
            meta.update({'banc': banc, 'pos': list(pos) if isinstance(pos, tuple) else None, 'yrad': yrad,
                         'clock': gdlive.fmt_datetime(self._info_val('time')), 'datetime': self._info_val('time'),
                         'child': bool(self._info_val('child'))})
        json.dump(meta, open(os.path.join(dst, 'state.json'), 'w'), indent=1)
        self.st_fill()
        self.status('State "%s" kept (%s%s). The slot files are a copy of what is on disk now: save in-game first if you want the '
                    'latest progress in it.' % (nm, slot, ', %s %s' % (meta.get('banc'), fmt_val(tuple(meta['pos']))) if meta.get('pos') else ''), 'blue')

    def st_restore(self):
        import shutil
        sel = self.st_list.curselection()
        if not sel:
            return self.status('Pick a state first.', 'red')
        d, m = self.states[sel[0]]
        src = os.path.join(self._states_root(), d)
        slot = m.get('slot') or self.st_slot.get()
        dst = os.path.join(self.saves[0], slot)
        bak = os.path.join(USER, 'backups', '%s_%s_before_state' % (time.strftime('%Y%m%d_%H%M%S'), slot))
        if os.path.isdir(dst):
            shutil.copytree(dst, bak)
        os.makedirs(dst, exist_ok=True)
        for fn in os.listdir(src):
            if fn.endswith('.sav'):
                shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
        msg = 'State "%s" restored into %s (previous files in %s).' % (m.get('name', d), slot, bak)
        if self.live and self.live.host and m.get('banc') and m.get('pos'):
            try:
                if isinstance(m.get('datetime'), int):
                    self._set('World_CurrentDateTime', m['datetime'])
            except Exception:
                pass
            self.raw_warp(m['banc'], pos=m['pos'], yaw=m.get('yrad', 0.0), boot='FaroreWindArrive', fade='BlackFade', how='restore state')
            msg += ' Reloading %s at the saved position (raw position route).' % m['banc']
        else:
            msg += ' Load the slot from the title screen to use it.'
        self.status(msg, 'blue')

    def st_delete(self):
        import shutil
        sel = self.st_list.curselection()
        if not sel:
            return
        d, m = self.states[sel[0]]
        shutil.rmtree(os.path.join(self._states_root(), d), ignore_errors=True)
        self.st_fill()

    # ---- split timer and FPS
    def split_hit(self):
        now = time.time()
        if self.split_t0 is None:
            self.split_t0 = self.split_last = now
            self.split_box.delete(0, 'end')
            self.split_lbl.configure(text='00:00.0')
            return
        seg, tot = now - self.split_last, now - self.split_t0
        self.split_last = now
        self.split_box.insert('end', 'split %d: %6.1f s   total %6.1f s' % (self.split_box.size() + 1, seg, tot))
        self.split_box.see('end')
        self.log('[split] %d: +%.1f s, total %.1f s' % (self.split_box.size(), seg, tot))

    def split_reset(self):
        self.split_t0 = self.split_last = None
        self.split_lbl.configure(text='00:00.0')
        self.split_box.delete(0, 'end')

    def _fps_tick(self):
        if self.split_t0 is not None:
            t = time.time() - self.split_t0
            self.split_lbl.configure(text='%02d:%04.1f' % (t // 60, t % 60))
            self.root.after(100, self._fps_tick)
            return
        if self.dbg and self.pt_fps.get():
            def done(st):
                self.fps_text = '%s fps' % st.get('fps') if isinstance(st, dict) else ''
            self.bg(lambda: self.dbg.status(), done)
        self.root.after(2000, self._fps_tick)

    # ============================================================ Mod manager tab (v0.6)
    def _tab_mods(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text='Mod manager')
        top = ttk.Frame(f)
        top.pack(fill='x')
        self.mods_lbl = ttk.Label(top, text=mods.load_dir(self.data, self.tid))
        self.mods_lbl.pack(side='left')
        ttk.Button(top, text='Refresh', command=self.mods_refresh).pack(side='right')
        ttk.Button(top, text='Open folder', command=lambda: self._open(mods.load_dir(self.data, self.tid))).pack(side='right', padx=4)
        cols = ('on', 'name', 'files', 'restbl', 'note')
        self.mods_tv = ttk.Treeview(f, columns=cols, show='headings', height=6, selectmode='browse')
        for c, w, t in (('on', 50, 'On'), ('name', 260, 'Mod folder (Lichii layers them in this order; first wins)'),
                        ('files', 60, 'Files'), ('restbl', 60, 'RESTBL'), ('note', 520, 'Note')):
            self.mods_tv.heading(c, text=t)
            self.mods_tv.column(c, width=w, anchor='w')
        self.mods_tv.pack(fill='x', pady=4)
        self.mods_tv.bind('<Double-1>', lambda e: self.mods_toggle())
        r = ttk.Frame(f)
        r.pack(fill='x')
        ttk.Button(r, text='Enable / disable selected (Lichii closed)', command=self.mods_toggle).pack(side='left')
        ttk.Button(r, text='Check conflicts', command=self.mods_conflicts).pack(side='left', padx=4)
        ttk.Button(r, text='Rebuild merged RESTBL', command=self.mods_restbl).pack(side='left', padx=4)
        ttk.Button(r, text='What loaded? (Lichii log)', command=self.mods_loaded).pack(side='left', padx=4)
        b = ttk.LabelFrame(f, text='Build the tool\'s own mods (game closed: Lichii locks mod files while it runs)', padding=6)
        b.pack(fill='x', pady=(8, 4))
        r1 = ttk.Frame(b)
        r1.pack(fill='x')
        self.fog_panel = fog.FogPanel(self, b, r1)   # v0.8.3: the Fog section (fog.py), compact row + sliders on demand
        ttk.Label(r1, text='Perf preset').pack(side='left')
        self.perf_preset = ttk.Combobox(r1, values=mods.PERF_PRESETS, width=10, state='readonly')
        self.perf_preset.set('balanced')
        self.perf_preset.pack(side='left', padx=4)
        ttk.Button(r1, text='Build perf mod', command=lambda: self.mods_build('perf')).pack(side='left', padx=4)
        ttk.Label(r1, text='   Camera: see the Camera tab (Build camera mod)').pack(side='left')
        r2 = ttk.LabelFrame(b, text='DevTool Core (Engine 2): each part is a switch, so a crash at boot can be bisected', padding=4)
        r2.pack(fill='x', pady=(6, 0))
        self.core_parts = {}
        for key, text in core.PARTS:
            v = tk.BooleanVar(value=True)
            self.core_parts[key] = v
            ttk.Checkbutton(r2, text='%s: %s' % (key, text), variable=v).pack(anchor='w')
        self.core_reuse = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text='Fallback: reuse existing non-saved entries (%s) instead of new GameDataList entries (unconfirmed)'
                        % ', '.join(core.REUSE.values()), variable=self.core_reuse).pack(anchor='w')
        self.core_evp = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text='v0.7 event player: 2,000+ sub-flow cases in the DevTool flow (riskier: the event system may load every '
                        'flowchart; turn on once the hello command works)', variable=self.core_evp).pack(anchor='w')
        self.core_hello = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text='v0.8.3 bisect: hello only (a tiny DevTool flow with just the +1 rupee case; every other Core '
                        'command is off). Use it if the hello command fails with the full flow', variable=self.core_hello).pack(anchor='w')
        r3 = ttk.Frame(r2)
        r3.pack(fill='x', pady=(4, 0))
        ttk.Button(r3, text='Build DevTool Core (+ merged RESTBL)', command=self.core_build).pack(side='left')
        ttk.Button(r3, text='Remove DevTool Core', command=self.core_remove).pack(side='left', padx=4)
        self.core_status = ttk.Label(r3, text='')
        self.core_status.pack(side='left', padx=8)
        self.mods_txt = tk.Text(f, height=8, font=('Consolas', 9))
        self.mods_txt.pack(fill='both', expand=True, pady=4)
        self.mods_txt.insert('end', 'How mods load (read from Lichii\'s source, PatchManager::ApplyLayeredFS): every folder under the load\n'
                             'folder with a romfs\\ inside is a layer. Lichii sorts the folders by NAME (byte order) and the\n'
                             'game gets each file from the FIRST folder that has it; vanilla comes last. So two mods that ship\n'
                             'the same file are a conflict (the first wins) and two RESTBLs are a conflict: "Rebuild merged RESTBL"\n'
                             'writes one table for everything into "%s", which sorts first. Enable/disable uses Lichii\'s own\n'
                             'Add-Ons list in config\\qt-config.ini, which Lichii rewrites when it closes: toggle with Lichii closed.\n'
                             'Core parts: a boot crash means a part is wrong; turn them off one at a time (rebuild each time).\n' % mods.restbl.MERGED_MOD)
        self.mods_refresh()

    def _mods_say(self, text):
        self.mods_txt.insert('end', text + '\n')
        self.mods_txt.see('end')
        self.log(text)

    def mods_refresh(self):
        try:
            self.mod_list = mods.list_mods(self.data, self.tid)
        except Exception as e:
            self.mod_list = []
            self.status('Mod list: %s' % e, 'red')
        for i in self.mods_tv.get_children():
            self.mods_tv.delete(i)
        conf = {}
        for kind, text in mods.conflicts(self.mod_list):
            if kind == 'note':
                continue
            for m in self.mod_list:
                if '"%s"' % m['name'] in text or m['name'] in text:
                    conf[m['name']] = kind
        for m in self.mod_list:
            note = 'tool-built' if m['own'] else ''
            if m['exefs']:
                note += (', ' if note else '') + 'code patches / cheats'
            if m['name'] in conf:
                note += (', ' if note else '') + 'CONFLICT (%s)' % conf[m['name']]
            self.mods_tv.insert('', 'end', iid=m['name'], values=('on' if m['enabled'] else 'OFF', m['name'],
                                                                    len(m['files']), len(m['restbl']), note))
        st = self._core().status()
        self.core_status.configure(text=('built %s: parts %s%s' % (st['built'], '+'.join(st['parts']), ', reuse' if st.get('reuse') else '')
                                         if st else 'not built'))

    def mods_toggle(self):
        sel = self.mods_tv.selection()
        if not sel:
            return
        name = sel[0]
        if lichii.lichii_pids():
            return self.status('Close Lichii first: it rewrites its Add-Ons list when it closes.', 'red')
        m = next(x for x in self.mod_list if x['name'] == name)
        try:
            off = mods.set_enabled(self.data, name, not m['enabled'])
        except Exception as e:
            return self.status('Could not change the Add-Ons list: %s' % e, 'red')
        self._mods_say('%s %s. Disabled now: %s' % ('Disabled' if m['enabled'] else 'Enabled', name, ', '.join(off) or '(none)'))
        self.mods_refresh()
        self.mods_restbl()
        if name == core.MOD_NAME:
            self.bg(self._load_gamedata, self._after_gamedata)

    def mods_conflicts(self):
        cs = mods.conflicts(self.mod_list)
        if not any(k != 'note' for k, _t in cs):
            self._mods_say('No conflicts among the enabled mods.')
        for kind, text in cs:
            self._mods_say(('Note: %s' % text) if kind == 'note' else 'CONFLICT (%s): %s' % (kind, text))

    def mods_restbl(self):
        rf = gamedata.RomFS(self.romfs)

        def done(r):
            path, changes, notes = r
            self._mods_say('Merged RESTBL written: %s (%d mod entries, %d Core entries%s)' % (
                path, len(changes), len(notes), '; ' + '; '.join(notes) if notes else ''))
            self.mods_refresh()
        self.bg(lambda: mods.rebuild_restbl(self.data, self.romfs, rf, self.tid, log=self.log), done)

    def mods_loaded(self):
        seen, lines = mods.loaded_from_log(self.data, self.mod_list)
        on = [m['name'] for m in self.mod_list if m['enabled'] and m['files']]
        if seen:
            self._mods_say('Last boot: Lichii applied LayeredFS. Enabled RomFS mods at that time (Lichii logs no per-mod line): %s'
                           % (', '.join(on) or '(none)'))
        else:
            self._mods_say('Last boot: no "LayeredFS patches applied" line in the Lichii log: RomFS mods did NOT load '
                           '(booting the .nup instead of the loose dump, or no enabled mod has a romfs folder).')
        for ln in lines[:12]:
            self._mods_say('  ' + ln)

    def mods_build(self, which):
        if lichii.lichii_pids():
            return self.status('Close the game and Lichii first (mod files are locked while it runs).', 'red')
        preset = (self.fog_preset if which == 'fog' else self.perf_preset).get()
        self.status('Building the %s mod (%s)...' % (which, preset), 'blue')
        fn = mods.build_fog if which == 'fog' else mods.build_perf

        def done(out):
            self.status('%s mod built (%s). Enable it in the list, then Rebuild merged RESTBL.' % (which, preset), 'darkgreen')
            self.mods_refresh()
        self.bg(lambda: fn(self.romfs, self.data, preset, self.tid, self.log), done)

    def core_build(self):
        if lichii.lichii_pids():
            return self.status('Close the game and Lichii first (mod files are locked while it runs).', 'red')
        parts = [k for k, v in self.core_parts.items() if v.get()]
        reuse = self.core_reuse.get()
        self.status('Building DevTool Core (%s%s)...' % ('+'.join(parts), ', reuse' if reuse else ''), 'blue')
        c = self._core()
        rf = gamedata.RomFS(self.romfs)

        evp = self.core_evp.get()
        hello = self.core_hello.get()

        def work():
            rep = c.build(parts, reuse, evp, hello)
            mods.rebuild_restbl(self.data, self.romfs, rf, self.tid, core_report=rep, log=self.log)
            return rep

        def done(rep):
            self.status('DevTool Core built%s: %d files, %d checks passed, merged RESTBL updated. Boot the loose dump and watch the title screen.'
                        % (' (hello only)' if rep.get('hello_only') else '', len(rep['files']), len(rep['checks'])), 'darkgreen')
            self.mods_refresh()
            self.world_fill()
            self.items_fill()
            self.bg(self._load_gamedata, self._after_gamedata)
        self.bg(work, done)

    def core_remove(self):
        if lichii.lichii_pids():
            return self.status('Close the game and Lichii first.', 'red')
        if self._core().remove():
            self.status('DevTool Core removed. Rebuild the merged RESTBL (it still has the Core sizes; harmless but stale).', 'blue')
        self.mods_refresh()
        self.bg(self._load_gamedata, self._after_gamedata)

    def core_test(self):
        self.core_send('hello', 0, '+1 rupee')

    def _core_names(self):
        st = self._core().status() or {}
        return st, (st.get('names') or core.names(False))

    def core_send(self, cmd_key, arg0=0, what='', done=None):
        """Write Arg0 then Cmd, wait for Ack (polled 10x a second). One clear message per outcome.
        done(ok) is called at the end when given."""
        if not self.live or not self.live.host:
            return self.status('Connect first (with write access).', 'red')
        st, nm = self._core_names()
        cmd = (st.get('cmds') or core.CMDS).get(cmd_key)
        if cmd is None:
            return self.status('The built Core has no "%s" command: rebuild DevTool Core (v0.7).' % cmd_key, 'red')
        H = gamedata.mmh
        ents = {k: self.live.by_hash.get(H(nm[k])) for k in ('Cmd', 'Arg0', 'Ack')}
        if not all(ents.values()):
            missing = [nm[k] for k, e in ents.items() if not e]
            return self.status('%s not in the running game\'s GameData: the Core GameDataList part is not loaded '
                               '(not built, disabled, booted from the .nup, or the game rejected the new entries).' % ', '.join(missing), 'red')
        if getattr(self, 'core_busy', False):
            return self.status('A Core command is still waiting for its Ack.', '#b06000')
        self.core_busy = True
        self.live.write(ents['Ack'].hash, 0)
        self.live.write(ents['Arg0'].hash, int(arg0))
        self.live.write(ents['Cmd'].hash, cmd)
        self.core_lbl.configure(text='Cmd %d (%s) written, waiting for Ack...' % (cmd, cmd_key))
        self.status('Core command %s (Cmd %d, Arg0 %d) sent: expect %s.' % (cmd_key, cmd, arg0, what or 'the effect'), 'blue')
        self.log('[core] sent %s: Cmd=%d Arg0=%d' % (cmd_key, cmd, arg0))
        t0 = time.time()

        def finish(ok, text, color):
            self.core_busy = False
            self.status(text, color)
            if done:
                done(ok)

        def poll():
            try:
                ack = self.live.read(ents['Ack'].hash)
                cur = self.live.read(ents['Cmd'].hash)
            except Exception as e:
                return finish(False, 'Core command: read failed: %s' % e, 'red')
            dt = time.time() - t0
            if ack == core.Core.ACK_REQUESTED:
                # the Sequence hook saw Cmd and asked for the event (v0.8.2); now the event itself has to run
                self.core_lbl.configure(text='hook saw Cmd %d after %.1f s, waiting for the event...' % (cmd, dt))
                if dt > 8:
                    for k in ('Cmd', 'Ack'):
                        try:
                            self.live.write(ents[k].hash, 0)
                        except Exception:
                            pass
                    return finish(False, 'The Sequence hook ran (Ack %d) but the DevTool event never answered in %.0f s (Cmd and '
                                  'Ack cleared): the event was refused or its EventSetting row / flow is wrong.' % (ack, dt), 'red')
                return self.root.after(100, poll)
            if ack:
                self.core_lbl.configure(text='Ack %s after %.1f s' % (ack, dt))
                return finish(True, 'Ack seen after %.1f s for %s: the Sequence hook and the DevTool event both ran. %s'
                              % (dt, cmd_key, ('Check: ' + what) if what else ''), 'darkgreen')
            if cur == 0 and dt > 0.5:
                self.core_lbl.configure(text='Cmd cleared, no Ack')
                return finish(False, 'Cmd was cleared without an Ack after %.1f s (%s): the event ran but the Ack write failed, '
                              'or the game reset the struct. Check the flag log for DevTool.*' % (dt, cmd_key), '#b06000')
            if dt > 8:
                self.core_lbl.configure(text='no Ack in 8 s')
                try:
                    self.live.write(ents['Cmd'].hash, 0)
                except Exception:
                    pass
                return finish(False, 'Ack never seen (Cmd still %s after 8 s, now cleared): the Sequence hook did not start the '
                              'event (MainUpdate part not loaded, event refused in this state, or the EventSetting row is wrong).' % cur, 'red')
            self.root.after(100, poll)
        self.root.after(100, poll)

    def _cheat_path(self):
        return os.path.join(self.data, 'load', self.tid, 'DevTool', 'cheats', '%s.txt' % self.bid[:16])

    def _tab_cheats(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text='Cheats')
        top = ttk.Frame(f)
        top.pack(fill='x')
        self.cheat_lbl = ttk.Label(top, text=self._cheat_path())
        self.cheat_lbl.pack(side='left')
        ttk.Button(top, text='Save', command=self.save_cheats).pack(side='right')
        ttk.Button(top, text='Open folder', command=lambda: self._open(os.path.dirname(self._cheat_path()))).pack(side='right', padx=4)
        self.cheat_txt = tk.Text(f, font=('Consolas', 10), undo=True)
        self.cheat_txt.pack(fill='both', expand=True, pady=4)
        p = self._cheat_path()
        self.cheat_txt.insert('end', open(p, encoding='utf-8').read() if os.path.exists(p) else CHEAT_TEMPLATE)

    def save_cheats(self):
        p = self._cheat_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w', encoding='utf-8').write(self.cheat_txt.get('1.0', 'end-1c'))
        self.log('Cheats saved to %s - restart the game to load them.' % p)


# ------------------------------------------------------------ dark theme
C = dict(bg='#1b1d22', panel='#23262d', field='#2b2f37', hover='#343943', border='#3a3f4a',
         fg='#e3e6eb', dim='#9aa3b2', sel='#2f5d9e', accent='#4c8dff', warn='#ffb454')


def dark_titlebar(win):
    """Ask Windows 10/11 for a dark title bar on a Tk window."""
    if not lichii.IS_WIN:
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
    except Exception:
        pass


def apply_dark(root):
    st = ttk.Style(root)
    st.theme_use('clam')
    st.configure('.', background=C['bg'], foreground=C['fg'], fieldbackground=C['field'],
                 bordercolor=C['border'], darkcolor=C['bg'], lightcolor=C['bg'], troughcolor=C['bg'],
                 selectbackground=C['sel'], selectforeground=C['fg'], insertcolor=C['fg'],
                 arrowcolor=C['fg'], focuscolor=C['accent'])
    st.map('.', foreground=[('disabled', C['dim'])])
    st.configure('TButton', background=C['field'], padding=(9, 3))
    st.configure('Pad.TButton', padding=(0, 3))
    st.map('TButton', background=[('pressed', C['sel']), ('active', C['hover'])])
    st.configure('TNotebook', background=C['bg'], tabmargins=(4, 4, 4, 0))
    st.configure('TNotebook.Tab', background=C['panel'], foreground=C['dim'], padding=(14, 5))
    st.map('TNotebook.Tab', background=[('selected', C['field']), ('active', C['hover'])],
           foreground=[('selected', C['fg'])])
    st.configure('Treeview', background=C['panel'], fieldbackground=C['panel'], foreground=C['fg'],
                 rowheight=22)
    st.map('Treeview', background=[('selected', C['sel'])], foreground=[('selected', C['fg'])])
    st.configure('Treeview.Heading', background=C['field'], foreground=C['fg'], relief='flat')
    st.map('Treeview.Heading', background=[('active', C['hover'])])
    st.configure('TEntry', fieldbackground=C['field'], foreground=C['fg'], insertcolor=C['fg'])
    st.map('TEntry', fieldbackground=[('readonly', C['panel'])])
    st.configure('TCombobox', fieldbackground=C['field'], background=C['field'], foreground=C['fg'])
    st.map('TCombobox', fieldbackground=[('readonly', C['field'])], foreground=[('readonly', C['fg'])],
           selectbackground=[('readonly', C['field'])], selectforeground=[('readonly', C['fg'])],
           background=[('active', C['hover'])])
    st.configure('TLabelframe', background=C['bg'], bordercolor=C['border'])
    st.configure('TLabelframe.Label', background=C['bg'], foreground=C['dim'])
    st.configure('TCheckbutton', background=C['bg'], foreground=C['fg'], indicatorbackground=C['field'])
    st.map('TCheckbutton', background=[('active', C['bg'])],
           indicatorbackground=[('selected', C['accent'])])
    st.configure('TScrollbar', background=C['field'], troughcolor=C['bg'], bordercolor=C['bg'],
                 arrowcolor=C['dim'])
    st.map('TScrollbar', background=[('active', C['hover'])])
    st.configure('TSeparator', background=C['border'])
    root.configure(bg=C['bg'])
    for k, v in {'*Text.background': C['panel'], '*Text.foreground': C['fg'],
                 '*Text.insertBackground': C['fg'], '*Text.selectBackground': C['sel'],
                 '*Text.relief': 'flat', '*Text.highlightThickness': 0,
                 '*Listbox.background': C['panel'], '*Listbox.foreground': C['fg'],
                 '*Listbox.selectBackground': C['sel'], '*Listbox.selectForeground': C['fg'],
                 '*Listbox.relief': 'flat', '*Listbox.highlightThickness': 0,
                 '*TCombobox*Listbox.background': C['field'], '*TCombobox*Listbox.foreground': C['fg'],
                 '*TCombobox*Listbox.selectBackground': C['sel'],
                 '*Toplevel.background': C['bg']}.items():
        root.option_add(k, v)
    # every popup window gets the dark title bar too
    root.bind_class('Toplevel', '<Map>', lambda e: dark_titlebar(e.widget) if isinstance(e.widget, tk.Toplevel) else None, add='+')


def main():
    root = tk.Tk()
    apply_dark(root)
    App(root)
    root.after(50, lambda: dark_titlebar(root))
    root.mainloop()


if __name__ == '__main__':
    main()
