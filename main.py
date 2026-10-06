#!/usr/bin/env python3
"""
CK3 playset checksums

Hashes each mod in a Crusader Kings III playset separately, so reports from
different players can be compared to find the mod behind a "different
checksum" error in multiplayer.

Usage
-----
  python main.py                                # interactive menu
  python main.py --list                         # list playsets and exit
  python main.py --playset "AGOT MP" --out me.json
  python main.py me.json friend.json            # compare two reports
  python main.py me.json ana.json bob.json      # compare several with the first

These are SHA-256 hashes of the mod files, not the game's own checksum. The
launcher database is read from a temp copy, so the launcher can stay open.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1.3"
REPORT_FORMAT = 3
ISSUES_URL = "https://github.com/Haiostone/CK3-Checksum/issues"
STEAM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"

FROZEN = getattr(sys, "frozen", False)

SKIP_DIRS = {".git", ".svn", ".hg", ".vs", "__pycache__", ".idea"}
SKIP_FILES = {
    ".ds_store",
    "desktop.ini",
    "thumbs.db",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
}
# Extensions treated as text when --normalize-newlines is used.
TEXT_EXT = {
    ".txt", ".yml", ".yaml", ".gui", ".gfx", ".mod", ".asset", ".info",
    ".json", ".csv", ".lua", ".shader", ".fxh", ".md", ".settings",
}

CHUNK = 1024 * 1024
HASH_THREADS = 4
DISCORD_LIMIT = 10 * 1024 ** 2   # free-tier upload limit
MAX_MOVED_SHOWN = 30

EXAMPLES = """\
examples:
  %(prog)s                                   menu (same as double-clicking)
  %(prog)s --list                            list your playsets
  %(prog)s --playset "AGOT MP" --label Ana   scan without any questions
  %(prog)s mine.json ana.json bob.json       compare; the first is the reference
"""


# --------------------------------------------------------------------------
# console helpers
# --------------------------------------------------------------------------

def init_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


NO_PAUSE = False
INTERACTIVE = False   # started without arguments: menu and prompts
COLOR = False


def enable_color(disabled: bool) -> None:
    """Enable ANSI colours on a real console. Windows needs VT processing
    turned on first."""
    global COLOR
    if disabled or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.restype = wintypes.HANDLE
            kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = wintypes.DWORD()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return
        except Exception:
            return
    COLOR = True


def paint(text, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def red(text) -> str:
    return paint(text, "91")


def green(text) -> str:
    return paint(text, "92")


def yellow(text) -> str:
    return paint(text, "93")


def bold(text) -> str:
    return paint(text, "1")


def dim(text) -> str:
    return paint(text, "2")


def set_console_title(title: str) -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def double_clicked() -> bool:
    """Whether to hold the window open at the end.

    A --onefile exe always has two processes on its console, so
    GetConsoleProcessList can't tell Explorer from a shell. The exe always
    pauses unless --no-pause is given; a .py can still be detected."""
    if NO_PAUSE:
        return False
    if FROZEN:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        buf = (ctypes.c_uint * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 4)
        return count <= 1
    except Exception:
        return False


def pause_if_needed() -> None:
    if double_clicked() and sys.stdin and sys.stdin.isatty():
        try:
            print()
            input("Press Enter to close this window...")
        except (EOFError, OSError):
            pass


def clean_path(raw: str) -> str:
    """Strip the quotes a terminal adds to a dropped path."""
    return raw.strip().strip('"').strip("'")


def split_paths(raw: str) -> list:
    """Split a line of input into paths. Dropping several files puts them on
    one line, quoted if they contain spaces."""
    raw = raw.strip()
    if not raw:
        return []
    if Path(clean_path(raw)).is_file():
        return [clean_path(raw)]
    try:
        return [clean_path(p) for p in shlex.split(raw, posix=False)]
    except ValueError:
        return [clean_path(raw)]


def ask_report_paths(prompt: str, default: Path = None, optional: bool = False) -> list:
    """Ask for report files until all of them can be read."""
    while True:
        paths = split_paths(input(prompt))
        if not paths:
            if default is not None:
                return [str(default)]
            if optional:
                return []
            print("  drag a report file into this window, or type its path.")
            continue
        problems = []
        for p in paths:
            try:
                read_report(p)
            except ValueError as exc:
                problems.append(str(exc))
        if not problems:
            return paths
        for msg in problems:
            print(f"  {msg}")


def interactive_menu(ap) -> "argparse.Namespace":
    """Menu shown when started without arguments."""
    print("=" * 78)
    print(bold(f" CK3 playset checksums  v{SCRIPT_VERSION}"))
    print("=" * 78)
    print(" Finds which mod is behind a 'different checksum' error in multiplayer.")
    print(" Everyone in the lobby runs option 1 and shares the file it writes,")
    print(" then anyone runs option 2 to compare them.")
    print("\n  1. Scan my playset and write a report to share")
    print("  2. Compare reports")
    while True:
        choice = input("\nChoice [1]: ").strip() or "1"
        if choice in ("1", "2"):
            break
        print("  type 1 or 2.")

    args = ap.parse_args([])
    if choice == "1":
        return args

    mine = find_own_report(report_dirs())
    hint = f" [{mine.name}]" if mine else ""
    print()
    reports = ask_report_paths(f"Your report (drag the .json file here){hint}: ", default=mine)
    reports += ask_report_paths("Their report (drag one or more files here): ")
    while True:
        more = ask_report_paths("More reports? Drag them here, or press Enter to compare: ",
                                optional=True)
        if not more:
            break
        reports += more
    args.reports = reports
    return args


def human(nbytes: int) -> str:
    step = 1024.0
    val = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < step or unit == "GB":
            return f"{val:.1f}{unit}" if unit != "B" else f"{int(val)}B"
        val /= step
    return f"{val:.1f}GB"


def clip(text, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[:width - 3] + "..."


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def display_path(p) -> str:
    """Shorten the home folder to ~ so shared reports don't include the
    account name."""
    text = str(p)
    home = str(Path.home())
    if text.lower().startswith(home.lower()) and text[len(home):len(home) + 1] in ("", "\\", "/"):
        return "~" + text[len(home):]
    return text


def die(msg: str, code: int = 1) -> "None":
    text = f"\nERROR: {msg}"
    sys.stdout.flush()   # so the error prints after earlier output
    print(red(text) if sys.stderr.isatty() else text, file=sys.stderr)
    pause_if_needed()
    sys.exit(code)


# --------------------------------------------------------------------------
# locating and reading the launcher database
# --------------------------------------------------------------------------

def windows_documents_dir() -> Path:
    """Documents folder from the registry, so moved and OneDrive folders
    are found."""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, "Personal")
        return Path(os.path.expandvars(value))
    except (ImportError, OSError):
        return None


def candidate_ck3_dirs() -> list:
    home = Path.home()
    names = ["Crusader Kings III"]
    roots = []
    system = platform.system()

    if system == "Windows":
        docs = [windows_documents_dir(),
                home / "Documents", home / "OneDrive" / "Documents",
                home / "OneDrive" / "Dokumenter", home / "Dokumenter"]
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            docs.append(Path(userprofile) / "Documents")
        onedrive = os.environ.get("OneDrive")
        if onedrive:
            docs.append(Path(onedrive) / "Documents")
        for d in docs:
            if d is not None:
                roots.append(d / "Paradox Interactive")
    elif system == "Darwin":
        roots.append(home / "Documents" / "Paradox Interactive")
        roots.append(home / "Library" / "Application Support" / "Paradox Interactive")
    else:
        roots.append(home / ".local" / "share" / "Paradox Interactive")
        roots.append(home / "Documents" / "Paradox Interactive")
        # common Proton / Steam layout
        roots.append(home / ".steam" / "steam" / "steamapps" / "compatdata"
                     / "1158310" / "pfx" / "drive_c" / "users" / "steamuser"
                     / "Documents" / "Paradox Interactive")

    out = []
    for root in roots:
        for name in names:
            if root / name not in out:
                out.append(root / name)
    return out


def launcher_db_in(p: Path) -> Path:
    """p itself if it is a file, otherwise the launcher DB inside folder p."""
    try:
        if p.is_file():
            return p
        if p.is_dir():
            exact = p / "launcher-v2.sqlite"
            if exact.is_file():
                return exact
            hits = sorted(p.glob("launcher-v2*.sqlite"))
            return hits[0] if hits else None
    except OSError:
        pass
    return None


def ask_launcher_db() -> Path:
    print("\nCould not find the CK3 launcher database. It is in your Documents")
    print("folder, under Paradox Interactive\\Crusader Kings III.")
    while True:
        raw = clean_path(input("Drag that folder (or launcher-v2.sqlite) here: "))
        if not raw:
            continue
        db = launcher_db_in(Path(raw).expanduser())
        if db:
            return db
        print(f"  no launcher-v2.sqlite in {raw}")


def find_launcher_db(explicit: str = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        db = launcher_db_in(p)
        if db is None:
            die(f"no launcher database (launcher-v2.sqlite) at {p}")
        return db

    searched = candidate_ck3_dirs()
    for d in searched:
        db = launcher_db_in(d)
        if db:
            return db

    if INTERACTIVE:
        return ask_launcher_db()
    die("could not find the CK3 launcher database. Looked in:\n"
        + "\n".join(f"  {d}" for d in searched)
        + "\nPoint to it with --db, e.g.\n"
          '  --db "D:\\Documents\\Paradox Interactive\\Crusader Kings III"')


def open_db_readonly(db_path: Path):
    """Copy the DB and its WAL files to a temp dir, so an open launcher
    can't lock it, then connect."""
    tmpdir = Path(tempfile.mkdtemp(prefix="ck3sum_"))
    target = tmpdir / db_path.name
    try:
        shutil.copy2(db_path, target)
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        die(f"could not read {db_path}: {exc.strerror or exc}\n"
            "Close the Paradox launcher and try again.")
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            try:
                shutil.copy2(side, tmpdir / side.name)
            except OSError:
                pass
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    return conn, tmpdir


def table_columns(conn, table: str) -> set:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {r["name"] for r in rows}


def pick_columns(available: set, wanted: list) -> list:
    return [c for c in wanted if c in available]


def load_playsets(conn) -> list:
    cols = table_columns(conn, "playsets")
    if not cols:
        die("this database has no 'playsets' table - is it really the CK3 launcher DB?")
    sel = pick_columns(cols, ["id", "name", "isActive", "isRemoved", "updatedOn", "createdOn"])
    where = " WHERE isRemoved = 0" if "isRemoved" in cols else ""
    sql = f'SELECT {", ".join(sel)} FROM playsets{where}'
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    rows.sort(key=lambda r: (0 if r.get("isActive") else 1, (r.get("name") or "").lower()))

    # enabled/disabled mod counts per playset
    pcols = table_columns(conn, "playsets_mods")
    enabled = ("SUM(CASE WHEN pm.enabled THEN 1 ELSE 0 END)"
               if "enabled" in pcols else "COUNT(*)")
    try:
        counts = {r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT pm.playsetId, COUNT(*), {enabled} FROM playsets_mods pm "
            f"JOIN mods m ON m.id = pm.modId GROUP BY pm.playsetId")}
    except sqlite3.DatabaseError:
        counts = None
    if counts is not None:
        for r in rows:
            total, on = counts.get(r.get("id"), (0, 0))
            r["mods_enabled"] = on or 0
            r["mods_disabled"] = total - (on or 0)
    return rows


def load_playset_mods(conn, playset_id) -> list:
    mcols = table_columns(conn, "mods")
    pcols = table_columns(conn, "playsets_mods")
    if not mcols or not pcols:
        die("expected tables 'mods' and 'playsets_mods' in the launcher DB")

    mfields = pick_columns(mcols, [
        "id", "name", "displayName", "steamId", "pdxId", "gameRegistryId",
        "dirPath", "version", "requiredVersion", "source", "status", "timeUpdated",
    ])
    pfields = pick_columns(pcols, ["position", "enabled"])

    sel = ", ".join([f"m.{c} AS m_{c}" for c in mfields] +
                    [f"pm.{c} AS pm_{c}" for c in pfields])
    order = "ORDER BY pm.position" if "position" in pcols else ""
    sql = (f"SELECT {sel} FROM playsets_mods pm "
           f"JOIN mods m ON m.id = pm.modId "
           f"WHERE pm.playsetId = ? {order}")
    rows = conn.execute(sql, (playset_id,)).fetchall()

    mods = []
    for i, r in enumerate(rows):
        d = dict(r)
        mod = {k[2:]: v for k, v in d.items() if k.startswith("m_")}
        for k, v in d.items():
            if k.startswith("pm_"):
                mod[k[3:]] = v
        mod.setdefault("position", i)
        mod.setdefault("enabled", 1)
        mods.append(mod)

    # 'position' is text in some launcher versions; sort numbers numerically
    def sort_key(m):
        p = m.get("position")
        try:
            return (0, int(p), "")
        except (TypeError, ValueError):
            return (1, 0, str(p))
    mods.sort(key=sort_key)
    return mods


# --------------------------------------------------------------------------
# resolving mod paths
# --------------------------------------------------------------------------

def parse_descriptor_path(mod_file: Path) -> Path:
    """A .mod descriptor may contain path="mod/foo" or an absolute path."""
    try:
        text = mod_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("path"):
            _, _, value = line.partition("=")
            value = value.strip().strip('"')
            if value:
                return Path(value)
    return None


def resolve_mod_dir(mod: dict, ck3_dir: Path) -> Path:
    candidates = []
    dir_path = mod.get("dirPath")
    if dir_path:
        p = Path(str(dir_path).replace("\\", "/"))
        candidates.append(p)
        if not p.is_absolute():
            candidates.append(ck3_dir / p)

    reg = mod.get("gameRegistryId")
    if reg:
        rp = Path(str(reg).replace("\\", "/"))
        if not rp.is_absolute():
            rp = ck3_dir / rp
        if rp.suffix.lower() == ".mod" and rp.is_file():
            inner = parse_descriptor_path(rp)
            if inner is not None:
                if inner.is_absolute():
                    candidates.append(inner)
                else:
                    candidates.append(ck3_dir / inner)
        else:
            candidates.append(rp)

    for c in candidates:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    # a zipped/binary mod is still worth hashing as a single file
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def hash_file(path: Path, normalize_newlines: bool) -> tuple:
    h = hashlib.sha256()
    size = 0
    if normalize_newlines and path.suffix.lower() in TEXT_EXT:
        data = path.read_bytes()
        size = len(data)
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        # strip a UTF-8 BOM
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        h.update(data)
    else:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                h.update(chunk)
    return h.hexdigest(), size


def iter_mod_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.lower() in SKIP_FILES:
                continue
            yield Path(dirpath) / name


def hash_one(path: Path, normalize_newlines: bool) -> tuple:
    """(digest, size, error) for one file. Errors are returned, not raised,
    so one unreadable file doesn't stop the pool."""
    try:
        digest, size = hash_file(path, normalize_newlines)
        return digest, size, None
    except OSError as exc:
        return None, 0, exc


def hash_mod_dir(root: Path, normalize_newlines: bool, want_files: bool,
                 progress=None) -> dict:
    """Hash of a mod folder: SHA-256 over 'relpath\\0filehash\\n' lines,
    sorted by lowercased relative path so it is machine independent.

    Done in two phases so a stall is visible: enumerating the tree, then
    hashing the files on HASH_THREADS threads."""
    if root.is_file():
        digest, size = hash_file(root, normalize_newlines)
        return {"hash": digest, "file_count": 1, "total_bytes": size,
                "files": {root.name.lower(): digest} if want_files else None,
                "errors": []}

    # phase 1 - walk the tree
    paths = []
    for path in iter_mod_files(root):
        paths.append(path)
        if progress and len(paths) % 500 == 0:
            progress("scanning", len(paths), 0, 0)

    entries = []
    files = {} if want_files else None
    total = 0
    errors = []

    # phase 2 - hash them; map() yields in input order, so the output is
    # the same as hashing one file at a time
    pool = ThreadPoolExecutor(HASH_THREADS)
    try:
        results = pool.map(lambda p: hash_one(p, normalize_newlines), paths)
        for i, (full, (digest, size, error)) in enumerate(zip(paths, results), 1):
            rel = full.relative_to(root).as_posix().lower()
            if error:
                errors.append(f"{rel}: {error}")
                continue
            total += size
            entries.append((rel, digest))
            if want_files:
                files[rel] = digest
            if progress and (i % 200 == 0 or i == len(paths)):
                progress("hashing", i, len(paths), total)
    finally:
        # on Ctrl+C, drop the queued files instead of hashing them all first
        pool.shutdown(wait=False, cancel_futures=True)

    entries.sort(key=lambda t: t[0])
    outer = hashlib.sha256()
    for rel, digest in entries:
        outer.update(rel.encode("utf-8"))
        outer.update(b"\0")
        outer.update(digest.encode("ascii"))
        outer.update(b"\n")

    return {"hash": outer.hexdigest(), "file_count": len(entries),
            "total_bytes": total, "files": files, "errors": errors}


# --------------------------------------------------------------------------
# report building
# --------------------------------------------------------------------------

def mod_key(mod: dict) -> str:
    steam = mod.get("steamId")
    if steam:
        return f"steam:{steam}"
    reg = mod.get("gameRegistryId") or mod.get("pdxId")
    if reg:
        return f"reg:{str(reg).replace(chr(92), '/').lower()}"
    return f"name:{(mod.get('displayName') or mod.get('name') or '?').lower()}"


def mod_label(mod: dict) -> str:
    return mod.get("displayName") or mod.get("name") or "<unnamed>"


def build_report(playset: dict, mods: list, ck3_dir: Path, db_path: Path,
                 normalize_newlines: bool, want_files: bool,
                 include_disabled: bool, label: str,
                 verbose: bool = False) -> dict:
    results = []
    active = [m for m in mods if include_disabled or m.get("enabled")]
    width = len(str(len(active)))
    tty = bool(sys.stdout.isatty())

    def progress(phase, done, total, nbytes):
        if not tty:
            return
        if phase == "scanning":
            line = f"          scanning folder... {done:,} files"
        else:
            pct = f"{100 * done // total:>3}%" if total else "  -"
            line = (f"          hashing {pct}  {done:,}/{total:,} files  "
                    f"{human(nbytes)}")
        sys.stdout.write("\r" + line.ljust(72))
        sys.stdout.flush()

    def clear():
        if tty:
            sys.stdout.write("\r" + " " * 72 + "\r")

    print(f"\nScanning {len(active)} mods from playset '{playset.get('name')}'")
    print("Big mods can take a minute.\n")
    scan_started = datetime.now()

    for idx, mod in enumerate(active, 1):
        name = mod_label(mod)
        print(f"  [{idx:>{width}}/{len(active)}] {clip(name, 60)}")

        path = resolve_mod_dir(mod, ck3_dir)
        entry = {
            "order": idx,
            "position": str(mod.get("position")),
            "key": mod_key(mod),
            "name": name,
            "steam_id": str(mod.get("steamId")) if mod.get("steamId") else None,
            "version": mod.get("version"),
            "required_version": mod.get("requiredVersion"),
            "source": mod.get("source"),
            "enabled": bool(mod.get("enabled")),
        }

        if path is None:
            where = mod.get("dirPath")
            entry.update({"status": "missing",
                          "path": display_path(where) if where else None,
                          "hash": None, "file_count": 0, "total_bytes": 0})
            print(red("          NOT ON DISK") + "  "
                  + (str(where) if where else "(the launcher has no folder for it)"))
            results.append(entry)
            continue

        if verbose:
            print(f"          path: {path}")

        started = datetime.now()
        res = hash_mod_dir(path, normalize_newlines, want_files, progress)
        elapsed = (datetime.now() - started).total_seconds()
        clear()

        entry.update({
            "status": "ok" if not res["errors"] else "partial",
            "path": display_path(path),
            "hash": res["hash"],
            "file_count": res["file_count"],
            "total_bytes": res["total_bytes"],
            "seconds": round(elapsed, 1),
        })
        if want_files:
            entry["files"] = res["files"]
        if res["errors"]:
            entry["errors"] = res["errors"][:20]

        print(dim(f"          {res['hash'][:12]}  {res['file_count']:,} files  "
                  f"{human(res['total_bytes'])}  {elapsed:.1f}s"))
        if res["errors"]:
            print(yellow(f"          {len(res['errors'])} file(s) could not be read, "
                         f"first: {res['errors'][0][:60]}"))
        if res["file_count"] > 100000 or res["total_bytes"] > 40 * 1024 ** 3:
            print(yellow("          that is a lot for one mod - is this the right folder?"))
            print(yellow(f"          {path}"))
        results.append(entry)

    total_s = (datetime.now() - scan_started).total_seconds()
    print(f"\nDone in {total_s:.0f}s.")

    # order-independent, so a load-order difference doesn't show up as a
    # file difference
    content = hashlib.sha256()
    for e in sorted(results, key=lambda r: r["key"]):
        content.update((e["key"] + "|" + (e["hash"] or "MISSING") + "\n").encode("utf-8"))

    order = hashlib.sha256()
    for e in results:
        order.update((e["key"] + "\n").encode("utf-8"))

    return {
        "report_format": REPORT_FORMAT,
        "script_version": SCRIPT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "machine": socket.gethostname(),
        "os": platform.platform(),
        "ck3_dir": display_path(ck3_dir),
        "db": display_path(db_path),
        "playset": playset.get("name"),
        "playset_id": str(playset.get("id")),
        "options": {
            "normalize_newlines": normalize_newlines,
            "include_disabled": include_disabled,
            "file_hashes": want_files,
        },
        "combined_hash": content.hexdigest(),
        "order_hash": order.hexdigest(),
        "mod_count": len(results),
        "mods": results,
    }


def print_summary(report: dict) -> None:
    print("\n" + "=" * 78)
    print(f" Playset     {report['playset']}  ({plural(report['mod_count'], 'mod')})")
    print(f" Name        {report['label']}  ({report['machine']})")
    print(f" Files hash  {bold(report['combined_hash'][:12])}   "
          + dim("same for everyone whose mod files match"))
    print(f" Order hash  {bold(report['order_hash'][:12])}   "
          + dim("same for everyone whose load order matches"))
    print("=" * 78)
    print(" Post both hashes in your group chat. If everyone's match, your mods")
    print(" are in sync and there is no need to compare reports.")

    missing = [m for m in report["mods"] if m["status"] == "missing"]
    if missing:
        print(red(f"\n{plural(len(missing), 'mod')} in this playset "
                  f"{'is' if len(missing) == 1 else 'are'} not on disk:"))
        for m in missing:
            print(f"  - {m['name']}")
        print("These WILL break the game checksum. Subscribe again, or let Steam")
        print("finish downloading, then scan again.")


def write_report(report: dict, out: Path, fallback: bool) -> Path:
    """Write the report JSON, falling back to the home folder if the current
    folder isn't writable."""
    targets = [out]
    if fallback:
        targets.append(Path.home() / out.name)
    error = None
    for target in targets:
        try:
            with target.open("w", encoding="utf-8") as fh:
                json.dump(report, fh, separators=(",", ":"))
            return target
        except OSError as exc:
            error = exc
    die(f"could not write the report to {targets[-1]}: {error}")


def reveal_in_explorer(path: Path) -> bool:
    """Open Explorer with the file selected."""
    if os.name != "nt":
        return False
    try:
        subprocess.Popen(f'explorer /select,"{path.resolve()}"')
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def read_report(path: str) -> dict:
    """Load a report, raising ValueError with a readable message."""
    name = Path(path).name
    if name.lower().endswith(".zip"):
        raise ValueError(f"{name} is a zip file - extract the .json report from it first")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot open {path}: {exc.strerror or exc}")
    except ValueError:
        raise ValueError(f"{name} is not a report from this program")
    if not isinstance(data, dict) or "mods" not in data:
        raise ValueError(f"{name} is not a report from this program")
    return data


def load_report(path: str) -> dict:
    try:
        return read_report(path)
    except ValueError as exc:
        die(str(exc))


def host_slug() -> str:
    return "".join(c if c.isalnum() else "-" for c in socket.gethostname())[:20]


def report_dirs() -> list:
    """Folders where this PC's own reports may be."""
    dirs = [Path.cwd()]
    if FROZEN:
        dirs.append(Path(sys.executable).resolve().parent)
    return dirs


def find_own_report(dirs: list) -> Path:
    """Newest report in dirs with this PC's default file name."""
    suffix = f"-{host_slug()}.json".lower()
    seen, hits = set(), []
    for d in dirs:
        try:
            for p in d.glob("ck3-checksums-*.json"):
                if p.name.lower().endswith(suffix) and p.resolve() not in seen:
                    seen.add(p.resolve())
                    hits.append(p)
        except OSError:
            continue
    return max(hits, key=lambda p: p.stat().st_mtime, default=None)


def same_file(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def complete_reports(paths: list) -> list:
    """Pair a single report with this PC's own report, or ask for another."""
    if len(paths) >= 2:
        return paths
    given = Path(paths[0])
    mine = find_own_report(report_dirs() + [given.parent])
    if mine and not same_file(mine, given):
        print(f"Comparing with your latest report: {mine.name}")
        return [str(mine), paths[0]]
    if not (sys.stdin and sys.stdin.isatty()):
        die("give at least two reports to compare")
    return paths + ask_report_paths("Drag the report to compare it with here: ")


def report_name(r: dict, fallback: str) -> str:
    return str(r.get("label") or r.get("machine") or fallback)


def scan_notes(r: dict) -> str:
    return "  newlines normalized" if r.get("options", {}).get("normalize_newlines") else ""


def steam_link(m: dict) -> str:
    return STEAM_URL.format(m["steam_id"]) if m.get("steam_id") else ""


def version_text(m: dict) -> str:
    return f"v{m['version']}" if m.get("version") else "v?"


def incompatibilities(a: dict, b: dict, name_a: str, name_b: str) -> list:
    """Reasons why the per-mod hashes of two reports cannot be compared."""
    out = []
    if a.get("report_format") != b.get("report_format"):
        out.append(f"the reports come from incompatible versions of this program "
                   f"(format {a.get('report_format')} vs {b.get('report_format')}).")
    opt_a, opt_b = a.get("options", {}), b.get("options", {})
    # v1.2 had a quick scan that hashed file sizes only
    if bool(opt_a.get("quick")) != bool(opt_b.get("quick")):
        quick = name_a if opt_a.get("quick") else name_b
        out.append(f"{quick}'s report is a quick scan from an older version.")
    if bool(opt_a.get("normalize_newlines")) != bool(opt_b.get("normalize_newlines")):
        out.append("only one of the reports used --normalize-newlines.")
    return out


def compare_order(a: dict, b: dict, ma: dict, mb: dict,
                  name_a: str, name_b: str) -> bool:
    """Compare load order using only the mods both sides have, so a mod that
    is missing on one side does not shift everything below it."""
    seq_a = [m["key"] for m in a["mods"] if m["key"] in mb]
    seq_b = [m["key"] for m in b["mods"] if m["key"] in ma]

    if seq_a == seq_b:
        print(green("\nLoad order matches") + " for every mod both playsets have.")
        return True

    # mods outside the longest common subsequence are the ones that moved
    matcher = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False)
    moved = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            moved.update(seq_a[i1:i2])
            moved.update(seq_b[j1:j2])
    pos_a = {k: i for i, k in enumerate(seq_a)}
    pos_b = {k: i for i, k in enumerate(seq_b)}

    def place(seq, pos, mods):
        m = mods[seq[pos]]
        num = f"#{m.get('order') or pos + 1}"
        where = f"after {clip(mods[seq[pos - 1]]['name'], 44)}" if pos else "first"
        return f"{num:<5}{where}"

    w = min(max(len(name_a), len(name_b)), 20)
    print(red("\nLoad order differs") + f" - {plural(len(moved), 'mod')} "
          f"{'is' if len(moved) == 1 else 'are'} in a different place:")
    ordered = sorted(moved, key=lambda k: pos_a[k])
    for k in ordered[:MAX_MOVED_SHOWN]:
        print(f"\n  {bold(ma[k]['name'])}")
        print(f"    {clip(name_a, w):<{w}}  {place(seq_a, pos_a[k], ma)}")
        print(f"    {clip(name_b, w):<{w}}  {place(seq_b, pos_b[k], mb)}")
    if len(ordered) > MAX_MOVED_SHOWN:
        print(f"\n  ... and {len(ordered) - MAX_MOVED_SHOWN} more.")

    print("\n  Fix this in the launcher: the mod that loads later wins any file")
    print("  both mods touch, so two people with the same files but a different")
    print("  order can still end up with different game state.")
    return False


def print_file_diff(x: dict, y: dict, name_a: str, name_b: str, max_files: int) -> bool:
    """List the files that differ inside one mod. Returns False if a report
    has no per-file hashes (--slim)."""
    fa, fb = x.get("files"), y.get("files")
    if fa is None or fb is None:
        return False
    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    changed = sorted(k for k in fa if k in fb and fa[k] != fb[k])
    print(f"    files: {len(changed)} changed, {len(only_a)} only in {name_a}, "
          f"{len(only_b)} only in {name_b}")
    for label, items in ((f"only in {name_a}", only_a),
                         (f"only in {name_b}", only_b),
                         ("changed", changed)):
        for f in items[:max_files]:
            print(dim(f"      [{label}] {f}"))
        if len(items) > max_files:
            print(dim(f"      ... and {len(items) - max_files} more {label}"))
    return True


def compare_pair(a: dict, b: dict, name_a: str, name_b: str,
                 show_files: bool, max_files: int) -> tuple:
    """Print how report b differs from report a.
    Returns (exit code, one-line verdict)."""
    ma = {m["key"]: m for m in a["mods"]}
    mb = {m["key"]: m for m in b["mods"]}

    w = min(max(len(name_a), len(name_b)), 16)
    print("\n" + "=" * 78)
    for name, r in ((name_a, a), (name_b, b)):
        print(" " + bold(f"{clip(name, w):<{w}}") + f"  '{clip(r.get('playset'), 26)}'  "
              f"{r.get('mod_count')} mods  "
              + dim(str(r.get("generated", ""))[:10]) + scan_notes(r))
    print("=" * 78)

    problems = incompatibilities(a, b, name_a, name_b)
    if problems:
        print(yellow("\nWARNING: the mod files cannot be compared, because"))
        for p in problems:
            print(yellow(f"  - {p}"))
        print(yellow("  Everyone should scan again with the same version and settings."))
        print(yellow("  Which mods are in each playset, and their order, are still checked."))

    only_a = [k for k in ma if k not in mb]
    only_b = [k for k in mb if k not in ma]
    both = [k for k in ma if k in mb]
    not_on_disk = [k for k in both
                   if (ma[k].get("status") == "missing") != (mb[k].get("status") == "missing")]
    differing = [] if problems else [
        k for k in both if k not in not_on_disk and ma[k].get("hash") != mb[k].get("hash")]

    for who, other, keys, mods in ((name_a, name_b, only_a, ma), (name_b, name_a, only_b, mb)):
        if keys:
            print(red(f"\nIn {who}'s playset but not {other}'s ({len(keys)}):"))
            for k in keys:
                link = steam_link(mods[k])
                print(f"  - {mods[k]['name']}" + (dim(f"   {link}") if link else ""))

    if not_on_disk:
        print(red(f"\nIn both playsets, but not downloaded on one PC ({len(not_on_disk)}):"))
        for k in not_on_disk:
            who = name_a if ma[k].get("status") == "missing" else name_b
            print(f"  - {ma[k]['name']}  (missing on {who}'s PC)")
        print("  Subscribe again, or let Steam finish downloading, then scan again.")

    if differing:
        print(red(f"\nSame mod, different files ({len(differing)}):"))
        missing_details = False
        for k in differing:
            x, y = ma[k], mb[k]
            print(f"\n  {bold(x['name'])}")
            if steam_link(x):
                print(dim(f"  {steam_link(x)}"))
            vw = min(max(len(version_text(x)), len(version_text(y))), 16)
            for name, m in ((name_a, x), (name_b, y)):
                print(f"    {clip(name, w):<{w}}  {clip(version_text(m), vw):<{vw}} "
                      f"{m.get('file_count') or 0:>7,} files {human(m.get('total_bytes') or 0):>9}  "
                      + dim(str(m.get("hash"))[:12]))
            if x.get("version") and y.get("version") and x["version"] != y["version"]:
                print("    -> different versions installed")
            else:
                print("    -> same version number, different files")
            if show_files and not print_file_diff(x, y, name_a, name_b, max_files):
                missing_details = True
        if missing_details:
            print(dim("\n  (No file-level details: a report was made with --slim.)"))
        print("\n  Usually one person has an outdated or half-downloaded copy. They")
        print("  should unsubscribe from the mod, wait until the launcher drops it,")
        print("  then subscribe again. Restarting Steam also triggers pending updates.")

    order_ok = compare_order(a, b, ma, mb, name_a, name_b)

    issues = []
    if differing:
        issues.append(f"{plural(len(differing), 'mod')} differ" + ("s" if len(differing) == 1 else ""))
    if not_on_disk:
        issues.append(f"{len(not_on_disk)} not downloaded")
    if only_a or only_b:
        issues.append(f"{len(only_a) + len(only_b)} not in both playsets")
    if not order_ok:
        issues.append("load order differs")
    if problems:
        issues.append("files not compared - scan again the same way")

    print()
    if not issues:
        print(green(bold(f"RESULT: {name_a} and {name_b} are in sync.")))
        print("  If CK3 still reports different checksums, the cause is outside the")
        print("  mods: game version, beta branch, or stray .mod files in")
        print("  Documents/Paradox Interactive/Crusader Kings III/mod/.")
        return 0, "in sync"

    verdict = ", ".join(issues)
    print(red(bold(f"RESULT: {verdict}.")))
    return 2, verdict


def compare_reports(paths: list, show_files: bool, max_files: int) -> int:
    reports = [load_report(p) for p in paths]

    names = []
    for path, r in zip(paths, reports):
        base = report_name(r, Path(path).stem)
        name, n = base, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        names.append(name)

    for i, p in enumerate(paths[1:], 1):
        if any(same_file(p, q) for q in paths[:i]):
            print(yellow(f"Note: {Path(p).name} was given more than once."))

    outcomes = []
    for r, name in zip(reports[1:], names[1:]):
        code, verdict = compare_pair(reports[0], r, names[0], name, show_files, max_files)
        outcomes.append((name, code, verdict))

    if len(outcomes) > 1:
        w = min(max(len(n) for n in names[1:]), 20)
        print("\n" + "=" * 78)
        print(bold(f" SUMMARY - everyone compared with {names[0]}"))
        for name, code, verdict in outcomes:
            line = f"   {clip(name, w):<{w}}  {verdict}"
            print(green(line) if code == 0 else red(line))
        print("=" * 78)

    return max(code for _, code, _ in outcomes)


# --------------------------------------------------------------------------
# playset selection
# --------------------------------------------------------------------------

def playset_info(p: dict) -> str:
    parts = []
    if "mods_enabled" in p:
        parts.append(plural(p["mods_enabled"], "mod"))
        if p["mods_disabled"]:
            parts.append(f"{p['mods_disabled']} disabled")
    if p.get("isActive"):
        parts.append("active")
    return f"({', '.join(parts)})" if parts else ""


def choose_playset(playsets: list, wanted: str, include_disabled: bool) -> dict:
    if wanted:
        low = wanted.lower()
        exact = [p for p in playsets if str(p.get("name", "")).lower() == low
                 or str(p.get("id")) == wanted]
        if exact:
            return exact[0]
        partial = [p for p in playsets if low in str(p.get("name", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            die("'{}' matches several playsets: {}".format(
                wanted, ", ".join(str(p.get("name")) for p in partial)))
        die(f"no playset matching '{wanted}' (see --list)")

    print("\nYour playsets:\n")
    for i, p in enumerate(playsets, 1):
        print(f"  {i:>2}. {p.get('name')}  " + dim(playset_info(p)))

    if not sys.stdin.isatty():
        die("no --playset given and stdin is not interactive")

    default = next((i for i, p in enumerate(playsets, 1) if p.get("isActive")), None)
    prompt = f"\nWhich playset? [{default}]: " if default else "\nWhich playset? (number): "
    while True:
        raw = input(prompt).strip() or str(default or "")
        if raw.isdigit() and 1 <= int(raw) <= len(playsets):
            p = playsets[int(raw) - 1]
            usable = p.get("mods_enabled", 1) + (p.get("mods_disabled", 0) if include_disabled else 0)
            if usable:
                return p
            print("  that playset has no enabled mods - pick another.")
            continue
        print(f"  type a number from 1 to {len(playsets)}.")


def default_out_name(playset_name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(playset_name))
    safe = "-".join(filter(None, safe.split("-")))[:40] or "playset"
    return f"ck3-checksums-{safe}-{host_slug()}.json"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Per-mod checksums for a CK3 playset, to find which mod breaks a\n"
                    "multiplayer checksum. Run without arguments for a menu.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reports", nargs="*", metavar="REPORT.json",
                    help="reports to compare instead of scanning; the first is the "
                         "reference the others are compared with")
    ap.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    scan = ap.add_argument_group("scanning")
    scan.add_argument("--db", help="path to launcher-v2.sqlite (or the CK3 user folder)")
    scan.add_argument("--playset", help="playset name or id (skips the prompt)")
    scan.add_argument("--list", action="store_true", help="list playsets and exit")
    scan.add_argument("--out", help="write the JSON report here")
    scan.add_argument("--label", help="your name/nickname, shown in comparisons")
    scan.add_argument("--slim", action="store_true",
                      help="omit per-file hashes (smaller report, no file-level diff)")
    scan.add_argument("--include-disabled", action="store_true",
                      help="also hash mods that are toggled off in the playset")
    scan.add_argument("--normalize-newlines", action="store_true",
                      help="ignore CRLF/LF and BOM differences in text files "
                           "(everyone must use the same setting)")
    scan.add_argument("--verbose", action="store_true",
                      help="print the folder each mod resolves to before hashing")
    comp = ap.add_argument_group("comparing")
    comp.add_argument("--compare", nargs="+", metavar="REPORT.json",
                      help="same as listing the reports without --compare")
    comp.add_argument("--no-files", action="store_true",
                      help="list which mods differ, but not the files inside them")
    comp.add_argument("--files", action="store_true", help=argparse.SUPPRESS)  # old flag, now the default
    comp.add_argument("--max-files", type=int, default=15,
                      help="max files listed per category and mod (default 15)")
    out = ap.add_argument_group("output")
    out.add_argument("--no-pause", action="store_true",
                     help="do not wait for Enter before exiting (for scripting)")
    out.add_argument("--no-color", action="store_true",
                     help="plain output without colours (also: NO_COLOR=1)")
    return ap


def main(argv=None) -> int:
    init_console()
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        pause_if_needed()
        raise

    global NO_PAUSE, INTERACTIVE
    NO_PAUSE = args.no_pause
    enable_color(args.no_color)

    if argv is None and len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty():
        INTERACTIVE = True
        set_console_title(f"CK3 playset checksums v{SCRIPT_VERSION}")
        args = interactive_menu(ap)

    reports = list(args.compare or []) + list(args.reports)
    if reports:
        return compare_reports(complete_reports(reports), not args.no_files, args.max_files)

    db_path = find_launcher_db(args.db)
    ck3_dir = db_path.parent
    print(dim(f"Launcher database: {db_path}"))

    conn, tmpdir = open_db_readonly(db_path)
    try:
        playsets = load_playsets(conn)
        if not playsets:
            die("no playsets in the database - create one in the CK3 launcher first")
        if args.list:
            for i, p in enumerate(playsets, 1):
                print(f"  {i:>2}. {p.get('name')}  {playset_info(p)}   id={p.get('id')}")
            return 0

        playset = choose_playset(playsets, args.playset, args.include_disabled)
        mods = load_playset_mods(conn, playset["id"])
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not any(args.include_disabled or m.get("enabled") for m in mods):
        hint = " (add --include-disabled to scan its disabled mods)" if mods else ""
        die(f"playset '{playset.get('name')}' has no enabled mods{hint}")

    if INTERACTIVE:
        default = socket.gethostname()
        args.label = input(f"\nYour name, as the others will see it [{default}]: ").strip() or default

    label = args.label or socket.gethostname()
    report = build_report(playset, mods, ck3_dir, db_path,
                          args.normalize_newlines, not args.slim,
                          args.include_disabled, label,
                          verbose=args.verbose)
    print_summary(report)

    out = Path(args.out) if args.out else Path(default_out_name(playset.get("name")))
    out = write_report(report, out, fallback=not args.out)
    size = out.stat().st_size
    print(f"\nReport written to: {bold(out.resolve())}  ({human(size)})")
    if size > DISCORD_LIMIT:
        print(yellow("That is over Discord's 10 MB upload limit. Zip it before sending "
                     "(right-click > Compress to ZIP file)."))
    if INTERACTIVE and reveal_in_explorer(out):
        print("The folder is open in Explorer, so you can drag the file into your chat.")

    exe = Path(sys.argv[0]).name
    shown = out.name if same_file(out.parent, Path.cwd()) else str(out)
    print("\nNext:")
    print(f"  1. Send {out.name} to the others in your lobby.")
    if FROZEN:
        print("  2. When you have their reports, start this program again and pick 2,")
        print(f"     or drag all the .json files onto {exe}.")
    else:
        print("  2. When you have their reports, compare them:")
        print(f'       python {exe} "{shown}" "their-report.json"')
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(130)
    except Exception as exc:  # keep the window open on a crash
        import traceback
        traceback.print_exc()
        print(f"\nUnexpected error: {exc}")
        print(f"Please report it at {ISSUES_URL} and include the text above.")
        pause_if_needed()
        sys.exit(1)
    pause_if_needed()
    sys.exit(code)
