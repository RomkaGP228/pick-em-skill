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

VRS data is published with a lag (monthly in 2026), and a month-old Elo loses ~60% of the
correction's value. So matches played after the last VRS update should be added with --extra
(collected from Liquipedia / HLTV pages into a small JSON file, see FRESH MATCHES below). The same
file can carry results of the running tournament, day by day.

Usage:
  python elo.py --date 2026-11-24 "Vitality" "Natus Vincere" FURIA ...
  python elo.py --date 2026-11-24 --fill stage.json --extra fresh.json
        # writes rank + elo_adj (+ sd 40 for roster changes) into each team of stage.json
  python elo.py --date 2026-11-24 --top 30
Options: --extra FILE (repeatable), --event-type major|other (default: from --fill file or major),
         --roster "Team=p1,p2,p3,p4,p5" (repeatable), --repo PATH, --json, --no-cache.
Ratings use only matches played BEFORE --date (default: today).

FRESH MATCHES (--extra), JSON list or {"matches": [...]}; one object per series:
  {"date": "2026-10-12", "team1": "FUT", "team2": "Aurora", "winner": "FUT",
   "score": "2-1", "tier": "S", "event": "BLAST Open Fall 2026", "lan": true,
   "roster1": ["..."], "roster2": ["..."]}          # rosters optional
  tier -> event weight: S 1.0, A 0.5, B 0.3, C 0.15 (or give "event_weight" directly).
  "winner" may be omitted if "score" is given ("2-1" = team1 won). Matches already in the VRS data
  are skipped automatically.

ROSTER CHECK: give the tournament roster per team ("roster": [...] in the --fill file, or
--roster). Players not in the last roster seen in the data -> warning and "sd": 40.
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
from vrs_ranks import ensure_repo, match as match_name, norm, core  # noqa: E402

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


# ------------------------------------------------------------------ fresh matches
TIER_WEIGHT = {"S": 1.0, "A": 0.5, "B": 0.3, "C": 0.15, "D": 0.1, "Q": 0.15}


def _canon_factory(table, hist_names):
    by_norm, by_core = {}, {}
    for nm in hist_names:
        by_norm.setdefault(norm(nm), nm)
        by_core.setdefault(core(nm), nm)
    unknown = set()

    def canon(name):
        if name in hist_names:
            return name
        row = match_name(name, table)
        if row:
            return row[2]
        hit = by_norm.get(norm(name)) or by_core.get(core(name))
        if hit:
            return hit
        unknown.add(name)
        return name
    canon.unknown = unknown
    return canon


def load_extra(paths, table, history):
    """Normalise fresh matches and drop the ones already present in the VRS history."""
    hist_names = {m["a"] for m in history} | {m["b"] for m in history}
    canon = _canon_factory(table, hist_names)
    known = defaultdict(list)
    for m in history:
        known[(m["a"], m["b"], m["winner"])].append(dt.date.fromisoformat(m["date"]).toordinal())
    out, skipped = [], 0
    for path in paths:
        raw = json.load(open(path, encoding="utf-8"))
        items = raw.get("matches", []) if isinstance(raw, dict) else raw
        for i, it in enumerate(items):
            t1, t2 = canon(it["team1"]), canon(it["team2"])
            win = it.get("winner")
            if not win and it.get("score"):
                s1, s2 = [int(x) for x in re.split(r"[-:]", str(it["score"]))[:2]]
                win = it["team1"] if s1 > s2 else it["team2"]
            if not win:
                sys.exit(f"{path}: match #{i + 1} has neither winner nor score")
            win = canon(win)
            if win not in (t1, t2):
                sys.exit(f"{path}: match #{i + 1}: winner '{it.get('winner')}' is not one of the two teams")
            a, b = sorted([t1, t2])
            dn = dt.date.fromisoformat(it["date"]).toordinal()
            if any(abs(dn - k) <= 1 for k in known.get((a, b, win), [])):
                skipped += 1
                continue
            ew = it.get("event_weight")
            if ew is None:
                ew = TIER_WEIGHT.get(str(it.get("tier", "A")).upper()[:1], 0.5)
            rosters = {t1: it.get("roster1"), t2: it.get("roster2")}
            out.append({"date": it["date"], "a": a, "b": b, "winner": win, "lan": it.get("lan"),
                        "ew": float(ew), "ra": rosters.get(a), "rb": rosters.get(b), "extra": True,
                        "event": it.get("event", "")})
    if canon.unknown:
        print("# WARNING: teams in --extra not found in VRS data (treated as new teams, Elo 1400): "
              + ", ".join(sorted(canon.unknown))
              + ". If any of them is a known team under another name, rename it in the file.", file=sys.stderr)
    return out, skipped


# ------------------------------------------------------------------ Elo
def run_elo(history, before_date):
    """Elo after all matches strictly before `before_date`.
    Returns ratings, matches played, matches since last roster change, last match date,
    last known roster, and per team (number of fresh/extra matches, last fresh date)."""
    R, roster, n, since = {}, {}, defaultdict(int), defaultdict(lambda: 99)
    last, fresh = {}, defaultdict(lambda: [0, None])
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
            if m.get("extra"):
                fresh[t][0] += 1
                fresh[t][1] = m["date"]
    return R, n, since, last, roster, fresh


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


def roster_check(entry_roster, known):
    """Players in the tournament roster that were not in the last roster seen in the data."""
    if not entry_roster or not known:
        return None
    k = {norm(p) for p in known}
    return [p for p in entry_roster if norm(p) not in k]


IDLE_WARN_DAYS = 30


def describe(name, table, st, event_type, entry_roster=None, ref_date=None):
    R, n, since, last, roster, fresh, alpha = st
    row = match_name(name, table)
    vname = row[2] if row else None
    rank = row[0] if row else None
    elo = R.get(vname) if vname else None
    rc = since.get(vname, 99) if vname else 99
    lm = last.get(vname) if vname else None
    idle = (dt.date.fromisoformat(ref_date) - dt.date.fromisoformat(lm)).days if (lm and ref_date) else None
    new_players = roster_check(entry_roster, roster.get(vname)) if vname else None
    return {"name": name, "vrs_name": vname, "rank": rank, "vrs_points": row[1] if row else None,
            "elo": round(elo, 1) if elo is not None else None,
            "elo_adj": round(elo_adjustment(rank, elo, alpha, event_type), 1) if rank else 0.0,
            "matches": n.get(vname, 0) if vname else 0,
            "fresh_matches": fresh[vname][0] if vname in fresh else 0,
            "last_match": lm,
            "days_without_match": idle,
            "since_roster_change": rc if rc < 99 else None,
            "known_roster": roster.get(vname) if vname else None,
            "new_players": new_players}


def parse_roster_args(items):
    out = {}
    for it in items or []:
        team, _, players = it.partition("=")
        out[team.strip()] = [p.strip() for p in players.split(",") if p.strip()]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("teams", nargs="*")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--repo")
    ap.add_argument("--fill", help="stage/bracket/match JSON: write rank, elo_adj (and sd) into its teams")
    ap.add_argument("--extra", action="append", default=[], help="fresh matches JSON (repeatable)")
    ap.add_argument("--event-type", choices=["major", "other"])
    ap.add_argument("--roster", action="append", help='"Team=p1,p2,p3,p4,p5" tournament roster (repeatable)')
    ap.add_argument("--top", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    repo = ensure_repo(a.repo)
    hist = load_history(repo, not a.no_cache)
    vrs_end = max((m["date"] for m in hist if m["date"] < a.date), default="?")
    snap, table = ranks_before(repo, a.date)
    extra, skipped = load_extra(a.extra, table, hist) if a.extra else ([], 0)
    allm = sorted(hist + extra, key=lambda m: m["date"])
    R, n, since, last, roster, fresh = run_elo(allm, a.date)
    alpha = table_alpha(table, R)
    st = (R, n, since, last, roster, fresh, alpha)
    extra_end = max((m["date"] for m in extra if m["date"] < a.date), default=None)
    print(f"# VRS snapshot {snap}; VRS match data until {vrs_end}"
          + (f"; + {len(extra)} fresh matches until {extra_end} ({skipped} duplicates skipped)" if extra else
             "; no fresh matches added (--extra)"), file=sys.stderr)
    gap = (dt.date.fromisoformat(a.date) - dt.date.fromisoformat(extra_end or vrs_end)).days if vrs_end != "?" else 0
    if gap > 7:
        print(f"# WARNING: the newest match used is {gap} days before {a.date}. Add results since then with --extra "
              "(a month-old Elo loses most of its value).", file=sys.stderr)
    rosters = parse_roster_args(a.roster)
    cfg = json.load(open(a.fill, encoding="utf-8")) if a.fill else None
    event_type = a.event_type or (cfg.get("event_type") if cfg else None) or "major"
    notes = []

    if cfg is not None:
        rows = []
        for t in cfg["teams"]:
            d = describe(t["name"], table, st, event_type, t.get("roster") or rosters.get(t["name"]), a.date)
            if d["rank"] is None:
                print(f"WARNING: '{t['name']}' not found in VRS — set rank manually", file=sys.stderr)
            else:
                t["rank"] = d["rank"]
                t["elo_adj"] = d["elo_adj"]
            why = []
            if d["new_players"]:
                why.append(f"new in roster vs data: {', '.join(d['new_players'])}")
            if d["since_roster_change"] is not None and d["since_roster_change"] < 5:
                why.append(f"roster changed {d['since_roster_change']} matches ago")
            if why:
                if "sd" not in t:
                    t["sd"] = 40
                if not t.get("note"):
                    t["note"] = "; ".join(why) + " (sd 40)"
                notes.append(f"{t['name']}: " + "; ".join(why) + f" -> sd {t['sd']}")
            rows.append(d)
        json.dump(cfg, open(a.fill, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"# wrote rank + elo_adj{' + sd' if notes else ''} into {a.fill} (event_type={event_type})", file=sys.stderr)
    elif a.top:
        rows = [describe(r[2], table, st, event_type, None, a.date) for r in table[:a.top]]
    else:
        if not a.teams:
            ap.error("give team names, --fill or --top")
        rows = [describe(t, table, st, event_type, rosters.get(t), a.date) for t in a.teams]
        for d in rows:
            if d["new_players"]:
                notes.append(f"{d['name']}: new in roster vs data: {', '.join(d['new_players'])} -> consider sd 40")

    for d in rows:
        if d["days_without_match"] is not None and d["days_without_match"] > IDLE_WARN_DAYS:
            notes.append(f"{d['name']}: no matches for {d['days_without_match']} days (last {d['last_match']}) — "
                         "check for roster news and results missing from the data; no automatic penalty "
                         "(backtest: inactivity alone did not change accuracy)")
    if a.json:
        print(json.dumps({"vrs_snapshot": snap, "vrs_data_until": vrs_end, "fresh_until": extra_end,
                          "event_type": event_type, "teams": rows, "notes": notes}, ensure_ascii=False, indent=2))
        return
    print("| Team | VRS rank | Elo | elo_adj | Matches (fresh) | Last match (days ago) | Since roster change |")
    print("|---|---|---|---|---|---|---|")
    for d in rows:
        rc = d["since_roster_change"]
        flag = f"{rc} ⚠" if rc is not None and rc < 5 else (rc if rc is not None else "—")
        if d["new_players"]:
            flag = f"new: {', '.join(d['new_players'])} ⚠"
        nm = d["name"] if d["vrs_name"] in (d["name"], None) else f"{d['name']} ({d['vrs_name']})"
        print(f"| {nm if d['vrs_name'] else d['name'] + ' — NOT FOUND'} | {d['rank'] or '—'} | {d['elo'] or '—'} | "
              f"{d['elo_adj']:+.0f} | {d['matches']} ({d['fresh_matches']}) | {d['last_match'] or '—'}"
              f"{'' if d['days_without_match'] is None else ' (' + str(d['days_without_match']) + ('d ⚠)' if d['days_without_match'] > IDLE_WARN_DAYS else 'd)')} | {flag} |")
    for line in notes:
        print(f"⚠ {line}")
    print(f"\nelo_adj: VRS top-20{' (+ ranks 21-50 at half weight, non-Major event)' if event_type == 'other' else ''}. "
          "⚠ = roster change: rating uncertainty sd 40.", file=sys.stderr)


if __name__ == "__main__":
    main()
