#!/usr/bin/env python3
"""
Leave-one-Major-out backtest of the Pick'Em optimizer on past Majors.

For each Major in assets/majors_config.json and each Swiss stage:
  * seeds are rebuilt exactly as Valve does (stage seeds 1-8: invited teams by VRS at the
    seeding snapshot; 9-16: teams from the previous stage in final-standings order), and the
    pairing engine is checked against the real pairings;
  * team strength = 1700 - A*ln(rank at strength snapshot) + elo_adj (Elo frozen at the stage
    start, see elo.py), with A and the Bo1 shrink fitted on the OTHER Majors only;
    --no-elo reproduces the v0.1 model (rank only) for comparison;
  * the optimizer builds a card, which is scored against what really happened, next to a
    "by seed" baseline (seeds 1-2 at 3-0, 3-8 advance, 15-16 at 0-3).
Then the playoff bracket is backtested the same way.

Usage:
  python backtest.py [--sims 10000] [--repo PATH] [--no-elo]
Needs assets/major_matches.json (built by calibrate.py --out).
"""
import argparse
import itertools
import json
import math
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import swiss_sim as S  # noqa: E402
from calibrate import fit, standings, series  # noqa: E402
from vrs_ranks import ensure_repo, norm  # noqa: E402
from elo import load_history, run_elo, ranks_before, table_alpha  # noqa: E402
from model import elo_adjustment  # noqa: E402


def rounds_of(rows, major, st):
    R = {}
    for m in rows:
        if m["major"] == major and m["stage"] == st:
            w, l = map(int, m["rec"].split("-"))
            R.setdefault(w + l + 1, []).append((m["a"], m["b"], m["winner"]))
    return R


def replay(seeds, R):
    st = S.Stage({"teams": [{"name": t, "rating": 1500} for t in seeds]})
    W = {t: 0 for t in seeds}
    L = {t: 0 for t in seeds}
    op = {t: [] for t in seeds}
    ok = True
    for r in range(1, 6):
        alive = [t for t in seeds if W[t] < 3 and L[t] < 3]
        calc = {frozenset(p) for p in st.make_pairings(r, W, L, op, alive)}
        ok &= calc == {frozenset(x[:2]) for x in R[r]}
        for a, b, w in R[r]:
            lo = b if w == a else a
            W[w] += 1
            L[lo] += 1
            op[a].append(b)
            op[b].append(a)
    bu = {t: sum(W[o] - L[o] for o in op[t]) for t in seeds}
    order = sorted(seeds, key=lambda t: (-(W[t] - L[t]), -bu[t], seeds.index(t)))
    return ok, order, {t: f"{W[t]}-{L[t]}" for t in seeds}


def score(card, actual):
    return (sum(actual[t] == "3-0" for t in card["3-0"]) + sum(actual[t] == "0-3" for t in card["0-3"])
            + sum(actual[t] in ("3-1", "3-2") for t in card["advance"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--repo")
    ap.add_argument("--config", default=os.path.join(HERE, "..", "assets", "majors_config.json"))
    ap.add_argument("--dataset", default=os.path.join(HERE, "..", "assets", "major_matches.json"))
    ap.add_argument("--no-elo", action="store_true", help="rank-only model (v0.1) for comparison")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))["majors"]
    rows = json.load(open(a.dataset, encoding="utf-8"))
    repo = ensure_repo(a.repo)
    hist = None if a.no_elo else load_history(repo)
    _elo_cache = {}

    def elo_adj_fn(date):
        """team-name -> elo_adj, with Elo frozen at `date` (rank from strength snapshot)."""
        if hist is None:
            return lambda team, rank: 0.0
        if date not in _elo_cache:
            R, *_ = run_elo(hist, date)
            _, table = ranks_before(repo, date)
            _elo_cache[date] = (R, table_alpha(table, R))
        R, alpha = _elo_cache[date]
        return lambda team, rank: elo_adjustment(rank, R.get(team), alpha)
    tot_m = tot_b = pass_m = pass_b = 0
    exp_pass = 0.0
    po_m = po_b = 0
    for major, mj in cfg.items():
        A, s = fit([m for m in rows if m["major"] != major], "rank")
        seed_rank = {norm(k): v[0] for k, v in standings(repo, mj["seeding_snapshot"]).items()}
        str_rank = {norm(k): v[0] for k, v in standings(repo, mj.get("strength_snapshot", mj["seeding_snapshot"])).items()}
        prev_adv = None
        print(f"\n== {mj['name']} (fit on other Majors: A={A:.0f}, bo1_shrink={s:.2f}; "
              f"{'rank only' if a.no_elo else 'rank + elo_adj'})")
        for st in ("S1", "S2", "S3"):
            R = rounds_of(rows, major, st)
            teams = sorted({t for x in R[1] for t in x[:2]})
            if prev_adv is None:
                seeds = sorted(teams, key=lambda t: seed_rank[norm(t)])
            else:
                seeds = sorted([t for t in teams if t not in prev_adv], key=lambda t: seed_rank[norm(t)]) + prev_adv
            ok, order, actual = replay(seeds, R)
            prev_adv = [t for t in order if actual[t].startswith("3")]
            ea = elo_adj_fn(mj["stages"][st][0])
            stage = S.Stage({"teams": [{"name": t, "rating": 1700 - A * math.log(str_rank[norm(t)])
                                        + ea(t, str_rank[norm(t)])} for t in seeds],
                             "format": "all_bo3" if st in mj["all_bo3_stages"] else "standard", "bo1_shrink": s})
            cats = S.run_sims(stage, a.sims, seed=7)
            card = S.optimize(cats, seeds)
            base = {"3-0": seeds[:2], "advance": seeds[2:8], "0-3": seeds[14:]}
            cm, cb = score(card, actual), score(base, actual)
            tot_m += cm; tot_b += cb; pass_m += cm >= 5; pass_b += cb >= 5
            exp_pass += card["p_at_least_target"]
            print(f"  {st}: pairings reproduced={'yes' if ok else 'NO'} | model {cm}/10 "
                  f"(predicted P(5+)={card['p_at_least_target']:.0%}) | by-seed {cb}/10")
        # playoffs
        po = prev_adv
        ea = elo_adj_fn(mj["stages"]["PO"][0])
        R = {t: 1700 - A * math.log(str_rank[norm(t)]) + ea(t, str_rank[norm(t)]) for t in po}
        pm = lambda x, y, bo: series(1 / (1 + 10 ** (-(R[x] - R[y]) / 400)), bo)
        sd = {i + 1: po[i] for i in range(8)}
        qf = [(sd[1], sd[8]), (sd[4], sd[5]), (sd[2], sd[7]), (sd[3], sd[6])]
        win = {frozenset((m["a"], m["b"])): m["winner"] for m in rows if m["major"] == major and m["stage"] == "PO"}
        qw = [win[frozenset(p)] for p in qf]
        sw = [win[frozenset(qw[:2])], win[frozenset(qw[2:])]]
        ch = win[frozenset(sw)]

        def dist(da, db, bo):
            o = {}
            for x, px in da.items():
                for y, py in db.items():
                    w = pm(x, y, bo)
                    o[x] = o.get(x, 0) + px * py * w
                    o[y] = o.get(y, 0) + px * py * (1 - w)
            return o
        Dq = [dist({x: 1}, {y: 1}, 3) for x, y in qf]
        Ds = [dist(Dq[0], Dq[1], 3), dist(Dq[2], Dq[3], 3)]
        Df = dist(Ds[0], Ds[1], mj["final_bo"])
        _, q, s1, s2, f = max((sum(Dq[i][q[i]] for i in range(4)) + Ds[0][s1] + Ds[1][s2] + Df[f], q, s1, s2, f)
                              for q in itertools.product(*qf) for s1 in q[:2] for s2 in q[2:] for f in (s1, s2))
        cm = sum(q[i] == qw[i] for i in range(4)) + (s1 == sw[0]) + (s2 == sw[1]) + (f == ch)
        hi = lambda x, y: x if po.index(x) < po.index(y) else y
        bq = [x for x, _ in qf]
        bs = [hi(*bq[:2]), hi(*bq[2:])]
        cb = sum(bq[i] == qw[i] for i in range(4)) + (bs[0] == sw[0]) + (bs[1] == sw[1]) + (hi(*bs) == ch)
        po_m += cm; po_b += cb
        print(f"  Playoffs: model {cm}/7 (champion pick {f}, real {ch}) | by-seed {cb}/7")
    n = 3 * len(cfg)
    print(f"\nSwiss stages: model {tot_m}/{10 * n} correct, passed {pass_m}/{n} stages "
          f"(model expected {exp_pass:.1f}); by-seed {tot_b}/{10 * n}, passed {pass_b}/{n}")
    print(f"Playoffs: model {po_m}/{7 * len(cfg)}, by-seed {po_b}/{7 * len(cfg)}")


if __name__ == "__main__":
    main()
