# CS2 Pick'Em Skill for AI

[![Release](https://img.shields.io/github/v/release/RomkaGP228/pick-em-skill?include_prereleases&sort=semver)](https://github.com/RomkaGP228/pick-em-skill/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-Claude-d97757)](https://code.claude.com/docs/en/skills)
![Python](https://img.shields.io/badge/python-3.8%2B-3776ab)

**An Agent Skill that helps you fill in Counter-Strike 2 Pick'Em cards and free prediction games — with real research, a Monte Carlo simulator that follows Valve's actual Swiss rules, and honest odds instead of "guaranteed" picks.**

Tell Claude *"the Major starts next week, help me with my Pick'Em"* — it finds the event, works out the format and seeding, researches every team (form, roster changes, map pools, influencer picks, news), simulates the stage thousands of times and hands you a card labelled *safe / medium / risky*.

> **Not for betting.** The skill only works with free prediction games (Valve Pick'Em, free site predictors). It won't discuss odds, bookmakers, stakes or skin gambling.

---

## Install

**Claude apps (web, desktop, mobile)**
1. Download **[`cs2-pickem.zip`](https://github.com/RomkaGP228/pick-em-skill/releases/latest/download/cs2-pickem.zip)** from the latest release.
2. Turn on code execution: *Settings → Capabilities → Code execution and file creation*.
3. Open *Customize → Skills → + → Create skill → Upload a skill* and choose the ZIP.

**Claude Code**
```bash
git clone https://github.com/RomkaGP228/pick-em-skill ~/.claude/skills/cs2-pickem
```
For a single project, clone into `.claude/skills/cs2-pickem` inside that repository instead.

**Other Agent Skills runtimes** (e.g. Codex) — clone into the runtime's skills folder, e.g. `~/.codex/skills/cs2-pickem`.

Requirements: Python 3.8+ with `numpy`. `git` and access to GitHub are used to read Valve's official rankings (with a fallback to Liquipedia when GitHub is unreachable).

## Use

Just ask, in any language:

- *"Help me with my Pick'Em for Stage 1 of the Singapore Major."*
- *"Here's a screenshot of my Pick'Em — what would you change?"*
- *"IEM is starting on Friday, I want to do the predictor on Cybershoke."*
- *"Stage 2 is live, two rounds played — what are FaZe's chances now?"*

In Claude Code you can also call it directly with `/cs2-pickem`.

### What you get

A real card the v0.1 model built for **Budapest Major 2025, Stage 3** in the backtest (rankings only, no research, no peeking at results):

| Slot | Pick | Chance | Label | What happened |
|---|---|---|---|---|
| 3-0 | FURIA | 41% | safe | ✅ 3-0 |
| 3-0 | Vitality | 33% | medium | ❌ 3-1 |
| Advance | The MongolZ | 53% | medium | ✅ 3-1 |
| Advance | Falcons | 53% | medium | ✅ 3-2 |
| Advance | NAVI | 48% | medium | ✅ 3-2 |
| Advance | MOUZ | 46% | medium | ✅ 3-1 |
| Advance | Spirit | 44% | medium | ❌ 3-0 (too good for the slot) |
| Advance | G2 | 42% | medium | ❌ 2-3 |
| 0-3 | Imperial | 28% | medium | ❌ 1-3 |
| 0-3 | Passion UA | 27% | medium | ❌ 2-3 |

Predicted chance of 5+ correct: **39%**. Result: **5/10 — stage passed.** In a real run the skill adds research, a short "why" for every pick, a bolder alternative card and the risks that could break it.

---

## How it works

1. **Figure out the game and the event.** Valve Pick'Em slots (3-0 / advance / 0-3, playoff bracket) or a site predictor (match winners, who advances, champion, map score). Format of every stage and every match (Bo1/Bo3/Bo5), seeding.
2. **Research.** Form over the last 2–3 months, roster changes and stand-ins, map pools, how each team got into the stage, news from HLTV, X and public Telegram channels, published Pick'Em cards of pros and analysts.
3. **Model.** Team strength from the Valve Regional Standings (VRS) rank, plus a recent-form correction from the skill's own Elo (built from ~36,000 matches in Valve's data and topped up with results Valve hasn't published yet), plus explicit, reasoned adjustments from the research. Announced roster changes add uncertainty even before the new lineup has played. Non-Major events are treated as slightly less predictable. Everything is calibrated on real matches and checked on data the model never saw.
4. **Simulate.** Swiss stages with Valve's exact pairing rules, any bracket (GSL groups, single / double elimination), or single matches.
5. **Optimise for the real goal.** For Valve Pick'Em that's *5+ correct out of 10*, not "most likely team in every slot" — the optimiser accounts for correlations (two favourites meeting in round 2 can't both go 3-0).

## How good is it?

Honest numbers, all reproducible with `scripts/backtest.py` (each Major predicted by a model fitted on the other two):

| | Correct Swiss picks | Swiss stages passed (5+) | Playoff bracket picks |
|---|---|---|---|
| Model (VRS rank + Elo form correction) | 37 / 90 | **5 / 9** | **11 / 21** |
| "Pick by seed" baseline | 40 / 90 | 3 / 9 | 6 / 21 |

How well it predicts single matches (log-loss, lower is better; a coin flip is 0.693), on data that was **not** used to fit the model:

| | VRS rank only (v0.1) | + Elo form correction (v0.2) |
|---|---|---|
| LAN matches between top-20 teams, 2026 | 0.663 | **0.644** |
| All 318 Major matches (leave-one-Major-out) | 0.649 | **0.647** |
| Major Stage 3 matches | 0.635 | **0.618** |

- **Majors are close to a lottery.** The higher-ranked team wins 58% of Bo1s and 65% of Bo3s. Stage 1 is the hardest (≈25% chance of 5+ even for the best card), Stage 3 the most predictable (40–60%).
- The automatic model doesn't pick more Swiss winners than seeding over 9 stages — its edge is building the card around the 5+ target and the playoffs. **The rest has to come from research**, which can't be backtested without hindsight. That's why the skill keeps a prediction log: it records the automatic and the researched probability before every match and reports which one was more accurate.
- Every idea is tested before it ships. Some didn't make it: using Elo *instead* of VRS made Major predictions worse (Elo can't compare regions), and "punishing" teams after roster changes hurt accuracy. The full list is in [`references/calibration.md`](references/calibration.md).

What *is* verified exactly: with Valve's seeding rule, the pairing engine reproduces **all 45 rounds of all 9 Swiss stages** of Austin 2025, Budapest 2025 and Cologne 2026 (297 matches). Details: [`references/calibration.md`](references/calibration.md).

## Responsible use

- Free prediction games only. If a site requires a deposit or a skin stake to play, that's betting — the skill won't help with it.
- No "100%" picks. Every pick comes with a probability, and every answer ends with a reminder that this is entertainment.
- Bookmaker odds are never used as input.

---

## Under the hood

The instructions (`SKILL.md`, `references/`) are in Russian; Claude answers in the user's language. Scripts and their help texts are in English.

| Script | What it does |
|---|---|
| `scripts/elo.py` | VRS rank, the skill's own Elo, the form correction `elo_adj`, fresh matches (`--extra`) and roster checks at any date; `--fill` writes them into a stage file |
| `scripts/vrs_ranks.py` | VRS rank and points for any team at any date, from Valve's official repository |
| `scripts/model.py` | Shared calibrated strength model (the only place constants live) |
| `scripts/swiss_sim.py` | 16-team Swiss: simulation, optimal Pick'Em card, live mode, final standings and next-stage seeds |
| `scripts/playoffs_sim.py` | Exact Major playoff odds and the best consistent bracket |
| `scripts/bracket_sim.py` | Any bracket: GSL groups, single / double elimination, custom graphs |
| `scripts/predict.py` | Per-match probabilities and the prediction log (`probs`, `result`, `score`) |
| `scripts/calibrate.py` | Rebuilds the Major match dataset and refits the model |
| `scripts/backtest.py` | Leave-one-Major-out backtest of cards and brackets |

```bash
python3 scripts/elo.py --date 2026-11-25 Vitality "Natus Vincere" FURIA
python3 scripts/elo.py --date 2026-11-25 --fill stage.json --extra fresh.json
python3 scripts/swiss_sim.py assets/example_stage3_cologne2026.json --sims 20000
python3 scripts/bracket_sim.py --template gsl4 --teams A B C D > group.json
python3 scripts/predict.py probs matches.json --log predictions.json
python3 scripts/predict.py score predictions.json
```

### Repository layout
```
SKILL.md                     # the skill: workflow, rules, answer format
references/
  rules_and_format.md        # Valve Pick'Em rules, Swiss pairing and seeding
  tournament_formats.md      # any other event: formats, tools, predictor types
  research_playbook.md       # sources, team checklist, rating adjustments
  major_history.md           # every Swiss stage of recent Majors + patterns
  calibration.md             # model fit and backtest
scripts/                     # see the table above
assets/
  majors_config.json         # Majors used for calibration
  major_matches.json         # 318 Major matches with VRS ranks
  example_stage3_cologne2026.json
```

## Contributing

Contributions are very welcome — especially after each Major.

- **New Major results.** Add the event to `assets/majors_config.json`, run `python3 scripts/calibrate.py --out assets/major_matches.json` and `python3 scripts/backtest.py`, add the stage tables to `references/major_history.md`, and put the backtest numbers in your PR.
- **Prediction logs.** If you used the skill on a real event, share your `predictions.json` (or the `score` table). This is the only honest test of the research layer.
- **Format notes.** Organiser-specific Swiss rules, seeding quirks, new predictor sites (free ones only) → `references/tournament_formats.md`.
- **Sources.** Reliable X accounts, Telegram channels and sites → `references/research_playbook.md`.
- **Code.** Keep the scripts dependency-light (standard library + numpy). Any change to the model or the pairing engine must keep `backtest.py` reporting `pairings reproduced=yes` for every stage; mention the before/after numbers in the PR.

Please open an issue first for larger changes.

## Versioning

The project follows [Semantic Versioning](https://semver.org). Each release ships a ready-to-upload `cs2-pickem.zip`; changes are listed in [`CHANGELOG.md`](CHANGELOG.md).

## Data & attribution

- Rankings and match results: [Valve Regional Standings](https://github.com/ValveSoftware/counter-strike_regional_standings).
- Stage tables cross-checked with [Liquipedia](https://liquipedia.net/counterstrike/) (CC BY-SA 3.0).
- Swiss pairing rules: Valve's Major Supplemental Rulebook.

Not affiliated with Valve, Anthropic, tournament organisers or any prediction site. Counter-Strike is a trademark of Valve Corporation.

## License

[MIT](LICENSE)
