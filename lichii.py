"""Everything that touches Lichii, kept to stable, documented interfaces so a new build only has
to be dropped into ZELDAOOT:

  * builds:   any ZELDAOOT\\Lichii-build-<N>\\lichii.exe; the highest N wins
  * data dir: a portable 'user' folder beside the build, else %APPDATA%\\eden
  * debug server (EDEN_NX2_DEBUG_SERVER, JSON lines on 127.0.0.1): status, memory read/search,
    input injection. Read-only by design.
  * the boot log line "Module <name> loaded at <addr> (size <size>)" for module bases
  * host memory: with fastmem (default), guest address X lives at <arena> + X inside
    lichii.exe. We find <arena> by matching bytes the debug server returns, then write data
    there with WriteProcessMemory. Nothing depends on emulator offsets or symbols.
"""
import glob
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ZELDAOOT = os.path.normpath(os.path.join(HERE, '..', '..'))
TITLE_ID = '0400486024DEC000'
DEFAULT_PORT = 6545
IS_WIN = sys.platform == 'win32'
NOWIN = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW


# ================================================================ discovery
DESKTOP = os.path.dirname(ZELDAOOT)                  # ZELDAOOT sits on the Desktop
ARCHIVE = os.path.join(DESKTOP, 'ARCHIVE')
FRIZTI_DIR = os.path.join(ZELDAOOT, 'Lichii Frizti')  # the default launch build


def find_builds(root=ZELDAOOT):
    """[(number, folder)] for every Lichii build folder in root (any name), newest lichii.exe last."""
    out = []
    for d in os.listdir(root):
        p = os.path.join(root, d)
        exe = os.path.join(p, 'lichii.exe')
        if os.path.isfile(exe):
            m = re.search(r'(\d+)\s*$', d)
            out.append((os.path.getmtime(exe), int(m.group(1)) if m else -1, p))
    out.sort()
    return [(n, p) for _t, n, p in out]


def all_builds(roots=(ZELDAOOT,), depth=3):
    """(v0.5.9) Every folder under the roots (3 levels deep) that holds lichii.exe, newest exe
    first: [{'path', 'label', 'mtime', 'default', 'archived'}]. The Frizti fork is 'default'.
    Since Nov 7 ARCHIVE is not scanned: the dropdown shows only the installed builds
    (Frizti and the tray app's "Other builds\\Lichii Main"), not every archived one."""
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        root = os.path.normpath(root)
        for cur, dirs, files in os.walk(root):
            rel = os.path.relpath(cur, root)
            lvl = 0 if rel == '.' else rel.count(os.sep) + 1
            if lvl >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() not in ('user', 'shader', 'nand', 'sdmc', 'load')]
            if 'lichii.exe' in files:
                exe = os.path.join(cur, 'lichii.exe')
                try:
                    mt = os.path.getmtime(exe)
                except OSError:
                    continue
                found.append({'path': cur, 'label': os.path.relpath(cur, DESKTOP) if cur.startswith(DESKTOP) else cur,
                              'mtime': mt, 'default': os.path.normcase(cur) == os.path.normcase(FRIZTI_DIR),
                              'archived': os.path.normcase(cur).startswith(os.path.normcase(ARCHIVE))})
                dirs[:] = []      # a build folder has no builds inside it
    found.sort(key=lambda b: (not b['default'], -b['mtime']))
    return found


def default_build(builds=None):
    """The Frizti fork if installed, else the newest build anywhere."""
    builds = all_builds() if builds is None else builds
    for b in builds:
        if b['default']:
            return b['path']
    return builds[0]['path'] if builds else None


def newest_build(root=ZELDAOOT):
    try:
        b = find_builds(root)
    except OSError:
        return None
    return b[-1][1] if b else None


def resolve_lichii(path):
    """Folder holding lichii.exe, from that folder or the exe itself; None if not valid."""
    if not path:
        return None
    if os.path.isfile(path) and path.lower().endswith('.exe'):
        path = os.path.dirname(path)
    return path if os.path.isfile(os.path.join(path, 'lichii.exe')) else None


def resolve_dump(path):
    """Unpacked-game folder (the one with exefs\\main) from it, its parent or the OoT_dump folder."""
    if not path or not os.path.isdir(path):
        return None
    if os.path.isfile(os.path.join(path, 'exefs', 'main')):
        return path
    for pat in (('updates', '*'), ('*', 'updates', '*'), ('*',), ('*', '*')):
        cands = []
        for p in glob.glob(os.path.join(path, *pat)):
            if os.path.isfile(os.path.join(p, 'exefs', 'main')):
                try:
                    n = int(os.path.basename(p))
                except ValueError:
                    n = 0
                cands.append((n, p))
        if cands:
            return max(cands)[1]
    return None


def data_dir(build=None):
    if build and os.path.isdir(os.path.join(build, 'user')):
        return os.path.join(build, 'user')
    return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'eden')


def find_dump(root=ZELDAOOT):
    """Folder holding exefs/ and romfs/ of the newest loose dump (update folders preferred)."""
    cands = []
    for p in glob.glob(os.path.join(root, '*', 'updates', '*')):
        if os.path.isdir(os.path.join(p, 'exefs')):
            try:
                cands.append((1, int(os.path.basename(p)), p))
            except ValueError:
                cands.append((1, 0, p))
    for p in glob.glob(os.path.join(root, '*')):
        if os.path.isdir(os.path.join(p, 'exefs')) and os.path.isdir(os.path.join(p, 'romfs')):
            cands.append((0, 0, p))
    cands.sort()
    return cands[-1][2] if cands else None


def find_romfs(root=ZELDAOOT):
    d = find_dump(root)
    if d and os.path.isdir(os.path.join(d, 'romfs')):
        return os.path.join(d, 'romfs')
    return os.path.join(root, 'extracted', 'romfs')


def build_id(main_path):
    """Module (build) ID from an NSO header, as 40 hex chars."""
    with open(main_path, 'rb') as f:
        h = f.read(0x60)
    if h[:4] != b'NSO0':
        raise ValueError('not an NSO: ' + main_path)
    return h[0x40:0x54].hex().upper()


def title_id(npdm_path):
    try:
        b = open(npdm_path, 'rb').read()
        aci = struct.unpack_from('<I', b, 0x70)[0]
        if b[aci:aci + 4] == b'ACI0':
            return '%016X' % struct.unpack_from('<Q', b, aci + 0x10)[0]
    except Exception:
        pass
    return TITLE_ID


def save_dirs(data, tid=TITLE_ID):
    return sorted(glob.glob(os.path.join(data, 'nand', 'user', 'save', '0000000000000000', '*', tid)))


def log_path(data):
    return os.path.join(data, 'log', 'eden_log.txt')


MODULE_RX = re.compile(r'Module (\S+) loaded at (0x[0-9a-fA-F]+) \(size (0x[0-9a-fA-F]+)\)')


def read_modules(data):
    """{name: (base, size)} for the most recent boot, from eden_log.txt."""
    p = log_path(data)
    if not os.path.exists(p):
        return {}
    boots, cur = [], {}
    with open(p, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = MODULE_RX.search(line)
            if m:
                name = m.group(1)
                if name in cur:
                    boots.append(cur)
                    cur = {}
                cur[name] = (int(m.group(2), 16), int(m.group(3), 16))
    if cur:
        boots.append(cur)
    return boots[-1] if boots else {}


LOG_KEYS = ('layeredfs', 'romfs', 'debug server', 'loadmodule', 'patch_manager', 'devtool', 'cheat',
            'add-on', 'failed to load')


def log_excerpt(data, max_lines=60):
    """Lines of eden_log.txt (latest boot) about mods, patches and the debug server."""
    p = log_path(data)
    if not os.path.exists(p):
        return ['eden_log.txt not found at ' + p]
    with open(p, encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()[-20000:]
    start = 0
    for i, ln in enumerate(lines):
        if 'Module rtld loaded' in ln or 'Module main loaded' in ln:
            start = max(0, i - 400)  # LayeredFS lines come shortly before the modules load
            break
    for i in range(len(lines) - 1, -1, -1):
        if 'Module rtld loaded' in lines[i]:
            start = max(0, i - 400)
            break
    out = [ln.strip() for ln in lines[start:]
           if any(k in ln.lower() for k in LOG_KEYS) and 'Render.' not in ln and 'Querying NSO' not in ln]
    return out[:max_lines] or ['(no mod/patch lines in the latest boot)']


def lichii_pids():
    if not IS_WIN:
        return []
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq lichii.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, creationflags=NOWIN).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().strip('"') == 'lichii.exe':
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def launch(build, game_main, port=DEFAULT_PORT):
    """Start Lichii on the loose dump with the debug server on 127.0.0.1:port."""
    env = dict(os.environ)
    env['EDEN_NX2_DEBUG_SERVER'] = '127.0.0.1:%d' % port
    exe = os.path.join(build, 'lichii.exe')
    return subprocess.Popen([exe, '-g', game_main], cwd=build, env=env)


# ================================================================ debug server
class DebugError(Exception):
    pass


class DebugClient:
    """Client for Lichii's debug server (docs/nx2/debug-mcp.md). Thread-safe, auto-reconnects."""

    def __init__(self, port=DEFAULT_PORT, host='127.0.0.1', timeout=15):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock = None
        self.buf = b''
        self.n = 0
        self.lock = threading.Lock()

    def _connect(self):
        self.sock = socket.create_connection(self.addr, timeout=3)
        self.sock.settimeout(self.timeout)
        self.buf = b''

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def call(self, method, timeout=None, **params):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()
                    self.n += 1
                    rid = self.n
                    msg = json.dumps({'id': rid, 'method': method, 'params': params}) + '\n'
                    self.sock.settimeout(timeout or self.timeout)
                    self.sock.sendall(msg.encode())
                    while True:
                        while b'\n' not in self.buf:
                            chunk = self.sock.recv(1 << 20)
                            if not chunk:
                                raise ConnectionError('debug server closed the connection')
                            self.buf += chunk
                        line, self.buf = self.buf.split(b'\n', 1)
                        r = json.loads(line)
                        if r.get('id') != rid:
                            continue
                        if 'error' in r:
                            raise DebugError(r['error'])
                        return r.get('result')
                except (OSError, ConnectionError, ValueError) as e:
                    self.close()
                    if attempt:
                        raise DebugError('debug server not reachable on %s:%d (%s)'
                                         % (self.addr[0], self.addr[1], e))

    def status(self):
        return self.call('status', fps_window_ms=250)

    def read(self, addr, size):
        out = bytearray()
        while size > 0:
            n = min(size, 0x10000)
            r = self.call('read_memory', address='0x%X' % addr, size=n)
            out += bytes.fromhex(r['data'] if 'data' in r else r['hex'])
            addr += n
            size -= n
        return bytes(out)

    def search(self, pattern, regions='heap', start=None, end=None, max_hits=64, total_ms=6000):
        hits = []
        t0 = time.time()
        params = {'pattern': pattern.hex(), 'regions': regions, 'max_hits': min(max_hits, 256),
                  'budget_ms': 2000}
        if start is not None:
            params['start'] = '0x%X' % start
        if end is not None:
            params['end'] = '0x%X' % end
        complete = False
        while True:
            r = self.call('search_memory', timeout=30, **params)
            hits += [int(h, 16) if isinstance(h, str) else int(h.get('address'), 16)
                     for h in r.get('hits', [])]
            complete = r.get('complete', True)
            if complete or len(hits) >= max_hits or (time.time() - t0) * 1000 > total_ms:
                break
            params['start'] = r['next']
        return hits[:max_hits], complete

    def input(self, steps):
        return self.call('input_sequence', timeout=900, steps=steps)

    def screenshot(self, height=720, timeout_s=5):
        """PNG bytes of the next presented frame (fails while the game is paused or loading)."""
        import base64
        r = self.call('screenshot', timeout=timeout_s + 10, height=int(height), timeout_s=int(timeout_s),
                      inline=True)
        return base64.b64decode(r['png_base64']), r.get('width'), r.get('height')

    def log_tail(self, after=None, limit=200, contains=None):
        params = {'limit': limit}
        if after is not None:
            params['after'] = after
        if contains:
            params['contains'] = contains
        return self.call('log_tail', **params)


# ================================================================ host memory (Windows)
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class MBI(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_ulonglong), ('AllocationBase', ctypes.c_ulonglong),
                    ('AllocationProtect', wintypes.DWORD), ('RegionSize', ctypes.c_ulonglong),
                    ('State', wintypes.DWORD), ('Protect', wintypes.DWORD),
                    ('Type', wintypes.DWORD)]

    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.WriteProcessMemory.argtypes = k32.ReadProcessMemory.argtypes
    k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI),
                                   ctypes.c_size_t]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

MEM_COMMIT, MEM_MAPPED = 0x1000, 0x40000
PAGE_NOACCESS, PAGE_GUARD = 0x01, 0x100


class HostMemory:
    """Reads/writes lichii.exe's memory; maps guest addresses through the fastmem arena."""

    def __init__(self, pid):
        if not IS_WIN:
            raise OSError('host memory access needs Windows')
        self.pid = pid
        self.h = k32.OpenProcess(0x0438, False, pid)  # VM_OPERATION|VM_READ|VM_WRITE|QUERY_INFO
        if not self.h:
            raise OSError('cannot open lichii.exe (pid %d): error %d' % (pid, ctypes.get_last_error()))
        self.arena = None

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def regions(self):
        mbi = MBI()
        addr = 0
        while addr < 0x7FFFFFFF0000:
            if not k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            yield mbi.BaseAddress, mbi.RegionSize, mbi.State, mbi.Protect, mbi.Type
            addr = mbi.BaseAddress + mbi.RegionSize

    def hread(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)) or got.value != n:
            return None
        return buf.raw

    def hwrite(self, addr, data):
        got = ctypes.c_size_t()
        ok = k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), data, len(data), ctypes.byref(got))
        if not ok or got.value != len(data):
            raise OSError('write of %d bytes at host %#x failed (error %d)'
                          % (len(data), addr, ctypes.get_last_error()))

    def calibrate(self, dbg, anchors):
        """Find the fastmem arena. anchors: guest addresses (first = main base) to match.

        Every guest mapping is its own host view, so the view that holds main starts exactly at
        arena + main_base: compare the start of each mapped region with the guest bytes."""
        want = [(g, dbg.read(g, 256)) for g in anchors]
        g0, b0 = want[0]
        cands = []
        for base, size, state, prot, typ in self.regions():
            if state != MEM_COMMIT or typ != MEM_MAPPED or prot & (PAGE_NOACCESS | PAGE_GUARD):
                continue
            if size < 256 or base < g0:
                continue
            if self.hread(base, 64) == b0[:64]:
                cands.append(base - g0)
        for arena in cands:
            if all(self.hread(arena + g, len(b)) == b for g, b in want):
                self.arena = arena
                return arena
        raise OSError('fastmem arena not found (%d candidate views). Is fastmem on in Lichii?'
                      % len(cands))

    def read(self, gaddr, n):
        r = self.hread(self.arena + gaddr, n)
        if r is None:
            raise OSError('host read failed at guest %#x' % gaddr)
        return r

    def write(self, gaddr, data):
        self.hwrite(self.arena + gaddr, bytes(data))


# ================================================================ address expressions
def parse_addr(expr, modules):
    """'0x1234', 'main+0x40', 'main+40h', '[main+0x100]+0x8' is not supported (no deref)."""
    s = expr.strip().replace(' ', '')
    total = 0
    for term in re.split(r'(?=[+-])', s):
        if not term:
            continue
        sign = -1 if term[0] == '-' else 1
        t = term.lstrip('+-')
        if t in modules:
            v = modules[t][0]
        elif t.lower().endswith('h'):
            v = int(t[:-1], 16)
        else:
            v = int(t, 16)  # addresses and offsets are always hex
        total += sign * v
    return total


def nso_text_end(main_path):
    """Offset where main's code ends (.rodata memory offset), from the NSO header."""
    with open(main_path, 'rb') as f:
        h = f.read(0x40)
    return struct.unpack_from('<I', h, 0x24)[0]
