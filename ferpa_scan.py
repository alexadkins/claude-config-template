# /// script
# requires-python = ">=3.11"
# ///
"""Block commits containing student identifiers or secrets (run by hooks/pre-commit).

Checks the staged version of each added/changed file for:
  1. emails other than your own (git user.email + allow_emails in config.toml)
  2. ID numbers matching id_patterns in config.toml (e.g. your university's
     student-ID format; none by default)
  3. terms in ~/.config/claude-config/ferpa_blocklist.txt (roster names,
     usernames, emails; built by tools/make_blocklist.py, never committed)
  0. credentials (API tokens, private keys; in dotfiles/ also VAR_TOKEN=...)
  4. single first/last names in ferpa_names.txt: in file PATHS case-insensitively
     (e.g. project_ai_reflection_<name>_draft.md), in content only as a
     capitalized word. Common English words and ferpa_allow.txt are skipped.
Underscores, digits, and hyphens count as word boundaries.
Your own names (own_names in config.toml) are never flagged.

    uv run --script ferpa_scan.py          # staged files (pre-commit)
    uv run --script ferpa_scan.py --all    # every tracked file
"""
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CONF = Path.home() / ".config" / "claude-config"
try:
    CFG = tomllib.loads((CONF / "config.toml").read_text(encoding="utf-8"))
except (OSError, ValueError):
    CFG = {}
BLOCKLIST = CONF / "ferpa_blocklist.txt"
NAMES = CONF / "ferpa_names.txt"
ALLOW_FILE = CONF / "ferpa_allow.txt"  # local false positives, one per line
# Names that are also ordinary words: never matched as single tokens.
COMMON = {"will", "mark", "grant", "chase", "hunter", "page", "price", "rose", "hope", "joy", "faith",
          "carter", "mason", "parker", "cook", "baker", "hill", "king", "long", "young", "white",
          "black", "brown", "green", "gray", "grey", "wood", "woods", "ward", "bell", "lane", "stone",
          "rich", "rice", "may", "june", "august", "summer", "april", "dawn", "art", "bill", "case",
          "lead", "reed", "ray", "glenn", "frank", "sky", "storm", "river", "brook", "brooks", "banks",
          "cash", "sage", "jade", "ruby", "pearl", "early", "small", "short", "little", "love", "best",
          "hall", "park", "field", "fields", "rivers", "wells", "fox", "wolf", "bird", "swift", "noble",
          "wise", "golden", "light", "moore", "more", "kind", "sharp", "hart", "heart", "street", "west",
          "north", "south", "east", "march", "gold", "silver", "sterling", "major", "miles", "drew",
          "jean", "penny", "tate", "cole", "nash", "shaw", "dean", "lord", "pope", "duke", "earl",
          "kent", "york", "jordan", "lincoln", "austin", "dallas", "paris", "london", "china", "india",
          "jersey", "carolina", "georgia", "virginia", "florida", "logan", "pierce", "cross", "blue",
          "neil", "reid", "lamb", "fish", "cotton", "mills", "miller", "smith", "turner", "walker",
          "porter", "fisher", "taylor", "cooper", "hunt", "bond", "booth", "cannon", "child", "coleman",
          "data", "test", "user", "team", "sprint", "story", "spec", "code", "card", "lab", "api",
          "none", "tree", "archer"}
COMMON |= {n.lower() for n in CFG.get("own_names", [])}
OK_MARK = "ferpa-ok"  # put on a line to skip the single-name check there (e.g. made-up example names)
B = r"(?<![A-Za-z])"
E = r"(?![A-Za-z])"


def _git_email():
    try:
        return subprocess.run(["git", "config", "user.email"], capture_output=True,
                              text=True).stdout.strip()
    except OSError:
        return ""


_allowed = ["noreply@anthropic.com", "git@github.com", _git_email(), *CFG.get("allow_emails", [])]
ALLOW = re.compile("|".join(re.escape(e) for e in _allowed if e), re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ID_PATS = [re.compile(p) for p in CFG.get("id_patterns", [])]
# Credentials (all files): GitHub, Anthropic/OpenAI, AWS, Slack, Canvas tokens, private keys
SECRET = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-(?:ant-)?[A-Za-z0-9_-]{20,}"
                    r"|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|\d{4,5}~[A-Za-z0-9]{40,})"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
# dotfiles/ only: a long value assigned to a token/secret/password/key variable
SECRET_ASSIGN = re.compile(r"(?i)\w*(?:token|secret|passw(?:or)?d|api_?key)\w*\s*[=:]\s*['\"]?[^\s'\"$]{12,}")


def blocklist_pattern():
    if not BLOCKLIST.is_file():
        return None
    terms = [t.strip() for t in BLOCKLIST.read_text(encoding="utf-8").splitlines() if t.strip()]
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"{B}(?:{alts}){E}", re.I)


def read_terms(path):
    return [t.strip() for t in path.read_text(encoding="utf-8").splitlines() if t.strip()] if path.is_file() else []


def names_patterns():
    allow = {t.lower() for t in read_terms(ALLOW_FILE)} | COMMON
    toks = sorted({t for t in read_terms(NAMES) if t.lower() not in allow}, key=len, reverse=True)
    if not toks:
        return None, None
    alts = "|".join(re.escape(t) for t in toks)
    caps = "|".join(re.escape(t[0].upper() + t[1:].lower()) for t in toks)
    return re.compile(rf"{B}(?:{alts}){E}", re.I), re.compile(rf"{B}(?:{caps}){E}")


def scan(name, text, roster, path_pat=None, word_pat=None):
    hits = []
    if path_pat:
        hits += [f"{name}: filename has '{m}'" for m in path_pat.findall(name)]
        if roster:
            hits += [f"{name}: filename has '{m}'" for m in roster.findall(name)]
    for n, line in enumerate(text.splitlines(), 1):
        found = [e for e in EMAIL.findall(line) if not ALLOW.fullmatch(e)]
        for pat in ID_PATS:
            found += pat.findall(line)
        if roster:
            found += roster.findall(line)
        if word_pat and OK_MARK not in line:
            found += word_pat.findall(line)
        secrets = [m.group(0) for m in SECRET.finditer(line)]
        if name.startswith("dotfiles/"):
            secrets += [m.group(0) for m in SECRET_ASSIGN.finditer(line)]
        found += [f"possible secret '{s[:12]}...'" for s in secrets]
        hits += [f"{name}:{n}: {f}" for f in found]
    return hits


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def main():
    all_files = "--all" in sys.argv
    if all_files:
        names = [n for n in git("ls-files", "-z").decode().split("\0") if n]
    else:
        names = [n for n in git("diff", "--cached", "--name-only", "-z",
                                "--diff-filter=ACMR").decode().split("\0") if n]
    roster = blocklist_pattern()
    path_pat, word_pat = names_patterns()
    if roster is None and CFG.get("canvas_course_ids"):
        print(f"warning: no privacy blocklist at {BLOCKLIST} (run tools/make_blocklist.py)")
    hits = []
    for n in names:
        if n in ("ferpa_scan.py", "README.md"):  # pattern docs and made-up examples
            continue
        hits += scan(n, git("show", f":{n}").decode("utf-8", "ignore"), roster, path_pat, word_pat)
    if hits:
        print("Privacy scan: possible student data or secrets:")
        print("\n".join(hits))
        print("Commit blocked. Scrub the source memory, or bypass with "
              "git commit --no-verify if it's a false positive.")
        sys.exit(1)


if __name__ == "__main__":
    main()
