---
description: Write a handoff so this conversation can continue on another machine (or `/handoff pickup` to resume one)
argument-hint: "[optional topic] | pickup [number|topic|all]"
---

If the argument starts with `pickup`, do **Pick up** (anything after `pickup` selects handoffs). Otherwise do **Write**, using the argument (if any) as the topic.

## Write

Save a handoff as a memory in this project's memory directory, so `~/claude-config` syncs it to your other machines.

1. Check the project's git state (`git status -sb`, unpushed commits). Do NOT commit or push without asking; list what's pending instead.
2. Save any durable lessons from this conversation (preferences, corrections, gotchas) as regular memories first. The handoff is temporary; it gets deleted after pickup.
3. Write `handoff_<topic-slug>_<host-short>_<MMDD-HHMM>.md` (e.g. `handoff_api-refactor_mac_1017-1411.md`; host-short = `mac`, `win`, or the short hostname), so handoffs from different agents or machines never collide. Frontmatter `type: project`, at most ~60 lines, with these sections:
   - Header lines: `From <hostname>, <YYYY-MM-DD HH:MM>` and `**Claimed:** no`
   - **Goal**: what we're doing and why, in 1–3 lines
   - **Status**: done / in progress / not started
   - **Decisions**: choices the user made in this conversation, with the reasons, that aren't already in other memories
   - **Next steps**: an ordered, concrete list; the first item is what to do right after pickup
   - **References**: file paths (relative to the repo), issue/assignment/doc ids, artifact links, drafts in progress
   - **Machine-only state**: uncommitted or unpushed work, gitignored files used (e.g. data files, CSVs), and anything that needed this machine's logins or apps
   - **Open questions** for the user
   - **No student or private identifiers**: no names, usernames, emails, or LMS user ids. Use counts, and point to where the details live.
4. Add to MEMORY.md: `- [HANDOFF: <topic>](<file>) — from <host> <date>; resume with /handoff pickup`
5. Sync now instead of waiting for the session to end: `uv run --script ~/claude-config/sync.py`, then check the end of `~/.config/claude-config/sync.log` for NOTIFY/ERROR (e.g. a privacy-scan block; if one fires, scrub the handoff and rerun).
6. Tell the user in a few lines: the handoff file, what's pending in git (and that they need to push it), whether the sync pushed.

## Pick up

1. Sync first: `uv run --script ~/claude-config/sync.py`.
2. Find every `handoff_*.md` in this project's memory directory, and in the other projects' memory folders (`~/.claude/projects/*/memory/`). Show the user one numbered list, oldest first:
   `N. <topic> — <project>, from <host>, <age>; next: <first next step>` plus flags:
   - `CLAIMED by <host> <time>` if its **Claimed:** line isn't `no` (no Claimed line = older format, unclaimed). Another session already has it; skip it unless the user says otherwise. A claim older than 12h is probably abandoned: say so.
   - `STALE` if it's older than 7 days: ask whether to resume it or delete it.
3. Selection: if the argument after `pickup` names a number, topic, or `all`, use that. Otherwise, if there's exactly one unclaimed handoff, use it; if there are several, ask which (one, several, or all). Suggest separate sessions for unrelated topics so each keeps its own context; take several here only if the user wants that.
4. Claim each selected handoff right away: set `**Claimed:** <hostname> <YYYY-MM-DD HH:MM>`, then sync, so a session on the other machine sees the claim.
5. For each selected handoff: read it, then check the project's git state against **Machine-only state**. Is the repo behind its remote (`git fetch`, then status)? Does it have the expected commits? Do the referenced files exist? Report anything missing (e.g. a gitignored file that's only on the other machine).
6. Give the user a short summary per handoff (goal, status, first next step, open questions) and confirm before continuing. With several, ask which to start with.
7. Once the user confirms a handoff's work has resumed, delete that handoff file and its MEMORY.md line, then sync (any durable info is already in regular memories). If the user stops before resuming, reset its line to `**Claimed:** no` and sync, so it can be picked up again.
