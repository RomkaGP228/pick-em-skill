#!/usr/bin/env python3
"""
CS2 Major Swiss stage Monte Carlo simulator + Pick'Em optimizer.

Implements the pairing rules from Valve's Major Supplemental Rulebook:
  * Round 1: seed 1v9, 2v10, ... 8v16
  * Mid-stage seed = (W-L record, Difficulty Score / Buchholz, initial seed)
  * Rounds 2-3: highest seed vs lowest seed available without a rematch
  * Rounds 4-5: 6-team pools use Valve's 15-row priority table (top-most row with no rematch)
  * Advancement / elimination matches are Bo3, everything else Bo1
    (format "all_bo3" = every match Bo3, as in IEM Cologne 2026 Stage 3)

Non-Major Swiss events: "pairing": "high_low" (every round highest vs lowest available by
record, Buchholz, seed — no priority table) and "round1": "1v16" when the organizer pairs that way.

Strength model: scripts/model.py (calibrated on 318 Major matches, see references/calibration.md).

Usage:
  python swiss_sim.py input.json [--sims 20000] [--json out.json] [--seed 42]

Input JSON:
{
  "stage": "Stage 1",
  "format": "standard",                 # or "all_bo3" / "all_bo1"
  "pairing": "valve",                   # or "high_low" for non-Major Swiss events
  "round1": "1v9",                      # or "1v16"
  "bo1_shrink": 0.8,                    # how much Bo1 pulls map odds toward 50/50
  "advance_counts_3_0": false,          # Pick'Em rule: does a 3-0 team in an advance slot count?
  "teams": [                            # in INITIAL SEED order (seed 1 first)
     {"name": "Vitality", "rank": 1},              # global VRS rank (recommended base)
     {"name": "NAVI", "rank": 5, "adj": -20},     # adj = manual tweak with a stated reason
     {"name": "TeamX", "vrs_points": 1650},       # alternative base
     {"name": "TeamY", "rating": 1580},           # explicit rating
     ...
  ],
  "map_prob_overrides": {"NAVI|Spirit": 0.45},   # optional: P(first team wins a map)
  "rounds": [                           # optional LIVE mode: real pairings / results
     [["Vitality","Legacy","Vitality"], ["MOUZ","VP",null], ...],   # winner null = not played yet
     ...
  ],
  "locked_picks": {"3-0": ["Vitality"], "0-3": [], "advance": []},  # optional
  "target_correct": 5
}
"""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("numpy is required: pip install numpy", file=sys.stderr)
    raise

PRIORITY_TABLE_6 = [
    [(1, 6), (2, 5), (3, 4)],
    [(1, 6), (2, 4), (3, 5)],
    [(1, 5), (2, 6), (3, 4)],
    [(1, 5), (2, 4), (3, 6)],
    [(1, 4), (2, 6), (3, 5)],
    [(1, 4), (2, 5), (3, 6)],
    [(1, 6), (2, 3), (4, 5)],
    [(1, 5), (2, 3), (4, 6)],
    [(1, 3), (2, 6), (4, 5)],
    [(1, 3), (2, 5), (4, 6)],
    [(1, 4), (2, 3), (5, 6)],
    [(1, 3), (2, 4), (5, 6)],
    [(1, 2), (3, 6), (4, 5)],
    [(1, 2), (3, 5), (4, 6)],
    [(1, 2), (3, 4), (5, 6)],
]

# Final record categories
CATS = ["3-0", "3-1", "3-2", "2-3", "1-3", "0-3"]
CAT_INDEX = {c: i for i, c in enumerate(CATS)}


from model import BO1_SHRINK, Ratings  # noqa: E402  (shared, calibrated strength model)


class Stage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.teams = [t["name"] for t in cfg["teams"]]
        if len(self.teams) != 16:
            raise ValueError(f"Swiss stage needs exactly 16 teams, got {len(self.teams)}")
        if len(set(self.teams)) != 16:
            raise ValueError("Duplicate team names in input")
        self.seed = {t: i + 1 for i, t in enumerate(self.teams)}
        self.fmt = cfg.get("format", "standard")
        if self.fmt not in ("standard", "all_bo3", "all_bo1"):
            raise ValueError("format must be standard | all_bo3 | all_bo1")
        self.pairing = cfg.get("pairing", "valve")
        if self.pairing not in ("valve", "high_low"):
            raise ValueError("pairing must be valve | high_low")
        self.round1 = cfg.get("round1", "1v9")
        if self.round1 not in ("1v9", "1v16"):
            raise ValueError("round1 must be 1v9 | 1v16")
        self.bo1_shrink = float(cfg.get("bo1_shrink", BO1_SHRINK))
        self.model = Ratings(cfg["teams"], cfg.get("map_prob_overrides"), self.bo1_shrink)
        self.rating = self.model.r
        self.live_rounds = cfg.get("rounds") or []
        for rnd in self.live_rounds:
            for m in rnd:
                self._check(m[0])
                self._check(m[1])
                if len(m) > 2 and m[2] is not None:
                    self._check(m[2])

    def _check(self, name):
        if name not in self.seed:
            raise ValueError(f"Unknown team name '{name}' (must match the 'teams' list exactly)")

    # ---------- probabilities ----------
    def p_map(self, a, b):
        return self.model.p_map(a, b)

    def p_match(self, a, b, bo3):
        return self.model.p_series(a, b, 3 if bo3 else 1)

    # ---------- pairing ----------
    @staticmethod
    def buchholz(team, W, L, opps):
        return sum(W[o] - L[o] for o in opps[team])

    def sort_pool(self, pool, W, L, opps):
        return sorted(pool, key=lambda t: (-self.buchholz(t, W, L, opps), self.seed[t]))

    @staticmethod
    def pair_high_low(lst, opps):
        """Highest vs lowest available without rematch, with backtracking."""
        if not lst:
            return []
        a = lst[0]
        for b in reversed(lst[1:]):
            if b in opps[a]:
                continue
            rest = [x for x in lst[1:] if x != b]
            sub = Stage.pair_high_low(rest, opps)
            if sub is not None:
                return [(a, b)] + sub
        return None

    @staticmethod
    def pair_table6(lst, opps):
        for row in PRIORITY_TABLE_6:
            pairs = [(lst[i - 1], lst[j - 1]) for i, j in row]
            if all(b not in opps[a] for a, b in pairs):
                return pairs
        return None

    def make_pairings(self, rnd, W, L, opps, alive):
        if rnd == 1:
            if self.round1 == "1v16":
                return [(self.teams[i], self.teams[15 - i]) for i in range(8)]
            return [(self.teams[i], self.teams[i + 8]) for i in range(8)]
        pools = defaultdict(list)
        for t in alive:
            pools[(W[t], L[t])].append(t)
        pairs = []
        for rec in sorted(pools, key=lambda r: (-r[0], r[1])):
            lst = self.sort_pool(pools[rec], W, L, opps)
            if len(lst) % 2:
                raise RuntimeError(f"Odd pool size {len(lst)} at record {rec} in round {rnd}")
            got = None
            if self.pairing == "valve" and rnd >= 4 and len(lst) == 6:
                got = self.pair_table6(lst, opps)
            if got is None:
                got = self.pair_high_low(lst, opps)
            if got is None:  # rematch unavoidable
                got = [(lst[i], lst[len(lst) - 1 - i]) for i in range(len(lst) // 2)]
            pairs.extend(got)
        return pairs

    def is_bo3(self, a, W, L):
        if self.fmt == "all_bo3":
            return True
        if self.fmt == "all_bo1":
            return False
        return W[a] == 2 or L[a] == 2

    # ---------- one simulation ----------
    def simulate_once(self, rng, details=False):
        W = {t: 0 for t in self.teams}
        L = {t: 0 for t in self.teams}
        opps = {t: [] for t in self.teams}
        for rnd in range(1, 6):
            alive = [t for t in self.teams if W[t] < 3 and L[t] < 3]
            if not alive:
                break
            live = self.live_rounds[rnd - 1] if rnd - 1 < len(self.live_rounds) else None
            if live:
                pairs = [(m[0], m[1]) for m in live]
                winners = {frozenset((m[0], m[1])): (m[2] if len(m) > 2 else None) for m in live}
            else:
                pairs = self.make_pairings(rnd, W, L, opps, alive)
                winners = {}
            for a, b in pairs:
                w = winners.get(frozenset((a, b)))
                if w is None:
                    pa = self.p_match(a, b, self.is_bo3(a, W, L))
                    w = a if rng.random() < pa else b
                lo = b if w == a else a
                W[w] += 1
                L[lo] += 1
                opps[a].append(b)
                opps[b].append(a)
        out = {}
        for t in self.teams:
            out[t] = f"{W[t]}-{L[t]}"
        if details:
            return out, W, L, opps
        return out

    def final_standings(self, rng=None):
        """Order teams as Valve does after a completed stage: record, Difficulty Score, initial seed.
        The advancing teams in this order are seeds 9-16 of the next stage."""
        out, W, L, opps = self.simulate_once(rng or random.Random(0), details=True)
        return sorted(self.teams, key=lambda t: (-(W[t] - L[t]), -self.buchholz(t, W, L, opps), self.seed[t])), out


def run_sims(stage, n, seed=None):
    rng = random.Random(seed)
    cats = np.zeros((n, 16), dtype=np.int8)
    for s in range(n):
        res = stage.simulate_once(rng)
        for j, t in enumerate(stage.teams):
            cats[s, j] = CAT_INDEX[res[t]]
    return cats


# ---------- Pick'Em optimizer ----------
ADV_COUNTS_3_0 = False


def adv_hit(col):
    hit = (col == 1) | (col == 2)
    if ADV_COUNTS_3_0:
        hit = hit | (col == 0)
    return hit


def score_matrix(cats, s30, s03, sadv):
    c = np.zeros(cats.shape[0], dtype=np.int16)
    for j in s30:
        c += cats[:, j] == 0
    for j in s03:
        c += cats[:, j] == 5
    for j in sadv:
        c += adv_hit(cats[:, j])
    return c


def optimize(cats, teams, target=5, locked=None, iters=6):
    locked = locked or {}
    idx = {t: i for i, t in enumerate(teams)}
    L30 = [idx[t] for t in locked.get("3-0", [])]
    L03 = [idx[t] for t in locked.get("0-3", [])]
    LADV = [idx[t] for t in locked.get("advance", [])]
    lockset = set(L30 + L03 + LADV)

    p30 = (cats == 0).mean(0)
    p03 = (cats == 5).mean(0)
    padv = adv_hit(cats).mean(0)

    used = set(lockset)
    s30 = list(L30)
    for j in np.argsort(-p30):
        if len(s30) >= 2:
            break
        if j not in used:
            s30.append(int(j)); used.add(int(j))
    s03 = list(L03)
    for j in np.argsort(-p03):
        if len(s03) >= 2:
            break
        if j not in used:
            s03.append(int(j)); used.add(int(j))
    sadv = list(LADV)
    for j in np.argsort(-padv):
        if len(sadv) >= 6:
            break
        if j not in used:
            sadv.append(int(j)); used.add(int(j))

    def obj(a, b, c):
        sc = score_matrix(cats, a, b, c)
        return (sc >= target).mean(), sc.mean()

    best = obj(s30, s03, sadv)
    for _ in range(iters):
        improved = False
        slots = [("30", s30), ("03", s03), ("adv", sadv)]
        for name, lst in slots:
            for pos in range(len(lst)):
                cur = lst[pos]
                if cur in lockset:
                    continue
                cur_used = set(s30) | set(s03) | set(sadv)
                # replace with an unused team
                for j in range(16):
                    if j in cur_used:
                        continue
                    lst[pos] = j
                    val = obj(s30, s03, sadv)
                    if val > best:
                        best, cur, improved = val, j, True
                        cur_used = set(s30) | set(s03) | set(sadv)
                    else:
                        lst[pos] = cur
                # swap with a team in another slot
                for oname, olst in slots:
                    if oname == name:
                        continue
                    for opos in range(len(olst)):
                        other = olst[opos]
                        if other in lockset:
                            continue
                        lst[pos], olst[opos] = other, cur
                        val = obj(s30, s03, sadv)
                        if val > best:
                            best, cur, improved = val, other, True
                        else:
                            lst[pos], olst[opos] = cur, other
        if not improved:
            break
    dist = np.bincount(score_matrix(cats, s30, s03, sadv), minlength=11)[:11] / cats.shape[0]
    return {
        "3-0": [teams[j] for j in s30],
        "0-3": [teams[j] for j in s03],
        "advance": [teams[j] for j in sadv],
        "p_at_least_target": float(best[0]),
        "expected_correct": float(best[1]),
        "distribution": [float(x) for x in dist],
    }


def tier(slot, p):
    if slot in ("3-0", "0-3"):
        return "safe" if p >= 0.35 else ("medium" if p >= 0.20 else "risky")
    return "safe" if p >= 0.55 else ("medium" if p >= 0.40 else "risky")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--sims", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json", default=None, help="write full results JSON here")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        cfg = json.load(f)
    global ADV_COUNTS_3_0
    ADV_COUNTS_3_0 = bool(cfg.get("advance_counts_3_0", False))
    stage = Stage(cfg)
    live = cfg.get("rounds") or []
    if len(live) == 5 and all(len(m) > 2 and m[2] for r in live for m in r):
        order, recs = stage.final_standings()
        adv = [t for t in order if recs[t].startswith("3")]
        print("## Stage complete — final standings (Valve order)\n")
        for i, t in enumerate(order, 1):
            print(f"{i}. {t} {recs[t]}")
        print("\nNext stage seeds 9-16 (advancing teams in order): " + ", ".join(adv))
        return
    n = args.sims or int(cfg.get("n_sims", 20000))
    cats = run_sims(stage, n, args.seed)
    teams = stage.teams

    probs = {}
    for j, t in enumerate(teams):
        col = cats[:, j]
        probs[t] = {c: float((col == i).mean()) for i, c in enumerate(CATS)}
        probs[t]["advance_any"] = probs[t]["3-0"] + probs[t]["3-1"] + probs[t]["3-2"]
        probs[t]["advance_slot"] = probs[t]["3-1"] + probs[t]["3-2"] + (probs[t]["3-0"] if ADV_COUNTS_3_0 else 0.0)

    target = int(cfg.get("target_correct", 5))
    best = optimize(cats, teams, target=target, locked=cfg.get("locked_picks"))

    # ---------- print report ----------
    print(f"## {cfg.get('stage', 'Swiss stage')} — {n} simulations, format={stage.fmt}\n")
    print("| Seed | Team | Rating | 3-0 | Advance (3-1/3-2) | Any advance | 0-3 |")
    print("|---|---|---|---|---|---|---|")
    for t in sorted(teams, key=lambda x: -probs[x]["advance_any"]):
        p = probs[t]
        print(f"| {stage.seed[t]} | {t} | {stage.rating[t]:.0f} | {p['3-0']:.0%} | "
              f"{p['advance_slot']:.0%} | {p['advance_any']:.0%} | {p['0-3']:.0%} |")
    print()
    print(f"### Optimal card (maximises P(>= {target} correct))\n")
    for slot, key in (("3-0", "3-0"), ("0-3", "0-3"), ("advance", "advance_slot")):
        for t in best[slot]:
            pv = probs[t][key]
            print(f"- {slot}: **{t}** — {pv:.0%} ({tier(slot, pv)})")
    print()
    print(f"P(>= {target} correct): **{best['p_at_least_target']:.1%}**, "
          f"expected correct: {best['expected_correct']:.2f}")
    dist = best["distribution"]
    print("Distribution of correct picks: " +
          ", ".join(f"{k}:{v:.0%}" for k, v in enumerate(dist) if v >= 0.005))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"stage": cfg.get("stage"), "n_sims": n, "format": stage.fmt,
                       "ratings": stage.rating, "probs": probs, "best_card": best,
                       "tiers": {slot: {t: tier(slot, probs[t]["3-0" if slot == "3-0" else
                                                            "0-3" if slot == "0-3" else "advance_slot"])
                                        for t in best[slot]} for slot in ("3-0", "0-3", "advance")}},
                      f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
