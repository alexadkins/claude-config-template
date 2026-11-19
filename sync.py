# /// script
# requires-python = ">=3.11"
# ///
"""Two-way sync between ~/.claude and this repo (macOS + Windows).

    uv run --script sync.py               sync now
    uv run --script sync.py --background  return at once, sync in a detached process
    uv run --script sync.py --stage-only  export + stage only (for reviewing)
    uv run --script sync.py --first-of-day  skip if a sync already completed today (SessionStart)
    uv run --script sync.py --nightly     sync + transcript backup (9pm job)
    uv run --script sync.py --enable-dotfiles  turn on dotfile sync here, then sync

Runs on the first Claude Code session of the day, nightly, and on /handoff.
With start_onedrive = true in config.toml, each run also launches the
OneDrive client if it isn't running (so the transcript backup uploads).

Each run:
  1. discover projects (Claude Code hook input + ~/.claude/projects transcripts)
  2. pull --rebase other machines' commits (skipped when offline)
  3. reconcile each file three ways: local, repo, last-synced manifest
     (adds, edits, and deletes flow both ways; both-changed = conflict copy)
  4. commit (privacy pre-commit hook runs: ferpa_scan.py) and push

Projects are stored under short names (claude/projects/<Name>/memory/). Each
machine maps names to local folders in ~/.config/claude-config/config.toml.
A per-machine manifest (state.json) records each file's hash at the last
sync, so a change can be attributed to this machine or another one.

settings.json is synced without its "hooks" key; each machine keeps its own
hooks, since their commands contain machine-specific paths.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
CLAUDE = Path.home() / ".claude"
CONF_DIR = Path.home() / ".config" / "claude-config"
CONFIG = CONF_DIR / "config.toml"
STATE = CONF_DIR / "state.json"
LOG = CONF_DIR / "sync.log"
LOCK = CONF_DIR / "sync.lock"
LAST_SYNC = CONF_DIR / "last_sync"  # date of the last complete sync (--first-of-day)
GLOBAL = "claude/global"
PROJECTS = "claude/projects"
GLOBAL_NAMES = ["CLAUDE.md", "keybindings.json", "agents", "commands"]
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
HOST = platform.node().split(".")[0] or "unknown"
WINDOWS = os.name == "nt"

# Shell/terminal dotfiles, per platform: repo name (under dotfiles/<platform>/)
# -> local path. Opt-in per machine (dotfiles = true in config.toml, or
# --enable-dotfiles). Not synced: ~/.ssh/config (machine-specific hosts/keys).
DOTFILES = "dotfiles"
PLATFORM = "windows" if WINDOWS else "mac" if sys.platform == "darwin" else "linux"
_HOME = Path.home()
DOTFILE_PATHS = {
    "windows": {
        ".bashrc": _HOME / ".bashrc",
        ".bash_profile": _HOME / ".bash_profile",
        ".inputrc": _HOME / ".inputrc",
        ".minttyrc": _HOME / ".minttyrc",
        ".config/starship.toml": _HOME / ".config" / "starship.toml",
        ".config/powershell/profile.ps1": _HOME / ".config" / "powershell" / "profile.ps1",
        "windows-terminal/settings.json": Path(os.environ.get("LOCALAPPDATA", _HOME / "AppData" / "Local"))
            / "Packages" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState" / "settings.json",
    },
    "mac": {
        ".zshrc": _HOME / ".zshrc",
        ".vimrc": _HOME / ".vimrc",
        ".config/kitty/kitty.conf": _HOME / ".config" / "kitty" / "kitty.conf",
        ".config/starship.toml": _HOME / ".config" / "starship.toml",
    },
    "linux": {
        ".bashrc": _HOME / ".bashrc",
        ".zshrc": _HOME / ".zshrc",
        ".vimrc": _HOME / ".vimrc",
        ".config/starship.toml": _HOME / ".config" / "starship.toml",
    },
}.get(PLATFORM, {})
DOTFILE_BACKUPS = CONF_DIR / "dotfile-backups"


# ---------- logging, notifications, git ----------

def log(msg):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 1_000_000:
        LOG.replace(LOG.with_suffix(".log.old"))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def notify(msg):
    msg = f"{msg} See {LOG}"
    log(f"NOTIFY {msg}")
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e",
                              f'display notification {json.dumps(msg)} with title "Claude backup"'])
        elif WINDOWS:
            # message box, not a balloon: balloons vanish when Windows notifications are off
            # 0x40030 = warning icon + always on top
            text = msg.replace("'", "''")
            ps = f"(New-Object -ComObject WScript.Shell).Popup('{text}',0,'Claude backup',0x40030)"
            cmd = ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps]
            try:  # outlive a parent job that kills its children on exit
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW
                                 | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            except OSError:
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        pass


def git(*args, check=True):
    env = dict(os.environ)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=5 -o BatchMode=yes")
    # launchd / Task Scheduler PATHs lack uv's default install dir (pre-commit needs uv)
    env["PATH"] = os.pathsep.join([str(Path.home() / ".local" / "bin"), env.get("PATH", "")])
    cmd = ["git", "-C", str(REPO), *args]
    # stdin=DEVNULL: on Windows, a child that inherits a stdin pipe which the
    # hook_cwd_from_stdin thread is still blocked reading hangs at spawn
    env_kw = {"env": env, "stdin": subprocess.DEVNULL}
    if args[0] in ("pull", "push", "fetch", "ls-remote", "commit", "rebase"):
        # network or slow/stateful (commit runs the FERPA hook): no retry, a killed
        # run can leave a rebase or index.lock behind
        r = subprocess.run(cmd, capture_output=True, text=True, **env_kw, timeout=60)
    else:
        # local commands take ms, but the first git call after ~20 min idle
        # sometimes stalls on Windows (cold start); retry once before failing
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, **env_kw, timeout=20)
        except subprocess.TimeoutExpired:
            log(f"git {' '.join(args)}: timed out after 20s, retrying")
            r = subprocess.run(cmd, capture_output=True, text=True, **env_kw, timeout=60)
    if r.stdout.strip() or r.stderr.strip():
        log(f"git {' '.join(args)}: {(r.stdout + r.stderr).strip()}")
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return r


# ---------- config + project discovery ----------

def slug(path):
    """Claude Code's project folder name for a path."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def load_config():
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    cfg.setdefault("projects", {})
    return cfg


def save_config(cfg):
    """Rewrite config.toml. Top-level keys must be strings, bools, numbers, or
    lists of those (json.dumps output is valid TOML for these)."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Per-machine settings for claude-config (not committed). See README."]
    for key, val in cfg.items():
        if key != "projects" and val not in (None, "", []):
            lines.append(f"{key} = {json.dumps(val)}")
    lines += ["", "# repo project name = local project folder", "[projects]"]
    lines += [f"{json.dumps(k)} = {json.dumps(v)}" for k, v in sorted(cfg["projects"].items())]
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_project(cfg, path):
    """Map a local project folder to a repo name (its folder name). Returns True if added."""
    path = str(Path(path))
    if path in cfg["projects"].values():
        return False
    p = Path(path)
    name = p.name or slug(path).strip("-")
    if name in cfg["projects"]:  # same name, different folder on this machine
        name = f"{p.parent.name}-{name}"
    cfg["projects"][name] = path
    log(f"mapped project {name} -> {path}")
    return True


def cwd_from_transcripts(project_dir):
    for t in project_dir.glob("*.jsonl"):
        try:
            with open(t, encoding="utf-8") as f:
                for _, line in zip(range(50), f):
                    cwd = json.loads(line).get("cwd")
                    if cwd and slug(cwd) == project_dir.name:
                        return cwd
        except (OSError, ValueError):
            continue
    return None


def cwd_from_filesystem(name):
    """Rebuild a path from its slug by walking the filesystem (no transcripts)."""
    roots = [f"{d}:\\" for d in string.ascii_uppercase] if WINDOWS else ["/"]
    for root in roots:
        if not name.startswith(slug(root)) or not os.path.isdir(root):
            continue
        stack = [(Path(root), name[len(slug(root)):])]
        while stack:
            d, rest = stack.pop()
            if not rest:
                return str(d)
            try:
                for e in os.scandir(d):
                    s = slug(e.name)
                    if e.is_dir() and (rest == s or rest.startswith(s + "-")):
                        stack.append((Path(e.path), rest[len(s) + 1:]))
            except OSError:
                continue
    return None


def discover(cfg, hook_cwd=None):
    """Map every local Claude project that has memories, plus the hook's cwd."""
    changed = False
    if hook_cwd:
        changed |= add_project(cfg, hook_cwd)
    mapped = {slug(v) for v in cfg["projects"].values()}
    for d in sorted((CLAUDE / "projects").glob("*/")):
        mem = d / "memory"
        if d.name in mapped or not mem.is_dir() or not any(mem.iterdir()):
            continue
        cwd = cwd_from_transcripts(d) or cwd_from_filesystem(d.name)
        if cwd:
            changed |= add_project(cfg, cwd)
        else:
            log(f"WARNING: can't find the folder for project {d.name}; not synced")
    return changed


def repo_projects():
    d = REPO / PROJECTS
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


# ---------- items: one synced file each ----------

def fhash(data):
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def files_under(root, top_names=None):
    """Relative posix paths of files under root (optionally only these top-level names)."""
    out = set()
    if not root.is_dir():
        return out
    tops = [root / n for n in top_names] if top_names else [root]
    for t in tops:
        if t.is_file():
            out.add(t.relative_to(root).as_posix())
        elif t.is_dir():
            for f in t.rglob("*"):
                if f.is_file() and f.name not in SKIP_NAMES and ".git" not in f.parts:
                    out.add(f.relative_to(root).as_posix())
    return out


class Item:
    def __init__(self, rel, local):
        self.rel, self.local = rel, local

    def read_local(self):
        return self.local.read_bytes() if self.local.is_file() else None

    def write_local(self, data):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_bytes(data)

    def delete_local(self):
        self.local.unlink(missing_ok=True)


class SettingsItem(Item):
    """settings.json minus "hooks"; writing keeps this machine's hooks."""

    def _load(self):
        try:
            return json.loads(self.local.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def read_local(self):
        s = self._load()
        if s is None:
            return None
        s.pop("hooks", None)
        return (json.dumps(s, indent=2, sort_keys=True) + "\n").encode()

    def write_local(self, data):
        merged = json.loads(data)
        hooks = (self._load() or {}).get("hooks")
        if hooks:
            merged["hooks"] = hooks
        self.local.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    def delete_local(self):
        pass  # never delete local settings


class DotfileItem(Item):
    """A home-folder dotfile. Backs up the local copy before overwriting it."""

    def write_local(self, data):
        if self.local.is_file():
            dest = DOTFILE_BACKUPS / f"{datetime.now():%Y-%m-%d_%H%M%S}" / Path(self.rel).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.local, dest)
        super().write_local(data)
        if self.local.suffix == ".sh" and not WINDOWS:
            self.local.chmod(self.local.stat().st_mode | 0o111)

    def delete_local(self):
        pass  # never delete a dotfile because another machine dropped it


def build_items(cfg, manifest):
    items = {}
    rel = f"{GLOBAL}/settings.json"
    items[rel] = SettingsItem(rel, CLAUDE / "settings.json")
    if cfg.get("dotfiles"):
        for name, local in DOTFILE_PATHS.items():
            r = f"{DOTFILES}/{PLATFORM}/{name}"
            items[r] = DotfileItem(r, local)
    areas = [(GLOBAL, CLAUDE, GLOBAL_NAMES)]
    areas += [(f"{PROJECTS}/{name}/memory", CLAUDE / "projects" / slug(path) / "memory", None)
              for name, path in cfg["projects"].items()]
    for prefix, local_root, tops in areas:
        names = files_under(local_root, tops) | files_under(REPO / prefix, tops)
        names |= {m[len(prefix) + 1:] for m in manifest if m.startswith(prefix + "/")}
        for n in names:
            r = f"{prefix}/{n}"
            if r not in items:
                items[r] = Item(r, local_root / n)
    return items


def read_repo(rel):
    p = REPO / rel
    return p.read_bytes() if p.is_file() else None


def write_repo(rel, data):
    p = REPO / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def delete_repo(rel):
    p = REPO / rel
    p.unlink(missing_ok=True)
    for d in p.parents:
        if d == REPO / "claude" or not d.is_dir() or any(d.iterdir()):
            break
        d.rmdir()


def merge_lines(base, extra):
    """MEMORY.md first-contact merge: base lines, then extra's new lines."""
    lines = base.decode("utf-8").replace("\r\n", "\n").rstrip("\n").split("\n")
    seen = set(lines)
    for line in extra.decode("utf-8").replace("\r\n", "\n").split("\n"):
        if line.strip() and line not in seen:
            lines.append(line)
            seen.add(line)
    return ("\n".join(lines) + "\n").encode()


def conflict_name(rel):
    p = Path(rel)
    return p.with_name(f"{p.stem}.conflict-{HOST}{p.suffix}").as_posix()


# ---------- phases ----------

def reconcile(items, manifest):
    """Three-way sync of each file: local copy (L), repo copy (R, already
    pulled), and its hash at the last sync on this machine (m). Returns the
    new manifest and the list of conflicts."""
    new, conflicts = {}, []
    for rel, it in sorted(items.items()):
        L, R, m = it.read_local(), read_repo(rel), manifest.get(rel)
        hL = fhash(L) if L is not None else None
        hR = fhash(R) if R is not None else None
        if hL == hR:
            if hR:
                new[rel] = hR
            continue
        if L is None:
            if m is not None and hR == m:
                delete_repo(rel)  # deleted here
                log(f"deleted here: {rel}")
            else:
                it.write_local(R)  # new elsewhere, or restore
                new[rel] = hR
                log(f"pulled: {rel}")
        elif R is None:
            if m is not None and hL == m:
                it.delete_local()  # deleted elsewhere
                log(f"deleted elsewhere: {rel}")
            else:
                write_repo(rel, L)  # new here
                new[rel] = hL
                log(f"pushed: {rel}")
        elif hL == m:
            it.write_local(R)  # changed elsewhere
            new[rel] = hR
            log(f"pulled: {rel}")
        elif hR == m:
            write_repo(rel, L)  # changed here
            new[rel] = hL
            log(f"pushed: {rel}")
        elif rel.endswith("/MEMORY.md"):
            merged = merge_lines(R, L)  # both changed: union of index lines
            write_repo(rel, merged)
            it.write_local(merged)
            new[rel] = fhash(merged)
            log(f"merged: {rel}")
        elif rel.startswith(DOTFILES + "/"):
            # both changed: repo wins; DotfileItem backs up the local copy first
            it.write_local(R)
            new[rel] = hR
            conflicts.append(rel)
            log(f"CONFLICT {rel}: repo version applied, local copy in {DOTFILE_BACKUPS}")
        else:
            # both changed: repo version wins locally, this machine's copy is
            # kept next to it (and reaches ~/.claude on the next sync)
            write_repo(conflict_name(rel), L)
            it.write_local(R)
            new[rel] = hR
            conflicts.append(rel)
            log(f"CONFLICT {rel}: this machine's copy saved as {conflict_name(rel)}")
    return new, conflicts


def hook_cwd_from_stdin():
    """Claude Code hooks pass JSON (incl. cwd) on stdin. Times out so a manual
    run with an open, silent stdin doesn't hang."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    data = []
    t = threading.Thread(target=lambda: data.append(sys.stdin.read()), daemon=True)
    t.start()
    t.join(2)
    try:
        return json.loads(data[0] or "{}").get("cwd") if data else None
    except (ValueError, AttributeError):
        return None


def acquire_lock():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            LOCK.mkdir()
            return True
        except FileExistsError:
            if time.time() - LOCK.stat().st_mtime > 600:  # stale (crashed run)
                LOCK.rmdir()
            else:
                return False
    return False


def sync(hook_cwd=None, stage_only=False):
    cfg = load_config()
    if discover(cfg, hook_cwd):
        save_config(cfg)
    manifest = json.loads(STATE.read_text()) if STATE.exists() else {}

    online = False
    if not stage_only and git("remote", "get-url", "origin", check=False).returncode == 0:
        heads = git("ls-remote", "--heads", "origin", "main", check=False)
        online = heads.returncode == 0
        if not online:
            log("offline: syncing locally, will push next time")
        elif heads.stdout.strip():
            if git("pull", "-q", "--rebase", "--autostash", "origin", "main",
                   check=False).returncode:
                git("rebase", "--abort", check=False)
                notify("Pull failed (usually the same file changed on both machines).")
                return

    if stage_only:  # local wins; never writes ~/.claude
        for rel, it in build_items(cfg, manifest).items():
            L = it.read_local()
            if L is None:
                delete_repo(rel)
            elif read_repo(rel) != L:
                write_repo(rel, L)
        git("add", "-A")
        log("staged only")
        return
    new_manifest, conflicts = reconcile(build_items(cfg, manifest), manifest)
    git("add", "-A")
    STATE.write_text(json.dumps(new_manifest, indent=1, sort_keys=True))
    if conflicts:
        notify(f"{len(conflicts)} file(s) changed on both machines; this machine's "
               f"copies were saved as *.conflict-* files (dotfiles: in {DOTFILE_BACKUPS}).")

    if git("diff", "--cached", "--quiet", check=False).returncode:
        if git("commit", "-q", "-m", f"Sync {HOST} {datetime.now():%Y-%m-%d %H:%M}",
               check=False).returncode:
            notify("Privacy scan (ferpa_scan.py) blocked the backup; nothing pushed.")
            return
    if online:
        ahead = git("rev-list", "--count", "origin/main..HEAD", check=False)
        if ahead.returncode or ahead.stdout.strip() != "0":
            if git("push", "-q", "-u", "origin", "main", check=False).returncode:
                notify("Push failed; will retry next sync.")
                return
    LAST_SYNC.write_text(f"{datetime.now():%Y-%m-%d}")


BLOCKLIST = CONF_DIR / "ferpa_blocklist.txt"
BLOCKLIST_NAG = CONF_DIR / "blocklist_nag"
ROSTER_DATES = ["01-25", "09-05"]  # MM-DD after spring/fall add/drop; override with roster_dates


def check_blocklist(cfg):
    """Nag (at most daily) when the blocklist predates this semester's rosters.
    Only when the Canvas blocklist is set up (canvas_course_ids in config.toml)."""
    if not cfg.get("canvas_course_ids"):
        return
    today = datetime.now()
    dates = [tuple(int(x) for x in d.split("-")) for d in cfg.get("roster_dates") or ROSTER_DATES]
    due = max((datetime(y, m, d) for y in (today.year - 1, today.year)
               for m, d in dates if datetime(y, m, d) <= today), default=datetime.min)
    built = datetime.fromtimestamp(BLOCKLIST.stat().st_mtime) if BLOCKLIST.exists() else None
    if built and built >= due:
        return
    if BLOCKLIST_NAG.exists() and BLOCKLIST_NAG.read_text().strip() == f"{today:%Y-%m-%d}":
        return
    BLOCKLIST_NAG.write_text(f"{today:%Y-%m-%d}")
    notify("Privacy blocklist is out of date for this semester's rosters. "
           "Run: uv run --script ~/claude-config/tools/make_blocklist.py")


def find_onedrive():
    """Work/school OneDrive folder on this machine (e.g. "OneDrive - <Org>"), or None."""
    candidates = [os.environ.get("OneDriveCommercial")]
    candidates += sorted(Path.home().glob("OneDrive - *"))
    candidates += sorted((Path.home() / "Library" / "CloudStorage").glob("OneDrive-*"))
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def start_onedrive():
    """Launch the OneDrive client if it isn't running, so the transcript
    backup actually uploads."""
    try:
        if sys.platform == "darwin":
            if subprocess.run(["pgrep", "-x", "OneDrive"], capture_output=True).returncode != 0:
                subprocess.Popen(["open", "-g", "-a", "OneDrive"])  # -g: stay in background
                log("started OneDrive")
        elif WINDOWS:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OneDrive.exe", "/NH"],
                               capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if "OneDrive.exe" in r.stdout:
                return
            exes = [Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/OneDrive/OneDrive.exe",
                    Path(os.environ.get("ProgramFiles", "")) / "Microsoft OneDrive/OneDrive.exe"]
            exe = next((e for e in exes if e.is_file()), None)
            if not exe:
                log("OneDrive.exe not found")
                return
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            try:  # outlive a parent job that kills its children on exit
                subprocess.Popen([str(exe), "/background"], creationflags=flags
                                 | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            except OSError:
                subprocess.Popen([str(exe), "/background"], creationflags=flags)
            log("started OneDrive")
    except OSError as e:
        log(f"OneDrive start failed: {e!r}")


def backup_transcripts(cfg):
    """Nightly one-way copy of transcripts (student data: never git) to
    a cloud-synced folder (e.g. OneDrive), one folder per machine. Nothing is ever deleted there, so it also
    keeps transcripts that Claude Code's own cleanup removes locally."""
    root = cfg.get("transcripts_backup")
    if not root:
        return
    if not Path(root).parent.is_dir():
        notify(f"Transcript backup skipped: {Path(root).parent} not found.")
        return
    dest = Path(root) / HOST
    copied = 0
    sources = [(CLAUDE / "projects", dest / "projects"), (CLAUDE / "history.jsonl", dest / "history.jsonl")]
    for src, dst in sources:
        files = [src] if src.is_file() else src.rglob("*") if src.is_dir() else []
        for f in files:
            if not f.is_file() or f.name in SKIP_NAMES:
                continue
            rel = f.relative_to(src) if f != src else Path()
            if len(rel.parts) > 1 and rel.parts[1] == "memory":
                continue  # memories go through git
            out = dst / rel
            st = f.stat()
            if out.exists() and out.stat().st_size == st.st_size and out.stat().st_mtime >= st.st_mtime:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            copied += 1
    log(f"transcripts: {copied} file(s) copied to {dest}")


def main():
    args = sys.argv[1:]
    if "--enable-dotfiles" in args:
        cfg = load_config()
        cfg["dotfiles"] = True
        save_config(cfg)
        print(f"dotfile sync on for this machine ({PLATFORM}); syncing now")
    if "--background" in args and "--force" not in args:
        # retired SessionEnd hook (machines that haven't rerun install.py): no-op
        return
    if "--background" in args:
        cwd = hook_cwd_from_stdin()
        cmd = [sys.executable, str(Path(__file__).resolve())] + (["--cwd", cwd] if cwd else [])
        kw = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
               | subprocess.CREATE_NO_WINDOW} if WINDOWS else {"start_new_session": True})
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kw)
        return
    cwd = args[args.index("--cwd") + 1] if "--cwd" in args else hook_cwd_from_stdin()
    if "--first-of-day" in args:
        # SessionStart: sync only the first session of the day, but still map
        # a new project folder so its memories join the next sync
        if LAST_SYNC.exists() and LAST_SYNC.read_text().strip() == f"{datetime.now():%Y-%m-%d}":
            cfg = load_config()
            if cwd and add_project(cfg, cwd):
                save_config(cfg)
            return
    if not acquire_lock():
        log("skip: already running")
        return
    try:
        log(f"=== sync on {HOST}")
        if load_config().get("start_onedrive"):
            start_onedrive()
        sync(cwd, stage_only="--stage-only" in args)
        check_blocklist(load_config())
        if "--nightly" in args:
            backup_transcripts(load_config())
    except Exception as e:  # never break a Claude session over a backup
        log(f"ERROR {e!r}")
        notify(f"Sync error: {e}")
    finally:
        LOCK.rmdir()


if __name__ == "__main__":
    main()
