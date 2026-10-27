#!/usr/bin/env python3
"""
Generic CS2 bracket simulator: single elimination, double elimination, GSL groups, or any
custom bracket described as a graph of matches.

A bracket is a list of matches. Each side is a team name or a reference to another match:
  "W:<id>" = winner of match <id>,  "L:<id>" = loser of match <id>.
Matches must be listed so that every reference points to an EARLIER match.

Usage:
  python bracket_sim.py input.json [--sims 20000] [--seed 1] [--json out.json]
  python bracket_sim.py --template single_elim --teams A B C D E F G H [--bo 3 --final-bo 5]
  python bracket_sim.py --template gsl4 --teams A B C D [--bo 3]
  python bracket_sim.py --template double_elim8 --teams A B C D E F G H [--bo 3 --final-bo 3]
      (templates print a ready input JSON; add ranks/adj, then run it)

Input JSON:
{
  "event": "IEM Example 2026 — Playoffs",
  "event_type": "other",                        # "major" for Major playoffs
  "teams": [{"name": "A", "rank": 3, "elo_adj": 20, "adj": 10, "sd": 0, "note": "..."}, ...],
  "map_prob_overrides": {"A|B": 0.45},
  "matches": [
     {"id": "QF1", "a": "A", "b": "H", "bo": 3},
     {"id": "SF1", "a": "W:QF1", "b": "W:QF2", "bo": 3},
     ...
  ],
  "results": {"QF1": "A"},                      # optional: already played (live mode)
  "placements": {"Champion": ["W:GF"], "Runner-up": ["L:GF"], "Out in groups": ["L:D"]}  # optional
}
Single-elimination team order: pairs are (1st,2nd), (3rd,4th), ... in bracket order, top half first.
Output: for every match, each team's chance to win it; placement probabilities; the most likely
winner of each match for pick-by-pick predictors.
"""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import Ratings  # noqa: E402


# ---------------------------------------------------------------- templates
def tpl_single_elim(teams, bo=3, final_bo=None):
    n = len(teams)
    if n < 2 or n & (n - 1):
        raise SystemExit("single_elim needs 2, 4, 8, 16 or 32 teams")
    names = {2: ["GF"], 4: ["SF", "GF"], 8: ["QF", "SF", "GF"], 16: ["R16", "QF", "SF", "GF"],
             32: ["R32", "R16", "QF", "SF", "GF"]}[n]
    matches, prev = [], []
    for i in range(0, n, 2):
        mid = f"{names[0]}{i // 2 + 1}" if n > 2 else "GF"
        matches.append({"id": mid, "a": teams[i], "b": teams[i + 1], "bo": final_bo if n == 2 and final_bo else bo})
        prev.append(mid)
    for r, name in enumerate(names[1:], 1):
        cur = []
        for i in range(0, len(prev), 2):
            mid = name if name == "GF" else f"{name}{i // 2 + 1}"
            last = name == "GF"
            matches.append({"id": mid, "a": f"W:{prev[i]}", "b": f"W:{prev[i + 1]}",
                            "bo": final_bo if last and final_bo else bo})
            cur.append(mid)
        prev = cur
    return {"matches": matches,
            "placements": {"Champion": ["W:GF"], "Runner-up": ["L:GF"]}}


def tpl_gsl4(teams, bo=3):
    a, b, c, d = teams
    return {"matches": [
        {"id": "O1", "a": a, "b": d, "bo": bo},
        {"id": "O2", "a": b, "b": c, "bo": bo},
        {"id": "WM", "a": "W:O1", "b": "W:O2", "bo": bo},
        {"id": "EM", "a": "L:O1", "b": "L:O2", "bo": bo},
        {"id": "DM", "a": "L:WM", "b": "W:EM", "bo": bo}],
        "placements": {"1st (winners' match)": ["W:WM"], "2nd (decider)": ["W:DM"],
                       "Eliminated": ["L:EM", "L:DM"]}}


def tpl_double_elim8(teams, bo=3, final_bo=None):
    t = teams
    return {"matches": [
        {"id": "UQ1", "a": t[0], "b": t[1], "bo": bo}, {"id": "UQ2", "a": t[2], "b": t[3], "bo": bo},
        {"id": "UQ3", "a": t[4], "b": t[5], "bo": bo}, {"id": "UQ4", "a": t[6], "b": t[7], "bo": bo},
        {"id": "US1", "a": "W:UQ1", "b": "W:UQ2", "bo": bo}, {"id": "US2", "a": "W:UQ3", "b": "W:UQ4", "bo": bo},
        {"id": "LR1a", "a": "L:UQ1", "b": "L:UQ2", "bo": bo}, {"id": "LR1b", "a": "L:UQ3", "b": "L:UQ4", "bo": bo},
        {"id": "UF", "a": "W:US1", "b": "W:US2", "bo": bo},
        {"id": "LR2a", "a": "W:LR1a", "b": "L:US2", "bo": bo}, {"id": "LR2b", "a": "W:LR1b", "b": "L:US1", "bo": bo},
        {"id": "LR3", "a": "W:LR2a", "b": "W:LR2b", "bo": bo},
        {"id": "LF", "a": "W:LR3", "b": "L:UF", "bo": bo},
        {"id": "GF", "a": "W:UF", "b": "W:LF", "bo": final_bo or bo}],
        "placements": {"Champion": ["W:GF"], "Runner-up": ["L:GF"], "3rd": ["L:LF"],
                       "4th": ["L:LR3"], "5-6th": ["L:LR2a", "L:LR2b"], "7-8th": ["L:LR1a", "L:LR1b"]}}


TEMPLATES = {"single_elim": tpl_single_elim, "gsl4": tpl_gsl4, "double_elim8": tpl_double_elim8}


# ---------------------------------------------------------------- simulation
class Bracket:
    def __init__(self, cfg):
        self.teams = [t["name"] for t in cfg["teams"]]
        self.model = Ratings(cfg["teams"], cfg.get("map_prob_overrides"), float(cfg.get("bo1_shrink", 0.8)),
                             event_type=cfg.get("event_type", "other"), rating_sd=cfg.get("rating_sd", 0))
        self.matches = cfg["matches"]
        self.results = cfg.get("results") or {}
        self.placements = cfg.get("placements") or {}
        seen = set()
        for m in self.matches:
            for side in (m["a"], m["b"]):
                if side[:2] in ("W:", "L:"):
                    if side[2:] not in seen:
                        raise ValueError(f"Match {m['id']} references {side} before it is defined")
                elif side not in self.teams:
                    raise ValueError(f"Unknown team '{side}' in match {m['id']}")
            seen.add(m["id"])
        for mid, w in self.results.items():
            if mid not in seen:
                raise ValueError(f"Result for unknown match '{mid}'")
            if w not in self.teams:
                raise ValueError(f"Unknown winner '{w}' for match {mid}")

    def resolve(self, side, out):
        if side.startswith("W:"):
            return out[side[2:]][0]
        if side.startswith("L:"):
            return out[side[2:]][1]
        return side

    def once(self, rng):
        if self.model.noisy:
            self.model.redraw(rng)
        out = {}
        for m in self.matches:
            a, b = self.resolve(m["a"], out), self.resolve(m["b"], out)
            w = self.results.get(m["id"])
            if w is not None:
                if w not in (a, b):
                    raise ValueError(f"Result {m['id']}={w} but the match is {a} vs {b} — check the bracket")
            else:
                w = a if rng.random() < self.model.p_series(a, b, m.get("bo", 3)) else b
            out[m["id"]] = (w, b if w == a else a)
        return out


def run(cfg, n, seed=None):
    br = Bracket(cfg)
    rng = random.Random(seed)
    win = defaultdict(lambda: defaultdict(int))
    play = defaultdict(lambda: defaultdict(int))
    place = defaultdict(lambda: defaultdict(int))
    for _ in range(n):
        out = br.once(rng)
        for mid, (w, l) in out.items():
            win[mid][w] += 1
            play[mid][w] += 1
            play[mid][l] += 1
        for label, refs in br.placements.items():
            for ref in refs:
                place[label][br.resolve(ref, out)] += 1
    br.model.r.update(br.model.base)
    return br, win, play, place


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?")
    ap.add_argument("--template", choices=sorted(TEMPLATES))
    ap.add_argument("--teams", nargs="+")
    ap.add_argument("--bo", type=int, default=3)
    ap.add_argument("--final-bo", type=int)
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--json")
    a = ap.parse_args()

    if a.template:
        if not a.teams:
            sys.exit("--template needs --teams")
        kw = {"bo": a.bo}
        if a.template != "gsl4":
            kw["final_bo"] = a.final_bo
        elif len(a.teams) != 4:
            sys.exit("gsl4 needs exactly 4 teams (seed order 1,2,3,4: openers 1v4, 2v3)")
        if a.template == "double_elim8" and len(a.teams) != 8:
            sys.exit("double_elim8 needs exactly 8 teams (pairs 1-2, 3-4, 5-6, 7-8 in bracket order)")
        body = TEMPLATES[a.template](a.teams, **kw)
        cfg = {"event": "", "event_type": "other", "teams": [{"name": t, "rank": None} for t in a.teams],
               "map_prob_overrides": {}, **body, "results": {}}
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    if not a.input:
        ap.error("give an input JSON or --template")
    cfg = json.load(open(a.input, encoding="utf-8"))
    br, win, play, place = run(cfg, a.sims, a.seed)
    n = a.sims

    print(f"## {cfg.get('event', 'Bracket')} — {n} simulations\n")
    print("| Match | Bo | Most likely winner | Chance | Other contenders |")
    print("|---|---|---|---|---|")
    picks = {}
    for m in br.matches:
        mid = m["id"]
        if mid in br.results:
            print(f"| {mid} | {m.get('bo', 3)} | {br.results[mid]} (played) | — | |")
            picks[mid] = br.results[mid]
            continue
        ranked = sorted(win[mid].items(), key=lambda x: -x[1])
        top, c = ranked[0]
        picks[mid] = top
        others = ", ".join(f"{t} {v / n:.0%}" for t, v in ranked[1:4])
        print(f"| {mid} | {m.get('bo', 3)} | {top} | {c / n:.0%} | {others} |")
    if place:
        print("\n### Placements\n")
        print("| Team | " + " | ".join(place) + " |")
        print("|---|" + "---|" * len(place))
        for t in sorted(br.teams, key=lambda t: -sum(place[l].get(t, 0) for l in list(place)[:1])):
            print(f"| {t} | " + " | ".join(f"{place[l].get(t, 0) / n:.0%}" for l in place) + " |")
    print("\n### Chance to WIN each match (if it gets there or not)\n")
    print("| Team | " + " | ".join(m["id"] for m in br.matches) + " |")
    print("|---|" + "---|" * len(br.matches))
    for t in br.teams:
        print(f"| {t} | " + " | ".join(f"{win[m['id']].get(t, 0) / n:.0%}" for m in br.matches) + " |")
    if a.json:
        json.dump({"picks": picks,
                   "win": {mid: {t: v / n for t, v in d.items()} for mid, d in win.items()},
                   "plays": {mid: {t: v / n for t, v in d.items()} for mid, d in play.items()},
                   "placements": {l: {t: v / n for t, v in d.items()} for l, d in place.items()}},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
