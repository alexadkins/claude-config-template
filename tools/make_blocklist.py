# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""Build the privacy blocklist used by ferpa_scan.py from Canvas rosters.

Pulls student names, sortable names, login ids, and emails from each Canvas
course in config.toml (~/.config/claude-config/config.toml):

    canvas_url = "https://<school>.instructure.com/api/v1"
    canvas_course_ids = ["12345", "67890"]
    canvas_token_env = "CANVAS_TOKEN"   # env var holding your token (default)

Writes ~/.config/claude-config/ferpa_blocklist.txt and ferpa_names.txt
(single first/last names) outside the repo. Terms accumulate across
semesters. Add non-Canvas names (e.g. team rosters) with --extra FILE, one
term per line.

    uv run --script tools/make_blocklist.py [--extra FILE ...]
"""
import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

import requests

CONF_DIR = Path.home() / ".config" / "claude-config"
OUT = CONF_DIR / "ferpa_blocklist.txt"
NAMES_OUT = CONF_DIR / "ferpa_names.txt"  # single first/last-name tokens (matched more narrowly by ferpa_scan.py)
MIN_LEN = 4  # skip short tokens that would match ordinary words


def load_config():
    path = CONF_DIR / "config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def get_students(base_url, token, course_id):
    url = f"{base_url}/courses/{course_id}/users"
    params = {"per_page": 100, "enrollment_role": "StudentEnrollment"}
    headers = {"Authorization": f"Bearer {token}"}
    students = []
    while url:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        students += r.json()
        url, params = r.links.get("next", {}).get("url"), None
    return students


def merge_into(path, new):
    if path.exists():
        new |= {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(new)) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return len(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", action="append", default=[], help="text file of extra terms, one per line")
    args = ap.parse_args()
    cfg = load_config()

    base_url = cfg.get("canvas_url")
    course_ids = cfg.get("canvas_course_ids", [])
    token_env = cfg.get("canvas_token_env", "CANVAS_TOKEN")
    token = os.environ.get(token_env)
    if course_ids and not (base_url and token):
        sys.exit(f"Set canvas_url in config.toml and your Canvas token in ${token_env}")
    if not course_ids and not args.extra:
        sys.exit("Nothing to do: set canvas_course_ids in config.toml or pass --extra FILE")

    terms, names = set(), set()
    for cid in course_ids:
        try:
            students = get_students(base_url, token, cid)
        except requests.RequestException as e:
            print(f"course {cid}: skipped ({e})")
            continue
        for s in students:
            for key in ("name", "sortable_name", "short_name", "login_id", "email"):
                if s.get(key):
                    terms.add(s[key].strip())
            for key in ("name", "sortable_name", "short_name"):
                for tok in re.split(r"[\s,]+", s.get(key) or ""):
                    tok = tok.strip(".()'\"")
                    if len(tok) >= MIN_LEN and tok.isalpha():
                        names.add(tok)
        print(f"course {cid}: {len(students)} students")

    for f in args.extra:
        terms |= {t.strip() for t in Path(f).read_text(encoding="utf-8").splitlines() if t.strip()}

    # Keep your own identifiers out (the scan already allows them)
    own = [n.lower() for n in cfg.get("own_names", [])]

    def mine(t):
        return any(o in t.lower() for o in own)

    terms = {t for t in terms if len(t) >= MIN_LEN and not mine(t)}
    names = {n for n in names if not mine(n)}
    print(f"{merge_into(OUT, terms)} terms -> {OUT}")
    print(f"{merge_into(NAMES_OUT, names)} name tokens -> {NAMES_OUT}")


if __name__ == "__main__":
    main()
