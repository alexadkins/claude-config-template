# /// script
# requires-python = ">=3.11"
# ///
"""Claude Code status line: model · folder · git branch(*dirty) · context %.

Reads the status line JSON on stdin. Portable (macOS + Windows); settings.json
points at this file via ~/claude-config, so both machines share it.
"""
import json
import subprocess
import sys
from pathlib import Path

DIM, CYAN, PURPLE, YELLOW, RED, RESET = (
    "\033[2m", "\033[36m", "\033[35m", "\033[33m", "\033[31m", "\033[0m")


def git_branch(cwd):
    def run(*args):
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        branch = run("rev-parse", "--abbrev-ref", "HEAD")
        if not branch:
            return None
        dirty = run("status", "--porcelain", "--untracked-files=no")
        return branch + ("*" if dirty else "")
    except (OSError, subprocess.TimeoutExpired):
        return None


def context_pct(data):
    cw = data.get("context_window") or {}
    if cw.get("used_percentage") is not None:
        return float(cw["used_percentage"])
    size = cw.get("context_window_size") or 200_000
    usage = cw.get("current_usage")
    if not usage:  # fall back to the last assistant turn in the transcript
        try:
            lines = Path(data["transcript_path"]).read_text(encoding="utf-8").splitlines()
        except (KeyError, OSError, TypeError):
            return None
        for line in reversed(lines):
            try:
                msg = json.loads(line).get("message") or {}
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("usage"):
                usage = msg["usage"]
                break
    if not usage:
        return None
    used = sum(usage.get(k) or 0 for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return 100 * used / size


def main():
    data = json.loads(sys.stdin.read() or "{}")
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or "."
    parts = [(data.get("model") or {}).get("display_name") or "Claude",
             f"{CYAN}{Path(cwd).name}{RESET}"]
    branch = git_branch(cwd)
    if branch:
        parts.append(f"{PURPLE}{branch}{RESET}")
    pct = context_pct(data)
    if pct is not None:
        color = RED if pct >= 80 else YELLOW if pct >= 60 else DIM
        parts.append(f"{color}ctx {pct:.0f}%{RESET}")
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles may default to cp1252
    print(f" {DIM}·{RESET} ".join(parts))


if __name__ == "__main__":
    main()
