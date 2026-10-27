#!/usr/bin/env python3
"""
Build the Major match dataset from Valve's VRS repository and (re)calibrate the strength model.

Steps it performs:
  1. For every Major in assets/majors_config.json, read all match rows (date, opponent, W/L)
     from the per-team VRS detail files of the `results_snapshot`, keep matches where both
     teams are Major participants and the date is inside a stage window.
  2. Assign Bo format (Swiss deciders Bo3, else Bo1; `all_bo3_stages`; playoffs Bo3; final `final_bo`).
  3. Attach each team's global VRS rank/points from the `strength_snapshot` (latest before the Major).
  4. Fit rating = 1700 - A*ln(rank) and the Bo1 shrink by maximum likelihood (grid search),
     with leave-one-Major-out cross-validation, and the same for VRS points.

Usage:
  python calibrate.py [--config ../assets/majors_config.json] [--repo PATH] [--out dataset.json]
  python calibrate.py --dataset ../assets/major_matches.json      # skip building, just fit

After a new Major: add it to majors_config.json (participants as VRS names, stage dates,
snapshots), run this script, then put the fitted constants into scripts/model.py
(RANK_SCALE, VRS_SCALE, BO1_SHRINK) and update references/calibration.md.
"""
import argparse
import glob
import json
import math
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vrs_ranks import ensure_repo, norm  # noqa: E402

ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d{4}-\d\d-\d\d)\s*\|\s*([^|]+?)\s*\|\s*([WL])\s*\|")


def standings(repo, date):
    f = glob.glob(os.path.join(repo, "invitation", "*", f"standings_global_{date.replace('-', '_')}.md"))
    if not f:
        sys.exit(f"No invitation snapshot for {date}")
    out = {}
    for line in open(f[0], encoding="utf-8"):
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", line)
        if m and m.group(3) not in out:
            out[m.group(3)] = (int(m.group(1)), int(m.group(2)))
    return out


def build(cfg, repo):
    data = []
    for key, mj in cfg["majors"].items():
        pre = standings(repo, mj.get("strength_snapshot", mj["seeding_snapshot"]))
        det = glob.glob(os.path.join(repo, "invitation", "*", "details", mj["results_snapshot"].replace("-", "_"), "*.md"))
        if not det:
            sys.exit(f"No detail files for results_snapshot {mj['results_snapshot']}")
        parts = {norm(p) for p in mj["participants"]}
        first = min(v[0] for v in mj["stages"].values())
        last = max(v[1] for v in mj["stages"].values())
        matches = {}
        for f in det:
            team = None
            for line in open(f, encoding="utf-8"):
                if line.startswith("Team Name:"):
                    team = line.split(":", 1)[1].replace("<br />", "").strip()
                m = ROW.match(line)
                if not (m and team):
                    continue
                mid, date, opp, wl = m.group(2), m.group(3), m.group(4), m.group(5)
                if not (first <= date <= last) or norm(team) not in parts or norm(opp) not in parts:
                    continue
                matches.setdefault(mid, {"id": int(mid), "date": date, "teams": sorted([team, opp]),
                                         "winner": team if wl == "W" else opp})
        ms = sorted(matches.values(), key=lambda x: (x["date"], -x["id"]))
        rec, po = {}, []
        for m in ms:
            st = next((s for s, (a, b) in mj["stages"].items() if a <= m["date"] <= b), None)
            if st is None:
                continue
            if st == "PO":
                po.append(m)
                continue
            a, b = m["teams"]
            ra = rec.setdefault((st, a), [0, 0])
            bo3 = st in mj["all_bo3_stages"] or ra[0] == 2 or ra[1] == 2
            rec.setdefault((st, b), [0, 0])
            data.append(dict(major=key, stage=st, a=a, b=b, winner=m["winner"], bo=3 if bo3 else 1,
                             rec=f"{ra[0]}-{ra[1]}"))
            w = m["winner"]
            lo = b if w == a else a
            rec[(st, w)][0] += 1
            rec[(st, lo)][1] += 1
        for i, m in enumerate(po):
            a, b = m["teams"]
            data.append(dict(major=key, stage="PO", a=a, b=b, winner=m["winner"],
                             bo=mj["final_bo"] if i == len(po) - 1 else 3, rec="PO"))
        for row in data:
            if row["major"] == key:
                for side in ("a", "b"):
                    r = pre.get(row[side])
                    row["rank" + side], row["pts" + side] = (r if r else (None, None))
        n = {s: sum(1 for r in data if r["major"] == key and r["stage"] == s) for s in ("S1", "S2", "S3", "PO")}
        print(f"{key}: {n}  (expected 33/33/33/7)", file=sys.stderr)
    return data


def series(p, bo):
    need = bo // 2 + 1
    return sum(math.comb(need - 1 + l, l) * p ** need * (1 - p) ** l for l in range(need))


def predict(rows, mode, k, s):
    q = []
    for m in rows:
        if mode == "rank":
            diff = k * (math.log(m["rankb"]) - math.log(m["ranka"]))
        else:
            diff = k * (m["ptsa"] - m["ptsb"])
        p = 1 / (1 + 10 ** (-diff / 400))
        q.append(0.5 + (p - 0.5) * s if m["bo"] == 1 else series(p, m["bo"]))
    return np.clip(np.array(q), 1e-6, 1 - 1e-6)


def nll(rows, mode, k, s):
    q = predict(rows, mode, k, s)
    y = np.array([m["winner"] == m["a"] for m in rows], float)
    return -np.sum(y * np.log(q) + (1 - y) * np.log(1 - q))


GRID = {"rank": np.arange(20, 200.1, 2.0), "vrs": np.arange(0.05, 1.501, 0.01)}
SGRID = np.arange(0.3, 1.201, 0.02)


def fit(rows, mode):
    best = None
    for k in GRID[mode]:
        for s in SGRID:
            v = nll(rows, mode, k, s)
            if best is None or v < best[0]:
                best = (v, k, s)
    return best[1], best[2]


def report(rows):
    y = np.array([m["winner"] == m["a"] for m in rows], float)
    print(f"Matches: {len(rows)} (Bo1 {sum(m['bo'] == 1 for m in rows)}), coin-flip log-loss {math.log(2):.4f}")
    for mode in ("rank", "vrs"):
        k, s = fit(rows, mode)
        q = predict(rows, mode, k, s)
        ll = -np.mean(y * np.log(q) + (1 - y) * np.log(1 - q))
        unit = "A (rating = 1700 - A*ln rank)" if mode == "rank" else "k (rating = 1500 + k*(points-1500))"
        print(f"\n[{mode}] full fit: {unit} = {k:.2f}, bo1_shrink = {s:.2f}, log-loss {ll:.4f}")
        cv = np.zeros(len(rows))
        for mj in sorted({m["major"] for m in rows}):
            tr = [m for m in rows if m["major"] != mj]
            idx = [i for i, m in enumerate(rows) if m["major"] == mj]
            kk, ss = fit(tr, mode)
            cv[idx] = predict([rows[i] for i in idx], mode, kk, ss)
            print(f"   leave out {mj}: {kk:.2f}, {ss:.2f}")
        llcv = -np.mean(y * np.log(cv) + (1 - y) * np.log(1 - cv))
        print(f"   cross-validated log-loss {llcv:.4f}, accuracy {np.mean((cv > .5) == (y == 1)):.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "..", "assets", "majors_config.json"))
    ap.add_argument("--dataset", help="use an existing dataset JSON instead of building")
    ap.add_argument("--repo")
    ap.add_argument("--out", help="write the built dataset here")
    a = ap.parse_args()
    if a.dataset:
        rows = json.load(open(a.dataset, encoding="utf-8"))
    else:
        rows = build(json.load(open(a.config, encoding="utf-8")), ensure_repo(a.repo))
        if a.out:
            json.dump(rows, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    missing = [m for m in rows if m["ranka"] is None or m["rankb"] is None]
    if missing:
        print(f"WARNING: {len(missing)} matches lack VRS data and are skipped", file=sys.stderr)
    report([m for m in rows if m not in missing])


if __name__ == "__main__":
    main()
