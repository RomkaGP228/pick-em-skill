#!/usr/bin/env python3
"""
Automatic team-strength inputs from Valve's VRS repository: VRS rank, own Elo, the recent-form
correction `elo_adj`, and roster-change recency.

Why: the VRS rank (the model's base) accumulates six months of results and lags behind sudden
form changes. A series Elo built from every match in the VRS data (~36k matches since 2024,
LAN and online, bigger events weighted more) reacts faster. Elo on its own is bad at comparing
regions (teams mostly play inside their region), so it is used only as a correction for VRS
top-20 teams: elo_adj = 0.412 * (Elo - (alpha - 100*ln rank)), capped at ±100 rating points,
with alpha re-centred on the current VRS top-20 (so the average correction is ~0).
The coefficient was fitted outside Majors (2025 LAN, top-20 pairs). It improved 2026 top-20
LAN matches (log-loss 0.663 -> 0.644) and all 318 Major matches (0.646 -> 0.640; Stage 3
0.624 -> 0.599). See references/calibration.md.

Usage:
  python elo.py --date 2026-11-24 "Vitality" "Natus Vincere" FURIA ...
  python elo.py --date 2026-11-24 --fill stage.json     # writes rank + elo_adj into each team
  python elo.py --date 2026-11-24 --top 30              # Elo table of the VRS top 30
Options: --repo PATH (existing clone), --json, --no-cache.
Ratings use only matches played BEFORE --date (default: today).
"""
import argparse
import bisect
import datetime as dt
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from model import elo_adjustment, elo_alpha  # noqa: E402
from vrs_ranks import ensure_repo, match as match_name  # noqa: E402

ELO = {"K": 32.0, "online_weight": 1.0, "init": 1400.0, "event_weight_pow": 0.5}
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d{4}-\d\d-\d\d)\s*\|\s*([^|]+?)\s*\|\s*([WL])\s*\|"
                 r"\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|")


# ------------------------------------------------------------------ history
def build_history(repo):
    """All matches from the per-team detail files of every VRS snapshot, de-duplicated."""
    folders = sorted(set(glob.glob(os.path.join(repo, "live", "*", "details", "*")) +
                         glob.glob(os.path.join(repo, "invitation", "*", "details", "*"))),
                     key=os.path.basename)
    seen, snaps = set(), []
    for f in folders:
        if os.path.basename(f) not in seen:
            seen.add(os.path.basename(f))
            snaps.append(f)
    master = {}
    for f in snaps:
        per = {}
        for fp in glob.glob(os.path.join(f, "*.md")):
            team = None
            for line in open(fp, encoding="utf-8"):
                if line.startswith("Team Name:"):
                    team = line.split(":", 1)[1].replace("<br />", "").strip()
                    continue
                m = ROW.match(line)
                if not (m and team):
                    continue
                mid, date, opp, wl = int(m.group(2)), m.group(3), m.group(4), m.group(5)
                ew, lan, roster = m.group(7), m.group(10), m.group(12)
                rec = per.setdefault(mid, {"id": mid, "date": date, "teams": {}, "winner": None, "lan": None, "ew": None})
                rec["teams"][team] = [r.strip() for r in roster.split(",") if r.strip()]
                rec["teams"].setdefault(opp, rec["teams"].get(opp))
                rec["winner"] = team if wl == "W" else opp
                if wl == "W":
                    rec["lan"] = lan.strip() != "-"
                    rec["ew"] = None if ew.strip() == "-" else float(ew)
        cnt = defaultdict(int)
        for rec in sorted(per.values(), key=lambda r: (r["date"], -r["id"])):
            names = sorted(rec["teams"])
            if len(names) != 2:
                continue
            base = (rec["date"], names[0], names[1], rec["winner"])
            key = base + (cnt[base],)
            cnt[base] += 1
            old = master.get(key)
            if old is None:
                master[key] = {"date": rec["date"], "a": names[0], "b": names[1], "winner": rec["winner"],
                               "lan": rec["lan"], "ew": rec["ew"], "ra": rec["teams"][names[0]],
                               "rb": rec["teams"][names[1]], "ord": -rec["id"]}
            else:
                for fld in ("lan", "ew"):
                    if old[fld] is None and rec[fld] is not None:
                        old[fld] = rec[fld]
                for side, fld in ((names[0], "ra"), (names[1], "rb")):
                    if not old[fld] and rec["teams"][side]:
                        old[fld] = rec["teams"][side]
    out = sorted(master.values(), key=lambda r: (r["date"], r["ord"]))
    for r in out:
        r.pop("ord")
    return out


def load_history(repo, use_cache=True):
    try:
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        head = ""
    key = hashlib.sha1((os.path.abspath(repo) + head).encode()).hexdigest()[:12]
    cache = os.path.join(tempfile.gettempdir(), f"cs2pickem_history_{key}.json")
    if use_cache and head and os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    hist = build_history(repo)
    if head:
        json.dump(hist, open(cache, "w", encoding="utf-8"))
    return hist


# ------------------------------------------------------------------ Elo
def run_elo(history, before_date):
    """Elo after all matches strictly before `before_date`.
    Returns ratings, matches played, matches since last roster change, last match date."""
    R, roster, n, since = {}, {}, defaultdict(int), defaultdict(lambda: 99)
    last = {}
    for m in history:
        if m["date"] >= before_date:
            break
        a, b = m["a"], m["b"]
        for t, ro in ((a, m["ra"]), (b, m["rb"])):
            R.setdefault(t, ELO["init"])
            if ro:
                old = roster.get(t)
                if old is not None and set(ro) - set(old):
                    since[t] = 0
                roster[t] = ro
        ea = 1 / (1 + 10 ** (-(R[a] - R[b]) / 400))
        w = 1.0 if m["lan"] else ELO["online_weight"]
        if m["ew"] is not None:
            w *= max(m["ew"], 0.05) ** ELO["event_weight_pow"]
        ya = 1.0 if m["winner"] == a else 0.0
        R[a] += ELO["K"] * w * (ya - ea)
        R[b] -= ELO["K"] * w * (ya - ea)
        for t in (a, b):
            n[t] += 1
            since[t] += 1
            last[t] = m["date"]
        # roster of this match is now the team's known roster
    return R, n, since, last, roster


# ------------------------------------------------------------------ VRS rank at date
def ranks_before(repo, date):
    files = {}
    for p in glob.glob(os.path.join(repo, "live", "*", "standings_global_*.md")) + \
             glob.glob(os.path.join(repo, "invitation", "*", "standings_global_*.md")):
        d = re.search(r"(\d{4})_(\d\d)_(\d\d)", os.path.basename(p)).group(0).replace("_", "-")
        files.setdefault(d, p)
    dates = sorted(files)
    i = bisect.bisect_left(dates, date) - 1
    if i < 0:
        sys.exit(f"No VRS snapshot before {date}")
    table = []
    for line in open(files[dates[i]], encoding="utf-8"):
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", line)
        if m:
            table.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    return dates[i], table


def table_alpha(table, R):
    return elo_alpha([(r[0], R.get(r[2])) for r in table])


def describe(name, table, R, n, since, last, alpha):
    row = match_name(name, table)
    vname = row[2] if row else None
    rank = row[0] if row else None
    elo = R.get(vname) if vname else None
    out = {"name": name, "vrs_name": vname, "rank": rank, "vrs_points": row[1] if row else None,
           "elo": round(elo, 1) if elo is not None else None,
           "elo_adj": round(elo_adjustment(rank, elo, alpha), 1) if rank else 0.0,
           "matches": n.get(vname, 0) if vname else 0,
           "since_roster_change": since.get(vname) if vname and since.get(vname, 99) < 99 else None,
           "last_match": last.get(vname) if vname else None}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("teams", nargs="*")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--repo")
    ap.add_argument("--fill", help="stage/bracket/match JSON: write rank + elo_adj into its teams")
    ap.add_argument("--top", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    repo = ensure_repo(a.repo)
    hist = load_history(repo, not a.no_cache)
    R, n, since, last, _ = run_elo(hist, a.date)
    snap, table = ranks_before(repo, a.date)
    alpha = table_alpha(table, R)
    data_end = max((m["date"] for m in hist if m["date"] < a.date), default="?")
    print(f"# VRS snapshot {snap}; Elo from {len(hist)} matches, latest used {data_end}", file=sys.stderr)

    if a.fill:
        cfg = json.load(open(a.fill, encoding="utf-8"))
        rows = []
        for t in cfg["teams"]:
            d = describe(t["name"], table, R, n, since, last, alpha)
            if d["rank"] is None:
                print(f"WARNING: '{t['name']}' not found in VRS — set rank manually", file=sys.stderr)
            else:
                t["rank"] = d["rank"]
                t["elo_adj"] = d["elo_adj"]
            rows.append(d)
        json.dump(cfg, open(a.fill, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"# wrote rank + elo_adj into {a.fill}", file=sys.stderr)
    elif a.top:
        rows = [describe(r[2], table, R, n, since, last, alpha) for r in table[:a.top]]
    else:
        if not a.teams:
            ap.error("give team names, --fill or --top")
        rows = [describe(t, table, R, n, since, last, alpha) for t in a.teams]

    if a.json:
        print(json.dumps({"vrs_snapshot": snap, "elo_data_until": data_end, "teams": rows}, ensure_ascii=False, indent=2))
        return
    print("| Team | VRS name | VRS rank | Elo | elo_adj | Matches | Since roster change | Last match |")
    print("|---|---|---|---|---|---|---|---|")
    for d in rows:
        rc = d["since_roster_change"]
        flag = f"{rc} ⚠" if rc is not None and rc < 5 else (rc if rc is not None else "—")
        print(f"| {d['name']} | {d['vrs_name'] or 'NOT FOUND'} | {d['rank'] or '—'} | {d['elo'] or '—'} | "
              f"{d['elo_adj']:+.0f} | {d['matches']} | {flag} | {d['last_match'] or '—'} |")
    print("\nelo_adj applies to VRS top-20 only. ⚠ = roster change within the last 5 matches: "
          "consider \"sd\": 40 for that team.", file=sys.stderr)


if __name__ == "__main__":
    main()
