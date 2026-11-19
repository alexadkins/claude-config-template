# claude-config (template)

Two-way sync of your Claude Code setup across machines (macOS + Windows)
through a **private** git repo: global `CLAUDE.md`, `settings.json` (minus
hooks), custom commands and agents, every project's `memory/` folder, and
optionally your shell/terminal dotfiles. No transcripts or credentials go to
git, and a pre-commit privacy scan blocks student identifiers and secrets.

## Make your own copy (keep it private)

Click **Use this template → Create a new repository** and choose
**Private**. Don't fork: a fork of a public repo is public, and this repo
will hold your memories and settings.

## Setup on each machine (Windows: Git Bash or PowerShell)

1. Install [uv](https://docs.astral.sh/uv/). It fetches Python 3.11+ itself;
   always run the scripts with `uv run --script`. On Windows, use the official
   installer (it puts uv in `~/.local/bin`, which sync.py adds to PATH for
   Task Scheduler), then reopen the terminal:

       powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

2. Make sure `git push` to your copy works without a prompt (an SSH key
   with no passphrase, or a credential helper), since the 9pm sync runs
   unattended.

3. Clone into your home folder as `~/claude-config` (the status line
   command assumes that path) and install:

       git clone git@github.com:<you>/claude-config.git ~/claude-config
       cd ~/claude-config
       uv run --script install.py
       uv run --script sync.py

`install.py` is safe to rerun. On the first machine, the first sync pushes
your existing `~/.claude` setup; on later machines it pulls it down. A file
that already exists on both sides with different content is kept as a
`*.conflict-<host>.*` copy for you to merge by hand.

## Layout

- `claude/global/`: `CLAUDE.md`, `settings.json`, `agents/`, `commands/`
  (written by sync; edit them in `~/.claude`, not here)
- `claude/global/commands/handoff.md`: the `/handoff` command (see below)
- `claude/projects/<Name>/memory/`: each project's memories under a short name
- `dotfiles/<platform>/`: shell/terminal config, if you turn it on
- `sync.py`: the sync; `install.py`: per-machine setup
- `ferpa_scan.py` + `hooks/pre-commit`: privacy scan on every commit
- `tools/make_blocklist.py`: optional roster blocklist from Canvas
- `tools/statusline.py`: status line (model · folder · branch · ctx % · plan usage)

Per machine, not committed (`~/.config/claude-config/`): `config.toml`,
`state.json` (last-synced manifest), blocklists, `sync.log`.

## How sync works

Runs on the first Claude Code session of the day (SessionStart hook,
blocking, max 20s; later sessions that day skip it), daily at 9pm, and on
`/handoff`. Each run exports local changes and commits them (privacy scan),
pulls with rebase, applies incoming adds/edits/deletes to `~/.claude`, and
pushes. A per-machine manifest records each file's hash at the last sync, so
changes and deletes are attributed to the right machine.

Claude Code names project folders after their full path, which differs per
machine. The repo stores projects by folder name instead: opening a session
in `~/code/thesis` on one machine and `D:\code\thesis` on another maps both to
`claude/projects/thesis/`. Mappings live in `config.toml` under `[projects]`;
edit them if two machines use different folder names.

`settings.json` syncs without its `hooks` key, since hook commands contain
machine-specific paths; each machine keeps its own.

Notifications (macOS banner / Windows message box) when something needs you:
- Privacy scan blocked a commit: scrub the source memory and rerun sync.
- Pull failed: offline, or the same file changed on both machines. Local
  changes stay committed; resolve with `git pull --rebase` here, then rerun.
- `*.conflict-<host>.*` files: merge by hand and delete the conflict copy.
  (`MEMORY.md` indexes are merged automatically.)

## config.toml

`install.py` writes this. All keys are optional:

```toml
own_names = ["Pat", "Smith"]          # your names: never flagged by the scan
allow_emails = ["pat@school.edu"]     # your emails (git user.email is always allowed)
id_patterns = ['\bC\d{8}\b']          # student-ID formats to block (regex)
transcripts_backup = "C:\\Users\\pat\\OneDrive - School\\ClaudeTranscripts"
start_onedrive = true                 # launch OneDrive before syncing
dotfiles = true                       # set by sync.py --enable-dotfiles
canvas_url = "https://school.instructure.com/api/v1"
canvas_course_ids = ["12345"]
canvas_token_env = "CANVAS_TOKEN"     # env var with your Canvas token
roster_dates = ["01-25", "09-05"]     # MM-DD: remind to rebuild the blocklist after

[projects]
"thesis" = "C:\\Users\\pat\\code\\thesis"
```

## Privacy scan

`hooks/pre-commit` runs `ferpa_scan.py` on every commit and blocks:
- emails that aren't yours
- IDs matching `id_patterns`
- credentials (GitHub/Anthropic/OpenAI/AWS/Slack/Canvas tokens, private
  keys; in `dotfiles/` also `*_TOKEN=`-style assignments)
- roster terms from the blocklist, and single student names in file paths
  or as capitalized words (common English words are skipped; add local
  false positives to `~/.config/claude-config/ferpa_allow.txt`, or put
  `ferpa-ok` on the line)

Check every tracked file: `uv run --script ferpa_scan.py --all`.
Bypass a false positive: `git commit --no-verify`.

**Roster blocklist (optional).** Set the `canvas_*` keys in config.toml and
your token in the environment, then each semester once rosters settle:

    uv run --script tools/make_blocklist.py [--extra other_names.txt]

Terms accumulate across semesters and never leave the machine. Sync reminds
you (at most daily) when the blocklist predates the latest `roster_dates`.

## Transcript backup (optional)

Transcripts never go to git (size, and they may contain student data). With
`transcripts_backup` set, the 9pm job copies `~/.claude/projects/**` (minus
memory) and `history.jsonl` one way into that folder, one subfolder per
machine. It never deletes, so it also keeps transcripts that Claude Code's
own cleanup removes. Point it at institution-approved storage if your
transcripts may include student data.

## Dotfiles (opt-in per machine)

Windows: `.bashrc`, `.bash_profile`, `.inputrc`, `.minttyrc`,
`.config/starship.toml`, `.config/powershell/profile.ps1`, Windows Terminal
`settings.json`. Mac: `.zshrc`, `.vimrc`, `.config/kitty/kitty.conf`,
`.config/starship.toml`. Files that don't exist are skipped; add more in
`DOTFILE_PATHS` in `sync.py`. `~/.ssh/config` is deliberately not synced.
Keep secrets in an unsynced file (e.g. `~/.zshrc.local`) and source it.

Turn it on with `uv run --script sync.py --enable-dotfiles`. Enable it first
on the machine whose files should win: on another machine's first sync, the
repo's copy replaces the local one. Every overwritten local dotfile is backed
up to `~/.config/claude-config/dotfile-backups/<timestamp>/`, and dotfiles are
never deleted locally.

## /handoff: continue a conversation on another machine

`/handoff [topic]` saves a short handoff memory (goal, status, decisions,
next steps, machine-only state) and syncs. On the other machine,
`/handoff pickup` syncs, lists open handoffs, claims one so no other session
takes it, and resumes. On the same machine, plain `/resume` is enough.

## Schedule

launchd (macOS) and Task Scheduler (Windows) keep the 9pm job across
restarts. A run missed while asleep runs on wake (macOS); one missed while
powered off is skipped, and the next session's SessionStart sync covers it.
On Linux, install.py prints a command to add as a cron job.
