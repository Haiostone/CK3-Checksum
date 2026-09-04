#!/usr/bin/env python3
"""
ck3_playset_checksums.py

Diagnose "different checksum" problems in Crusader Kings III multiplayer.

CK3 shows one combined checksum, which is useless for finding *which* mod is
out of sync. This script reads the launcher database, lets you pick a playset,
and hashes every mod folder in it separately. Everyone in the lobby runs it and
exchanges the JSON report; the compare mode then shows exactly which mods
differ, and (optionally) which files inside them.

Usage
-----
  python ck3_playset_checksums.py                    # interactive
  python ck3_playset_checksums.py --list             # list playsets and exit
  python ck3_playset_checksums.py --playset "AGOT MP" --out me.json
  python ck3_playset_checksums.py --compare me.json friend.json
  python ck3_playset_checksums.py --compare me.json friend.json --files

Notes
-----
* The hashes here are NOT the game's own checksum. They are stable SHA-256
  digests of the mod folder contents, which is what you need to compare
  installs between two machines.
* Close the launcher first if you get a locked-database error (the script
  copies the DB to a temp file, so this should normally not happen).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1.2"
REPORT_FORMAT = 3

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


def double_clicked() -> bool:
    """Whether to hold the window open at the end.

    A --onefile PyInstaller exe runs a child process, so the console always has
    at least two processes attached and the usual GetConsoleProcessList trick
    cannot tell Explorer from a shell. For the exe we therefore always pause
    unless --no-pause was given; when run as a .py we can still detect it."""
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


def ask_path(prompt: str) -> str:
    """Read a path, tolerating drag-and-dropped quoting."""
    while True:
        raw = input(prompt).strip().strip('"').strip("'")
        if raw and Path(raw).is_file():
            return raw
        if raw:
            print(f"  not a file: {raw}")
        else:
            print("  type or drag a file here.")


def interactive_menu(ap) -> "argparse.Namespace":
    """Shown when the exe is launched with no arguments."""
    print("=" * 78)
    print(f" CK3 playset checksums  v{SCRIPT_VERSION}")
    print("=" * 78)
    print("\n  1. Scan my setup and write a report to send to the others")
    print("  2. Compare two reports")
    while True:
        choice = input("\nChoice [1]: ").strip() or "1"
        if choice in ("1", "2"):
            break
        print("  type 1 or 2.")

    if choice == "1":
        args = ap.parse_args([])
        label = input("Your name (shown in the comparison) [hostname]: ").strip()
        if label:
            args.label = label
        fast = input("Quick scan? (names and sizes only, much faster) [y/N]: ").strip().lower()
        args.quick = fast == "y"
        return args

    a = ask_path("\nYour report (drag the .json file here): ")
    b = ask_path("Their report (drag the .json file here): ")
    args = ap.parse_args(["--compare", a, b])
    detail = input("List the individual files that differ? [Y/n]: ").strip().lower()
    args.files = detail != "n"
    return args


def human(nbytes: int) -> str:
    step = 1024.0
    val = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < step or unit == "GB":
            return f"{val:.1f}{unit}" if unit != "B" else f"{int(val)}B"
        val /= step
    return f"{val:.1f}GB"


def die(msg: str, code: int = 1) -> "None":
    print(f"\nERROR: {msg}", file=sys.stderr)
    pause_if_needed()
    sys.exit(code)


# --------------------------------------------------------------------------
# locating and reading the launcher database
# --------------------------------------------------------------------------

def candidate_ck3_dirs() -> list:
    home = Path.home()
    names = ["Crusader Kings III"]
    roots = []
    system = platform.system()

    if system == "Windows":
        docs = [home / "Documents", home / "OneDrive" / "Documents",
                home / "OneDrive" / "Dokumenter", home / "Dokumenter"]
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            docs.append(Path(userprofile) / "Documents")
        onedrive = os.environ.get("OneDrive")
        if onedrive:
            docs.append(Path(onedrive) / "Documents")
        for d in docs:
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
            out.append(root / name)
    return out


def find_launcher_db(explicit: str = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_dir():
            hits = sorted(p.glob("launcher-v2*.sqlite"))
            if not hits:
                die(f"no launcher-v2*.sqlite found in {p}")
            return hits[0]
        if not p.exists():
            die(f"database not found: {p}")
        return p

    for d in candidate_ck3_dirs():
        if not d.is_dir():
            continue
        hits = sorted(d.glob("launcher-v2*.sqlite"))
        if hits:
            return hits[0]

    die("could not locate the CK3 launcher database.\n"
        "Pass it explicitly, e.g.:\n"
        '  --db "%USERPROFILE%\\Documents\\Paradox Interactive\\Crusader Kings III\\launcher-v2.sqlite"')


def open_db_readonly(db_path: Path):
    """Copy the DB (plus WAL sidecars) to a temp dir so an open launcher
    cannot lock us out, then connect."""
    tmpdir = Path(tempfile.mkdtemp(prefix="ck3sum_"))
    target = tmpdir / db_path.name
    shutil.copy2(db_path, target)
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

    # 'position' is a text field in some launcher versions; sort stably.
    def sort_key(m):
        p = m.get("position")
        if isinstance(p, str):
            return (0, p)
        return (0, f"{p:012d}" if isinstance(p, int) else "")
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
        # strip a UTF-8 BOM, which some editors add and others do not
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


def hash_mod_dir(root: Path, normalize_newlines: bool, want_files: bool,
                 quick: bool = False, progress=None) -> dict:
    """Hash of a mod folder: SHA-256 over 'relpath\\0filehash\\n' lines,
    sorted by lowercased relative path so it is machine independent.

    Done in two phases so a stall is visible: enumerating the tree, then
    reading the files."""
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

    # phase 2 - read them
    for i, full in enumerate(paths, 1):
        rel = full.relative_to(root).as_posix().lower()
        try:
            if quick:
                size = full.stat().st_size
                digest = f"size:{size}"
            else:
                digest, size = hash_file(full, normalize_newlines)
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        total += size
        entries.append((rel, digest))
        if want_files:
            files[rel] = digest
        if progress and (i % 200 == 0 or i == len(paths)):
            progress("hashing", i, len(paths), total)

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
                 quick: bool = False, verbose: bool = False) -> dict:
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

    mode = "size only (--quick)" if quick else "SHA-256"
    print(f"\nHashing {len(active)} mods from playset '{playset.get('name')}' [{mode}]")
    print("Large mods can take a while. If nothing moves at all, see --verbose.\n")

    for idx, mod in enumerate(active, 1):
        name = mod_label(mod)
        print(f"  [{idx:>{width}}/{len(active)}] {name[:56]}")

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
            entry.update({"status": "missing", "path": mod.get("dirPath"),
                          "hash": None, "file_count": 0, "total_bytes": 0})
            print(f"          MISSING ON DISK: {mod.get('dirPath')}")
            results.append(entry)
            continue

        if verbose:
            print(f"          path: {path}")

        started = datetime.now()
        res = hash_mod_dir(path, normalize_newlines, want_files, quick, progress)
        elapsed = (datetime.now() - started).total_seconds()
        clear()

        entry.update({
            "status": "ok" if not res["errors"] else "partial",
            "path": str(path),
            "hash": res["hash"],
            "file_count": res["file_count"],
            "total_bytes": res["total_bytes"],
            "seconds": round(elapsed, 1),
        })
        if want_files:
            entry["files"] = res["files"]
        if res["errors"]:
            entry["errors"] = res["errors"][:20]

        print(f"          {res['hash'][:12]}  {res['file_count']:,} files  "
              f"{human(res['total_bytes'])}  {elapsed:.1f}s")
        if res["errors"]:
            print(f"          {len(res['errors'])} file(s) could not be read, "
                  f"first: {res['errors'][0][:60]}")
        if res["file_count"] > 100000 or res["total_bytes"] > 8 * 1024 ** 3:
            print("          ** that folder looks far too big for one mod - "
                  "check the path above **")
        results.append(entry)

    # content hash ignores order on purpose, so a pure load-order difference
    # does not masquerade as a file difference
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
        "ck3_dir": str(ck3_dir),
        "db": str(db_path),
        "playset": playset.get("name"),
        "playset_id": str(playset.get("id")),
        "options": {
            "normalize_newlines": normalize_newlines,
            "include_disabled": include_disabled,
            "file_hashes": want_files,
            "quick": quick,
        },
        "combined_hash": content.hexdigest(),
        "order_hash": order.hexdigest(),
        "mod_count": len(results),
        "mods": results,
    }


def print_summary(report: dict) -> None:
    print("\n" + "=" * 78)
    print(f"Playset : {report['playset']}")
    print(f"Machine : {report['machine']}  ({report['label']})")
    print(f"Mods    : {report['mod_count']}")
    print(f"Content hash    : {report['combined_hash'][:24]}  (files, order-independent)")
    print(f"Load order hash : {report['order_hash'][:24]}  (sequence of mods)")
    print("=" * 78)
    missing = [m for m in report["mods"] if m["status"] == "missing"]
    if missing:
        print("\nMods with no folder on disk (these WILL break the game checksum):")
        for m in missing:
            print(f"  - {m['name']}  ({m.get('path')})")


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def load_report(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read report {path}: {exc}")
    if "mods" not in data:
        die(f"{path} does not look like a report from this script")
    return data


def compare_order(a: dict, b: dict, ma: dict, mb: dict,
                  name_a: str, name_b: str) -> bool:
    """Compare load order using only the mods both sides have, so a mod that
    is missing on one side does not shift everything below it."""
    seq_a = [m["key"] for m in a["mods"] if m["key"] in mb]
    seq_b = [m["key"] for m in b["mods"] if m["key"] in ma]

    if seq_a == seq_b:
        print("\nLoad order MATCHES for every mod both sides have.")
        return True

    print("\n-- Load order DIFFERS --")
    first = next((i for i, (x, y) in enumerate(zip(seq_a, seq_b)) if x != y), 0)
    print(f"   First divergence at position {first + 1}:")
    print(f"     {name_a}: {ma[seq_a[first]]['name']}")
    print(f"     {name_b}: {mb[seq_b[first]]['name']}")

    print(f"\n   {'#':>3}  {name_a[:32]:<34}{name_b[:32]}")
    for i in range(max(len(seq_a), len(seq_b))):
        ka = seq_a[i] if i < len(seq_a) else None
        kb = seq_b[i] if i < len(seq_b) else None
        left = ma[ka]["name"][:32] if ka else "-"
        right = mb[kb]["name"][:32] if kb else "-"
        mark = " " if ka == kb else "!"
        print(f" {mark} {i + 1:>3}  {left:<34}{right}")

    print("\n   Fix this in the launcher: the mod that loads later wins any file")
    print("   both mods touch, so two people with the same files but a different")
    print("   order can still end up with different game state.")
    return False


def compare_reports(path_a: str, path_b: str, show_files: bool, max_files: int) -> int:
    a = load_report(path_a)
    b = load_report(path_b)

    name_a = a.get("label") or a.get("machine") or "A"
    name_b = b.get("label") or b.get("machine") or "B"

    ma = {m["key"]: m for m in a["mods"]}
    mb = {m["key"]: m for m in b["mods"]}

    print("=" * 78)
    print(f"A: {name_a:<30} playset '{a.get('playset')}'  {a.get('mod_count')} mods")
    print(f"B: {name_b:<30} playset '{b.get('playset')}'  {b.get('mod_count')} mods")
    print("=" * 78)

    same_content = a.get("combined_hash") == b.get("combined_hash")
    same_order = a.get("order_hash") == b.get("order_hash")

    if same_content and same_order:
        print("\nMod files AND load order are identical on both machines.")
        print("If CK3 still reports different checksums, the cause is outside the")
        print("mods: game version, beta branch, or stray .mod files in")
        print("Documents/Paradox Interactive/Crusader Kings III/mod/.")
    elif same_content:
        print("\nMod files are identical, but the LOAD ORDER differs - see below.")
    else:
        print("\nMod files differ - details below.")

    only_a = [k for k in ma if k not in mb]
    only_b = [k for k in mb if k not in ma]
    both = [k for k in ma if k in mb]

    if only_a:
        print(f"\n-- Only in {name_a} ({len(only_a)}) --")
        for k in only_a:
            print(f"  + {ma[k]['name']}")
    if only_b:
        print(f"\n-- Only in {name_b} ({len(only_b)}) --")
        for k in only_b:
            print(f"  + {mb[k]['name']}")

    differing = [k for k in both if ma[k].get("hash") != mb[k].get("hash")]

    if differing:
        print(f"\n-- Same mod, different content ({len(differing)}) --")
        for k in differing:
            x, y = ma[k], mb[k]
            print(f"\n  {x['name']}")
            if x.get("steam_id"):
                print(f"    steam id : {x['steam_id']}")
            print(f"    {name_a:<14} v{x.get('version')}  {str(x.get('hash'))[:12]}  "
                  f"{x.get('file_count')} files  {human(x.get('total_bytes') or 0)}")
            print(f"    {name_b:<14} v{y.get('version')}  {str(y.get('hash'))[:12]}  "
                  f"{y.get('file_count')} files  {human(y.get('total_bytes') or 0)}")
            if x.get("status") == "missing" or y.get("status") == "missing":
                print("    ** one side does not have this mod installed **")
            if show_files:
                print_file_diff(x, y, name_a, name_b, max_files)
    else:
        print("\nNo per-mod content differences among the mods both sides have.")

    order_ok = compare_order(a, b, ma, mb, name_a, name_b)

    opt_a, opt_b = a.get("options", {}), b.get("options", {})
    for flag in ("normalize_newlines", "quick"):
        if opt_a.get(flag) != opt_b.get(flag):
            print(f"\nWARNING: the reports used different --{flag.replace('_', '-')} "
                  "settings,\nso the hashes are not comparable. Re-run both the same way.")
    if opt_a.get("quick") and opt_b.get("quick"):
        print("\nBoth reports are --quick (name+size only). Mods shown as matching "
              "could\nstill differ in content; re-run without --quick to be sure.")

    return 0 if not (differing or only_a or only_b or not order_ok) else 2


def print_file_diff(x: dict, y: dict, name_a: str, name_b: str, max_files: int) -> None:
    if x.get("status") == "missing" or y.get("status") == "missing":
        return
    fa, fb = x.get("files"), y.get("files")
    if not fa or not fb:
        print("    (no per-file hashes in one of the reports - re-run without --slim)")
        return
    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    changed = sorted(k for k in fa if k in fb and fa[k] != fb[k])
    print(f"    files: {len(changed)} changed, {len(only_a)} only in {name_a}, "
          f"{len(only_b)} only in {name_b}")
    for label, items in ((f"only in {name_a}", only_a),
                         (f"only in {name_b}", only_b),
                         ("changed", changed)):
        for f in items[:max_files]:
            print(f"      [{label}] {f}")
        if len(items) > max_files:
            print(f"      ... and {len(items) - max_files} more {label}")


# --------------------------------------------------------------------------
# playset selection
# --------------------------------------------------------------------------

def choose_playset(playsets: list, wanted: str) -> dict:
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
        die(f"no playset matching '{wanted}'")

    print("\nPlaysets found:\n")
    for i, p in enumerate(playsets, 1):
        flag = "  (active)" if p.get("isActive") else ""
        print(f"  {i:>2}. {p.get('name')}{flag}")

    if not sys.stdin.isatty():
        die("no --playset given and stdin is not interactive")

    while True:
        raw = input("\nWhich playset? (number, blank = active) ").strip()
        if not raw:
            active = [p for p in playsets if p.get("isActive")]
            if active:
                return active[0]
            print("  no active playset; type a number.")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(playsets):
            return playsets[int(raw) - 1]
        print("  invalid choice.")


def default_out_name(playset_name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(playset_name))
    safe = "-".join(filter(None, safe.split("-")))[:40] or "playset"
    host = "".join(c if c.isalnum() else "-" for c in socket.gethostname())[:20]
    return f"ck3-checksums-{safe}-{host}.json"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="Per-mod checksums for a CK3 playset, for comparing multiplayer setups.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="path to launcher-v2.sqlite (or the CK3 user folder)")
    ap.add_argument("--playset", help="playset name or id (skips the prompt)")
    ap.add_argument("--list", action="store_true", help="list playsets and exit")
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--label", help="your name/nickname, shown in comparisons")
    ap.add_argument("--slim", action="store_true",
                    help="omit per-file hashes (smaller report, no file-level diff)")
    ap.add_argument("--include-disabled", action="store_true",
                    help="also hash mods that are toggled off in the playset")
    ap.add_argument("--normalize-newlines", action="store_true",
                    help="ignore CRLF/LF and BOM differences in text files "
                         "(everyone must use the same setting)")
    ap.add_argument("--quick", action="store_true",
                    help="compare file names and sizes only - much faster, "
                         "catches most mismatches, everyone must use it")
    ap.add_argument("--no-pause", action="store_true",
                    help="do not wait for Enter before exiting (for scripting)")
    ap.add_argument("--verbose", action="store_true",
                    help="print the folder each mod resolves to before hashing")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"),
                    help="compare two reports instead of scanning")
    ap.add_argument("--files", action="store_true",
                    help="with --compare: list differing files inside each mod")
    ap.add_argument("--max-files", type=int, default=15,
                    help="max files listed per category with --files (default 15)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        pause_if_needed()
        raise

    global NO_PAUSE
    NO_PAUSE = args.no_pause

    if argv is None and len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty():
        args = interactive_menu(ap)

    if args.compare:
        return compare_reports(args.compare[0], args.compare[1], args.files, args.max_files)

    db_path = find_launcher_db(args.db)
    ck3_dir = db_path.parent
    print(f"Launcher DB: {db_path}")

    conn, tmpdir = open_db_readonly(db_path)
    try:
        playsets = load_playsets(conn)
        if not playsets:
            die("no playsets in the database")
        if args.list:
            for i, p in enumerate(playsets, 1):
                flag = "  (active)" if p.get("isActive") else ""
                print(f"  {i:>2}. {p.get('name')}{flag}   id={p.get('id')}")
            return 0

        playset = choose_playset(playsets, args.playset)
        mods = load_playset_mods(conn, playset["id"])
        if not mods:
            die(f"playset '{playset.get('name')}' contains no mods")
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    label = args.label or socket.gethostname()
    report = build_report(playset, mods, ck3_dir, db_path,
                          args.normalize_newlines, not args.slim,
                          args.include_disabled, label,
                          quick=args.quick, verbose=args.verbose)
    print_summary(report)

    out = Path(args.out) if args.out else Path(default_out_name(playset.get("name")))
    with out.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, separators=(",", ":"))
    print(f"\nReport written to: {out.resolve()}")
    print("Send this file to the others. To compare afterwards:")
    exe = Path(sys.argv[0]).name
    prefix = exe if FROZEN else f"python {exe}"
    print(f"  {prefix} --compare \"{out.name}\" \"their-report.json\" --files")
    if FROZEN:
        print("  (or just start the program again and choose option 2)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
    except Exception as exc:  # keep the window open on a crash
        import traceback
        traceback.print_exc()
        print(f"\nUnexpected error: {exc}")
        pause_if_needed()
        sys.exit(1)
    pause_if_needed()
    sys.exit(code)
