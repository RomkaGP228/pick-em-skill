#!/usr/bin/env python3
"""Offline regression tests (no network): python tests/run_tests.py
Fixtures are synthetic Liquipedia-style pages built from the real pairings of IEM Cologne 2026 Stage 3."""
import json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
F = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, S)
fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def run(*args):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True)


with tempfile.TemporaryDirectory() as tmp:
    out = os.path.join(tmp, "ev")
    r = run(os.path.join(S, "liquipedia_event.py"), "--wikitext", os.path.join(F, "event_main.wiki"),
            os.path.join(F, "event_playoffs.wiki"), "--out", out)
    check("liquipedia_event runs", r.returncode == 0, r.stderr[-300:])
    ev = json.load(open(os.path.join(out, "event.json")))
    check("16 participants with 5 players", len(ev["participants"]) == 16 and
          all(len(v["players"]) == 5 for v in ev["participants"].values()))
    check("former player and staff excluded", "oldguy" not in json.dumps(ev["participants"]) and
          ev["participants"]["Legacy"]["coach"] == "coachy")
    fresh = json.load(open(os.path.join(out, "matches.json")))["matches"]
    check("27 finished matches, walkover excluded", len(fresh) == 27, str(len(fresh)))
    check("all finished matches dated", all(m["date"] for m in fresh))
    sw = json.load(open(os.path.join(out, "swiss_1.json")))
    check("seed order from round 1", [t["name"] for t in sw["teams"]][:3] == ["Team Vitality", "Natus Vincere", "Team Falcons"])
    check("all-Bo3 format and Valve-compatible pairing", sw["format"] == "all_bo3" and sw["round1"] == "1v9")
    check("live rounds: 4 rounds, 4 unplayed", len(sw["rounds"]) == 4 and sum(m[2] is None for r in sw["rounds"] for m in r) == 4)
    po = json.load(open(os.path.join(out, "playoffs.json")))
    check("playoffs: 2 results, final Bo5", po["results"] == {"QF1": "Team Vitality", "QF3": "Team Spirit"} and po["matches"][-1]["bo"] == 5)

    # re-run keeps ranks / manual fields
    sw["teams"][0]["rank"] = 3; sw["teams"][0]["adj"] = -25; sw["locked_picks"] = {"3-0": ["Team Vitality"]}
    json.dump(sw, open(os.path.join(out, "swiss_1.json"), "w"))
    run(os.path.join(S, "liquipedia_event.py"), "--wikitext", os.path.join(F, "event_main.wiki"),
        os.path.join(F, "event_playoffs.wiki"), "--out", out)
    sw2 = json.load(open(os.path.join(out, "swiss_1.json")))
    check("re-run keeps rank, adj and locked picks", sw2["teams"][0].get("adj") == -25 and sw2["teams"][0].get("rank") == 3
          and sw2.get("locked_picks") == {"3-0": ["Team Vitality"]} and len(sw2["rounds"]) == 4)
    sw = sw2

    out16 = os.path.join(tmp, "v16")
    run(os.path.join(S, "liquipedia_event.py"), "--wikitext", os.path.join(F, "swiss_1v16.wiki"), "--out", out16)
    s16 = json.load(open(os.path.join(out16, "swiss_1.json")))
    check("1v16 pairing detected and seeds restored", s16["round1"] == "1v16" and s16["teams"][15]["name"] == "legacy")

    # swiss_sim live + playoffs on the fixture (ranks filled by hand: no VRS repo offline)
    for i, t in enumerate(sw["teams"]):
        t["rank"] = i + 1
    p = os.path.join(tmp, "sw.json")
    json.dump(sw, open(p, "w"))
    r = run(os.path.join(S, "swiss_sim.py"), p, "--sims", "2000", "--seed", "1", "--playoffs")
    check("swiss_sim live + --playoffs", r.returncode == 0 and "Champion" in r.stdout, r.stderr[-300:])

    # pgl8 template (Monte Carlo) == exact playoff calculation
    teams = ["A", "B", "C", "D", "E", "F", "G", "H"]
    tpl = json.loads(run(os.path.join(S, "bracket_sim.py"), "--template", "pgl8", "--teams", *teams).stdout)
    for i, t in enumerate(tpl["teams"]):
        t["rank"] = [1, 3, 6, 9, 12, 18, 25, 40][i]
    tpl["event_type"] = "major"
    bp = os.path.join(tmp, "b.json"); json.dump(tpl, open(bp, "w"))
    jp = os.path.join(tmp, "b_out.json")
    run(os.path.join(S, "bracket_sim.py"), bp, "--sims", "40000", "--seed", "3", "--json", jp)
    mc = json.load(open(jp))["placements"]["Champion"]
    pp = os.path.join(tmp, "p.json")
    json.dump({"teams": tpl["teams"], "final_bo": 5}, open(pp, "w"))
    ex = run(os.path.join(S, "playoffs_sim.py"), pp).stdout
    a_exact = float([l for l in ex.splitlines() if l.startswith("| 1 | A |")][0].split("|")[6].strip().rstrip("%")) / 100
    check("pgl8 Monte Carlo matches exact bracket", abs(mc["A"] - a_exact) < 0.015, f"{mc['A']:.3f} vs {a_exact:.3f}")

    # predict.py batch results
    pin = os.path.join(tmp, "pin.json"); log = os.path.join(tmp, "log.json")
    json.dump({"event": "T", "event_type": "other", "teams": [{"name": "Vitality", "rank": 3}, {"name": "FUT", "rank": 8}],
               "matches": [{"id": "x", "a": "Vitality", "b": "FUT", "bo": 3, "date": "2026-10-24"}]}, open(pin, "w"))
    run(os.path.join(S, "predict.py"), "probs", pin, "--log", log)
    r = run(os.path.join(S, "predict.py"), "results", log, os.path.join(out, "matches.json"))
    check("predict.py results matches 'Team Vitality' to 'Vitality'", "Updated: 1;" in r.stdout, r.stdout + r.stderr)

print("\n" + ("ALL TESTS PASSED" if not fails else f"{len(fails)} FAILED: {', '.join(fails)}"))
sys.exit(1 if fails else 0)
