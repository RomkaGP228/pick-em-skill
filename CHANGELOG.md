# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org).

Sections: **Added**, **Changed**, **Fixed**, **Removed**, and **Data** (new Majors, recalibration, history updates).

## [Unreleased]

## [0.2.0] - 2026-10-27

Model update after a live test on PGL Bucharest 2026. Every change was tested on data the model never saw; ideas that didn't help were dropped (see `references/calibration.md`).

### Added
- `scripts/elo.py`: the skill's own series Elo built from ~36,000 matches in Valve's VRS data (LAN and online, bigger events weighted more), the recent-form correction `elo_adj` for VRS top-20 teams, roster-change flags, and `--fill` to write `rank` + `elo_adj` into a stage file.
- `"event_type": "other"` for non-Major events: rating differences × 0.9 (non-Major favourites win a bit less often than Major-calibrated odds suggest).
- Optional rating uncertainty: `"sd"` per team (e.g. right after a roster change) and `"rating_sd"` for robustness checks; simulators redraw ratings every run, `predict.py` averages over them.
- `backtest.py --no-elo` to compare with the v0.1 model.

### Changed
- The automatic model is now VRS rank + `elo_adj`. Single-match log-loss: 2026 top-20 LAN matches 0.663 → 0.644, Major matches 0.649 → 0.647 (Stage 3: 0.635 → 0.618).
- Recent form is no longer a manual adjustment (`elo_adj` covers it); manual `adj` is for things not in match data: stand-ins, illness, visas, results after the last VRS update.
- After a roster change the skill adds uncertainty (`"sd": 40`) instead of a penalty — the data showed lower predictability, not lower strength.
- `predict.py` logs "automatic model vs skill" instead of "rank vs skill".

### Not adopted (tested, made predictions worse or couldn't be tested)
- Elo instead of VRS, or a VRS/Elo blend (Elo can't compare regions); a separate form residual; shrinking ratings after roster changes; rating noise by default.
- Map-level veto model and expert-consensus input: no data to calibrate them yet.

## [0.1.0] - 2026-10-27

First public beta.

### Added
- Valve Pick'Em for CS2 Majors: Swiss card optimiser that maximises the chance of 5+ correct picks, playoff bracket picks, safe / medium / risky labels.
- Swiss simulator with Valve's exact pairing rules (1v9 round 1, Difficulty Score, priority table for 6-team pools), live mode and final standings with next-stage seeds.
- Any other CS2 event: `bracket_sim.py` (GSL groups, single / double elimination, custom brackets), Swiss with organiser-style pairing, per-match probabilities for site predictors.
- Prediction log (`predict.py`) comparing the rank-only model with the researched forecast.
- `vrs_ranks.py`: Valve Regional Standings rank and points at any date.
- Research playbook, tournament format guide, history of the last three Majors.
- Free-games-only policy: no odds, bookmakers, stakes or skin gambling.

### Data
- 318 matches of Austin 2025, Budapest 2025 and Cologne 2026 with VRS ranks.
- Strength model calibrated on them (leave-one-Major-out): `rating = 1700 − 90·ln(VRS rank)`, Bo1 shrink 0.8.
- Seeding rule verified: the pairing engine reproduces all 45 rounds of all 9 Swiss stages.

[Unreleased]: https://github.com/RomkaGP228/pick-em-skill/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/RomkaGP228/pick-em-skill/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RomkaGP228/pick-em-skill/releases/tag/v0.1.0
