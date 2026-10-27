#!/usr/bin/env python3
"""
CS2 Major playoff bracket probabilities + best Pick'Em bracket.

Bracket (Valve rulebook): A: 1v8, 4v5   B: 2v7, 3v6 ; semis: A-winners, B-winners ; final.
Quarterfinals / semifinals Bo3, grand final Bo5 by default.

Usage:
  python playoffs_sim.py input.json

Input JSON:
{
  "teams": [ {"name": "Spirit", "rating": 1700}, ... ],   # 8 teams in PLAYOFF SEED order (1..8)
  "map_prob_overrides": {"Spirit|FURIA": 0.52},           # optional
  "final_bo": 5,                                           # optional, default 5
  "results": {"QF1": "Spirit"}                              # optional: already played matches
}
Match ids: QF1 = 1v8, QF2 = 4v5, QF3 = 2v7, QF4 = 3v6, SF1 = QF1/QF2 winners, SF2 = QF3/QF4 winners, F.
"""
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import Ratings, series_prob as _series  # noqa: E402


def series_prob(p, bo):
    return _series(p, bo)


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 2 else 1)
    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
    teams = [t["name"] for t in cfg["teams"]]
    if len(teams) != 8:
        raise SystemExit("Need exactly 8 playoff teams in seed order")
    model = Ratings(cfg["teams"], cfg.get("map_prob_overrides"))
    rating = model.r
    final_bo = int(cfg.get("final_bo", 5))
    results = cfg.get("results") or {}

    def pm(a, b, bo):
        return model.p_series(a, b, bo)

    s = {i + 1: teams[i] for i in range(8)}
    qf = {"QF1": (s[1], s[8]), "QF2": (s[4], s[5]), "QF3": (s[2], s[7]), "QF4": (s[3], s[6])}

    # exact distribution over winners of each slot
    def dist_match(da, db, bo, mid):
        out = {}
        if mid in results:
            w = results[mid]
            return {w: 1.0}
        for a, pa in da.items():
            for b, pb in db.items():
                pw = pm(a, b, bo)
                out[a] = out.get(a, 0) + pa * pb * pw
                out[b] = out.get(b, 0) + pa * pb * (1 - pw)
        return out

    D = {}
    for mid, (a, b) in qf.items():
        D[mid] = dist_match({a: 1.0}, {b: 1.0}, 3, mid)
    D["SF1"] = dist_match(D["QF1"], D["QF2"], 3, "SF1")
    D["SF2"] = dist_match(D["QF3"], D["QF4"], 3, "SF2")
    D["F"] = dist_match(D["SF1"], D["SF2"], final_bo, "F")

    print("## Playoff probabilities\n")
    print("| Seed | Team | Rating | Win QF | Win SF (reach final) | Champion |")
    print("|---|---|---|---|---|---|")
    for i in range(1, 9):
        t = s[i]
        qfw = sum(D[m].get(t, 0) for m in ("QF1", "QF2", "QF3", "QF4"))
        sfw = D["SF1"].get(t, 0) + D["SF2"].get(t, 0)
        print(f"| {i} | {t} | {rating[t]:.0f} | {qfw:.0%} | {sfw:.0%} | {D['F'].get(t, 0):.0%} |")

    # best consistent bracket by expected correct picks (enumerate all 128 brackets)
    best = None
    for q in itertools.product(*[qf[m] for m in ("QF1", "QF2", "QF3", "QF4")]):
        for s1 in (q[0], q[1]):
            for s2 in (q[2], q[3]):
                for fw in (s1, s2):
                    exp = (D["QF1"].get(q[0], 0) + D["QF2"].get(q[1], 0) + D["QF3"].get(q[2], 0)
                           + D["QF4"].get(q[3], 0) + D["SF1"].get(s1, 0) + D["SF2"].get(s2, 0)
                           + D["F"].get(fw, 0))
                    if best is None or exp > best[0]:
                        best = (exp, q, s1, s2, fw)
    exp, q, s1, s2, fw = best
    print("\n### Best bracket (max expected correct picks)\n")
    print(f"- Quarterfinals: {', '.join(q)}")
    print(f"- Semifinals: {s1}, {s2}")
    print(f"- Champion: **{fw}** ({D['F'].get(fw, 0):.0%})")
    print(f"- Expected correct picks: {exp:.2f} of 7")


if __name__ == "__main__":
    main()
