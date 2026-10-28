#!/usr/bin/env python3
"""
Look up global Valve Regional Standings (VRS) rank and points for teams at a given date.

Data: github.com/ValveSoftware/counter-strike_regional_standings (cloned on first use).
Monthly "invitation" snapshots are the ones Valve uses for Major invitations and seeding
(e.g. 2025-05-05 for Austin 2025, 2025-11-03 for Budapest 2025, 2026-05-04 for Cologne 2026).

Usage:
  python vrs_ranks.py --date 2026-11-02 "Vitality" "NAVI" "FURIA" ...
  python vrs_ranks.py --date 2026-11-02 --top 40          # print the top 40
  python vrs_ranks.py --list-snapshots
Options:
  --repo PATH     use an existing clone instead of cloning
  --live          also consider weekly "live" snapshots, not just monthly invitation ones
  --json          print JSON (name, matched_name, rank, points)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

REPO_URL = "https://github.com/ValveSoftware/counter-strike_regional_standings.git"


def ensure_repo(path):
    if path and os.path.isdir(path):
        return path
    path = path or os.path.join(tempfile.gettempdir(), "vrs_repo")
    if not os.path.isdir(os.path.join(path, ".git")):
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["git", "-C", path, "pull", "--ff-only"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def snapshots(repo, live=False):
    folders = ["invitation"] + (["live"] if live else [])
    files = []
    for f in folders:
        files += glob.glob(os.path.join(repo, f, "*", "standings_global_*.md"))
    def date_of(p):
        return re.search(r"(\d{4})_(\d\d)_(\d\d)", os.path.basename(p)).group(0).replace("_", "-")
    return sorted(((date_of(p), p) for p in files), key=lambda x: (x[0], "live" in x[1]))


def parse(path):
    out = []
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", line)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    return out


ALIASES = {
    "navi": "natusvincere", "natusvincere": "natusvincere",
    "nip": "ninjasinpyjamas", "pv": "parivision", "bb": "betboom", "betboomteam": "betboom",
    "mongolz": "themongolz", "tmz": "themongolz", "vp": "virtuspro", "gl": "gamerlegion",
    "pua": "passionua", "lvg": "lynnvision", "lynnvisiongaming": "lynnvision",
    "faze": "faze", "fazeclan": "faze", "gaiminggladiators": "gaimingladiators",
    "1w": "1win", "1wteam": "1win",
}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


_NOISE = re.compile(r"\b(team|esports?|gaming|clan|club|gg)\b", re.I)


def core(s):
    """Name without organisation noise words: 'Team Spirit' -> 'spirit', 'FUT Esports' -> 'fut'."""
    c = norm(_NOISE.sub(" ", s))
    return c or norm(s)


def match(name, table):
    n = norm(name)
    n = ALIASES.get(n, n)
    exact = [r for r in table if norm(r[2]) == n]
    if exact:
        return exact[0]
    c = ALIASES.get(core(name), core(name))
    same_core = [r for r in table if core(r[2]) == c]
    if same_core:
        return min(same_core, key=lambda r: r[0])
    # partial: query is the start of the VRS name ("Gaimin" -> "Gaimin Gladiators"),
    # or a VRS name of 4+ chars is contained in the query ("Team Liquid" -> "Liquid")
    part = [r for r in table if len(n) >= 3 and norm(r[2]).startswith(n)]
    part += [r for r in table if len(norm(r[2])) >= 4 and norm(r[2]) in n]
    return min(part, key=lambda r: r[0]) if part else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("teams", nargs="*")
    ap.add_argument("--date", help="YYYY-MM-DD; latest snapshot on or before this date (default: latest)")
    ap.add_argument("--repo")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--top", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list-snapshots", action="store_true")
    a = ap.parse_args()
    repo = ensure_repo(a.repo)
    snaps = snapshots(repo, a.live)
    if a.list_snapshots:
        for d, p in snaps:
            print(d, os.path.relpath(p, repo))
        return
    usable = [s for s in snaps if not a.date or s[0] <= a.date]
    if not usable:
        sys.exit("No snapshot on or before that date")
    date, path = usable[-1]
    table = parse(path)
    print(f"# VRS global snapshot {date} ({os.path.relpath(path, repo)})", file=sys.stderr)
    if a.top:
        rows = [{"name": r[2], "matched_name": r[2], "rank": r[0], "points": r[1]} for r in table[:a.top]]
    else:
        rows = []
        for t in a.teams:
            r = match(t, table)
            rows.append({"name": t, "matched_name": r[2] if r else None,
                         "rank": r[0] if r else None, "points": r[1] if r else None})
    if a.json:
        print(json.dumps({"snapshot": date, "teams": rows}, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            flag = "" if r["matched_name"] in (r["name"], None) else f"  (matched '{r['matched_name']}')"
            print(f"{r['name']}: rank {r['rank']}, points {r['points']}{flag}" if r["rank"] else f"{r['name']}: NOT FOUND")


if __name__ == "__main__":
    main()
