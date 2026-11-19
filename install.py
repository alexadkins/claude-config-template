# /// script
# requires-python = ">=3.11"
# ///
"""Set up claude-config sync on this machine (macOS or Windows).

    uv run --script install.py [--no-prompt]

- turns on the privacy pre-commit hook (ferpa_scan.py)
- maps local Claude projects to repo names (asks about repo-only projects)
- asks for your own names (never flagged by the privacy scan) and an
  optional nightly transcript backup folder (e.g. OneDrive)
- adds a SessionStart hook (first session of the day; blocking, 20s max)
- sets the status line to tools/statusline.py, unless you already have one
- schedules a daily 9pm sync (+ transcript backup if set)
  (launchd on macOS, Task Scheduler on Windows)
Safe to rerun. Run sync.py afterwards for the first sync/restore.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402

LABEL = "com.claude-config.sync"
TASK = "ClaudeConfigSync"


def ask(prompt, prompts):
    return input(prompt).strip() if prompts else ""


def setup_config(cfg, prompts):
    sync.discover(cfg)
    for name in sync.repo_projects():
        if name in cfg["projects"]:
            continue
        p = ask(f"Local folder for project '{name}' (blank = later): ", prompts)
        if p:
            cfg["projects"][name] = str(Path(p).expanduser().resolve())
        else:
            print(f"  unmapped: {name} (it maps itself when you open a session there)")
    if not cfg.get("own_names"):
        p = ask("Your own first/last names, comma-separated (never flagged by the privacy scan): ", prompts)
        names = [n.strip() for n in p.split(",") if n.strip()]
        if names:
            cfg["own_names"] = names
    if not cfg.get("transcripts_backup"):
        od = sync.find_onedrive()
        guess = str(od / "ClaudeTranscripts") if od else ""
        hint = f" [{guess}]" if guess else " (blank = off)"
        p = ask(f"Nightly transcript backup folder, e.g. on OneDrive{hint}: ", prompts) or guess
        if p:
            cfg["transcripts_backup"] = str(Path(p).expanduser())
            if od and Path(p).expanduser().is_relative_to(od):
                cfg["start_onedrive"] = True
        else:
            print("  transcript backup off (set transcripts_backup in config.toml)")
    sync.save_config(cfg)
    print(f"config: {sync.CONFIG}")


def setup_settings(cmd, uv):
    path = sync.CLAUDE / "settings.json"
    s = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = s.setdefault("hooks", {})
    wanted = {"SessionStart": {"type": "command", "command": f"{cmd} --first-of-day", "timeout": 20}}
    for event in ("SessionStart",):
        groups = [g for g in hooks.get(event, [])
                  if not any("claude-config" in h.get("command", "") for h in g.get("hooks", []))]
        groups.append({"hooks": [wanted[event]]})
        hooks[event] = groups
    if "statusLine" not in s:
        # same command on every machine (settings.json syncs), so no absolute paths
        s["statusLine"] = {"type": "command", "command":
                           '"$(command -v uv || echo ~/.local/bin/uv)" run --quiet --script '
                           f'~/{sync.REPO.name}/tools/statusline.py'}
        print("status line: tools/statusline.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    print(f"hooks: SessionStart (first session of the day) in {path}")


def setup_schedule(uv, script):
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        args = "".join(f"<string>{a}</string>"
                       for a in [uv, "run", "--quiet", "--script", script, "--nightly"])
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
""")
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
        print(f"schedule: launchd {LABEL}, daily 9pm")
    elif os.name == "nt":
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "21:00", "/TN", TASK,
                        "/TR", f'"{uv}" run --quiet --script "{script}" --nightly'], check=True)
        print(f"schedule: Task Scheduler {TASK}, daily 9pm")
    else:
        print(f"schedule: not set up on this OS; add a cron job running: {uv} run --script {script} --nightly")


def main():
    prompts = "--no-prompt" not in sys.argv
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found on PATH")
    uv = Path(uv).as_posix()
    script = (sync.REPO / "sync.py").as_posix()
    sync.git("config", "core.hooksPath", "hooks")

    setup_config(sync.load_config(), prompts)
    setup_settings(f'"{uv}" run --quiet --script "{script}"', uv)
    setup_schedule(uv, script)
    print("Optional: privacy blocklist from Canvas rosters, see README")
    print("Next: uv run --script sync.py (first sync/restore)")


if __name__ == "__main__":
    main()
