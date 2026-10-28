#!/usr/bin/env python3
"""
Match-by-match win probabilities for prediction games (Cybershoke predictor, bo3.gg / HLTV
pick'ems, Pick'Em playoff picks) + a prediction log that measures whether the research layer
actually beats the plain VRS-rank model.

Two numbers per match:
  base  = the automatic model: VRS rank + elo_adj (from scripts/elo.py), no research
  skill = the same model plus research: manual 'adj', 'sd', map_prob_overrides

Subcommands:
  python predict.py probs input.json [--log predictions.json]
  python predict.py result predictions.json <match_id> <winner> [--event "IEM X"]
  python predict.py results predictions.json matches.json [--event "IEM X"] [--dry-run]
        # many results at once: matches.json from liquipedia_event.py (or the fresh-matches
        # format: team1, team2, winner or score, date); matched to open predictions by team pair
  python predict.py score predictions.json [--event "IEM X"]
  python predict.py pending predictions.json

Input JSON for `probs`:
{
  "event": "IEM Example 2026",
  "stage": "Group A",
  "event_type": "other",                     # "major" at Majors
  "teams": [{"name": "A", "rank": 4, "elo_adj": 12, "adj": -25, "sd": 40, "note": "stand-in for X"}, ...],
  "map_prob_overrides": {"A|B": 0.42},
  "matches": [{"id": "A-R1-1", "a": "A", "b": "B", "bo": 3, "date": "2026-11-05"}, ...]
}
Match ids must be unique within an event. Re-running `probs --log` for the same event/match id
updates the stored prediction instead of duplicating it (only while no result is recorded).
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import Ratings  # noqa: E402
from vrs_ranks import core  # noqa: E402


def confidence(p):
    q = max(p, 1 - p)
    if q >= 0.70:
        return "уверенно"
    if q >= 0.58:
        return "склоняюсь"
    return "почти монетка"


def load_log(path):
    if path and os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {"version": 1, "predictions": []}


def save_log(path, log):
    tmp = path + ".tmp"
    json.dump(log, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def cmd_probs(a):
    cfg = json.load(open(a.input, encoding="utf-8"))
    teams = cfg["teams"]
    names = {t["name"] for t in teams}
    shrink = float(cfg.get("bo1_shrink", 0.8))
    et = cfg.get("event_type", "other")
    skill = Ratings(teams, cfg.get("map_prob_overrides"), shrink, use_adj=True, event_type=et)
    base = Ratings(teams, None, shrink, use_adj=False, event_type=et)
    notes = {t["name"]: t.get("note", "") for t in teams if t.get("adj") or t.get("note")}
    rows = []
    ids = set()
    for m in cfg["matches"]:
        for side in (m["a"], m["b"]):
            if side not in names:
                raise SystemExit(f"Unknown team '{side}' in match {m.get('id')}")
        if m["id"] in ids:
            raise SystemExit(f"Duplicate match id {m['id']}")
        ids.add(m["id"])
        bo = int(m.get("bo", 3))
        pb = base.p_series_expected(m["a"], m["b"], bo)
        ps = skill.p_series_expected(m["a"], m["b"], bo)
        pick = m["a"] if ps >= 0.5 else m["b"]
        rows.append({"event": cfg.get("event", ""), "stage": cfg.get("stage", ""), "id": m["id"],
                     "date": m.get("date", ""), "a": m["a"], "b": m["b"], "bo": bo,
                     "p_base": round(pb, 4), "p_skill": round(ps, 4), "pick": pick,
                     "winner": None, "logged_at": dt.datetime.now().isoformat(timespec="seconds")})
    print(f"## {cfg.get('event', '')} {cfg.get('stage', '')}\n")
    print("| Матч | Bo | Пик | Шанс (скилл) | Шанс (авто-модель) | Уверенность |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        ps = r["p_skill"] if r["pick"] == r["a"] else 1 - r["p_skill"]
        pb = r["p_base"] if r["pick"] == r["a"] else 1 - r["p_base"]
        flag = " ⚠ расходится с авто-моделью" if (r["p_base"] >= 0.5) != (r["p_skill"] >= 0.5) else ""
        print(f"| {r['a']} vs {r['b']} | {r['bo']} | **{r['pick']}**{flag} | {ps:.0%} | {pb:.0%} | {confidence(ps)} |")
    if notes:
        print("\nПоправки: " + "; ".join(f"{t}: {n}" for t, n in notes.items() if n))
    if a.log:
        log = load_log(a.log)
        idx = {(p["event"], p["id"]): i for i, p in enumerate(log["predictions"])}
        added = updated = kept = 0
        for r in rows:
            k = (r["event"], r["id"])
            if k in idx:
                old = log["predictions"][idx[k]]
                if old.get("winner"):
                    kept += 1
                    continue
                log["predictions"][idx[k]] = r
                updated += 1
            else:
                log["predictions"].append(r)
                added += 1
        save_log(a.log, log)
        print(f"\nЖурнал {a.log}: добавлено {added}, обновлено {updated}, уже сыграны (не тронуты) {kept}.")


def cmd_result(a):
    log = load_log(a.log)
    hits = [p for p in log["predictions"] if p["id"] == a.match_id and (not a.event or p["event"] == a.event)]
    if not hits:
        raise SystemExit(f"No prediction with id '{a.match_id}'" + (f" in event '{a.event}'" if a.event else ""))
    if len(hits) > 1:
        raise SystemExit("Several events have this match id — add --event")
    p = hits[0]
    if a.winner not in (p["a"], p["b"]):
        raise SystemExit(f"Winner must be '{p['a']}' or '{p['b']}'")
    p["winner"] = a.winner
    save_log(a.log, log)
    print(f"{p['a']} vs {p['b']}: winner {a.winner} (pick was {p['pick']} — {'верно' if p['pick'] == a.winner else 'мимо'})")


def cmd_results(a):
    log = load_log(a.log)
    raw = json.load(open(a.matches, encoding="utf-8"))
    items = raw.get("matches", []) if isinstance(raw, dict) else raw
    preds = [p for p in log["predictions"] if not a.event or p["event"] == a.event]
    updated, same, conflicts, unmatched = 0, 0, [], []
    for it in items:
        t1, t2 = it.get("team1"), it.get("team2")
        win = it.get("winner")
        if not win and it.get("score"):
            try:
                s1, s2 = [int(x) for x in str(it["score"]).replace(":", "-").split("-")[:2]]
                win = t1 if s1 > s2 else t2 if s2 > s1 else None
            except ValueError:
                win = None
        if not (t1 and t2 and win):
            continue
        pair = {core(t1), core(t2)}
        cands = [p for p in preds if {core(p["a"]), core(p["b"])} == pair]
        if not cands:
            unmatched.append(f"{t1} vs {t2}")
            continue
        open_c = [p for p in cands if not p.get("winner")] or cands
        if it.get("date"):
            def gap(p):
                if not p.get("date"):
                    return 0
                return abs((dt.date.fromisoformat(p["date"]) - dt.date.fromisoformat(it["date"])).days)
            open_c.sort(key=gap)
            if open_c[0].get("date") and gap(open_c[0]) > 1:
                unmatched.append(f"{t1} vs {t2} ({it['date']}: no prediction within a day)")
                continue
        p = open_c[0]
        w = p["a"] if core(win) == core(p["a"]) else p["b"]
        if p.get("winner"):
            if p["winner"] == w:
                same += 1
            else:
                conflicts.append(f"{p['id']}: log says {p['winner']}, file says {w}")
            continue
        if not a.dry_run:
            p["winner"] = w
        updated += 1
    if not a.dry_run:
        save_log(a.log, log)
    print(f"{'Would update' if a.dry_run else 'Updated'}: {updated}; already recorded: {same}; "
          f"results without a prediction: {len(unmatched)}; conflicts: {len(conflicts)}")
    for c in conflicts:
        print("CONFLICT " + c)
    if unmatched:
        print("No prediction for: " + "; ".join(unmatched[:12]) + (" ..." if len(unmatched) > 12 else ""))
    pending = [p for p in preds if not p.get("winner")]
    print(f"Predictions still without a result: {len(pending)}")


def metrics(rows, key):
    y = [1.0 if r["winner"] == r["a"] else 0.0 for r in rows]
    q = [min(max(r[key], 1e-6), 1 - 1e-6) for r in rows]
    n = len(rows)
    acc = sum((qq >= 0.5) == (yy == 1) for qq, yy in zip(q, y)) / n
    ll = -sum(yy * math.log(qq) + (1 - yy) * math.log(1 - qq) for qq, yy in zip(q, y)) / n
    br = sum((qq - yy) ** 2 for qq, yy in zip(q, y)) / n
    return acc, ll, br


def cmd_score(a):
    log = load_log(a.log)
    rows = [p for p in log["predictions"] if p.get("winner") and (not a.event or p["event"] == a.event)]
    if not rows:
        raise SystemExit("No matches with results yet")
    groups = {"ВСЕГО": rows}
    if not a.event:
        for p in rows:
            groups.setdefault(p["event"], []).append(p)
    print("| Выборка | Матчей | Точность: авто → скилл | Log-loss: авто → скилл | Brier: авто → скилл |")
    print("|---|---|---|---|---|")
    for g, rs in groups.items():
        ab, lb, bb = metrics(rs, "p_base")
        as_, ls, bs = metrics(rs, "p_skill")
        print(f"| {g} | {len(rs)} | {ab:.0%} → {as_:.0%} | {lb:.3f} → {ls:.3f} | {bb:.3f} → {bs:.3f} |")
    diff = [p for p in rows if (p["p_base"] >= 0.5) != (p["p_skill"] >= 0.5)]
    if diff:
        right = sum(p["pick"] == p["winner"] for p in diff)
        print(f"\nМатчи, где скилл пошёл против авто-модели: {len(diff)}, из них скилл прав в {right}.")
    ab, lb, _ = metrics(rows, "p_base")
    as_, ls, _ = metrics(rows, "p_skill")
    if len(rows) < 30:
        print(f"\nМатчей пока {len(rows)} — для вывода нужно хотя бы 30, надёжно — 60–100.")
        return
    verdict = ("поправки помогают" if ls < lb - 0.005 else "поправки вредят" if ls > lb + 0.005
               else "разницы пока нет")
    print(f"\nИтог по log-loss (меньше — лучше): {verdict}.")
    if len(rows) < 60:
        print(f"Матчей {len(rows)} — вывод предварительный, надёжно с 60–100.")


def cmd_pending(a):
    log = load_log(a.log)
    rows = [p for p in log["predictions"] if not p.get("winner")]
    for p in rows:
        print(f"{p['event']} | {p['id']} | {p['date']} | {p['a']} vs {p['b']} | pick {p['pick']}")
    print(f"Без результата: {len(rows)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probs"); p.add_argument("input"); p.add_argument("--log")
    p = sub.add_parser("result"); p.add_argument("log"); p.add_argument("match_id"); p.add_argument("winner")
    p.add_argument("--event")
    p = sub.add_parser("results"); p.add_argument("log"); p.add_argument("matches"); p.add_argument("--event")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("score"); p.add_argument("log"); p.add_argument("--event")
    p = sub.add_parser("pending"); p.add_argument("log")
    a = ap.parse_args()
    {"probs": cmd_probs, "result": cmd_result, "results": cmd_results, "score": cmd_score,
     "pending": cmd_pending}[a.cmd](a)


if __name__ == "__main__":
    main()
