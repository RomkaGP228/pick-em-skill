# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org).

Sections: **Added**, **Changed**, **Fixed**, **Removed**, and **Data** (new Majors, recalibration, history updates).

## [Unreleased]

## [0.4.0] - 2026-10-28

Tooling release after the PGL Bucharest 2026 test; it also ships the unreleased 0.2.0 and 0.3.0 model updates below. Compared with 0.3.0 the model is unchanged: every proposed change was backtested and none beat the current settings on held-out data (see `references/calibration.md`).

### Added
- `scripts/liquipedia_event.py`: a Liquipedia tournament page (via api.php, 1–3 requests, 30 s throttle, cached) → `event.json` (participants with rosters, all matches with map scores), `matches.json` (for `elo.py --extra` and the prediction log), `swiss_N.json` (seeds, rosters, played rounds for live mode; the pairing rule is verified against the played rounds; 1v9 / 1v16 detected), `playoffs.json`. Re-running after a match day keeps ranks, `elo_adj`, manual adjustments and locked picks. Works offline with `--wikitext`.
- `predict.py results`: import many results at once (matched by team pair and date, idempotent, `--dry-run`).
- `swiss_sim.py --playoffs [FINAL_BO]`: champion / finalist odds from the Swiss simulation (8-team bracket seeded by the Swiss standings).
- `bracket_sim.py --template pgl8`: Valve Major / PGL Masters playoffs (QF 1v8, 4v5, 2v7, 3v6; final Bo5).
- `elo.py`: "no matches for N days" warning (>30 days) and days since the last match in the table.
- `requirements.txt`; offline test suite `tests/run_tests.py` (runs in the release workflow).
- Playbook: Liquipedia access notes (web pages often 403, api.php ~1 request / 30 s).

### Changed
- Clear error messages: `swiss_sim.py` without numpy; a team without a VRS rank.
- `liquipedia_event.py` skips qualifier subpages by default (`--with-qualifiers` to include them).

### Fixed
- `liquipedia_event.py` on real pages (found on PGL Bucharest 2026 before release):
  - results are read when map scores exist only as halves, with overtimes up to `o4`, as forfeits (`score=FF` / `W`, `walkover=`), or without `finished=` once the maps are done (previously "0 finished");
  - short team tags in matches (`vit`, `tl`, `mongolz`, `navi`, `dendele`…) are matched to participants, so these teams get rosters; unmatched tags are listed, `--team tag="Full Name"` fixes them by hand;
  - `playoffs.json` for `Bracket/8`: read from Liquipedia, or seeded from the Swiss standings (1v8, 4v5, 2v7, 3v6) while the bracket is still TBD; `swiss_N.json` now has `final_standings`;
  - Swiss seeds: Liquipedia lists round 1 in schedule order, not seed order (PGL Masters Bucharest 2026: FUT listed 1st, seeded 7th), so simulated rounds 2–5 and the final-standings tiebreaks used wrong seeds. Seeds now keep the round-1 pairs and sides from Liquipedia and are ordered by VRS rank on the newest snapshot before the event that reproduces the played rounds (both PGL Bucharest 2026 events: rounds 1–5 reproduced); before round 2 the newest snapshot is used and marked unverified. `--no-vrs` / `--vrs-repo PATH`. On PGL Masters Bucharest 2026 this changed the blind Swiss card (3-0: Spirit + Vitality instead of Spirit + FUT);
  - a missing page gives a clear message instead of `StopIteration`;
  - the example page in `SKILL.md` / README is `PGL/2026/Masters`.

### Tested, not adopted
- Non-Major scale: re-fitted values (0.87–1.02) were not better than 0.9 on held-out 2026 data; no overconfidence found on 746 held-out LAN matches.
- Separate scale for S-tier events with top-16 teams: no held-out improvement.
- Extra uncertainty after long breaks (21–60 days): no effect on accuracy.

## 0.3.0 - 2026-10-27 (not released separately; included in 0.4.0)

Second round of fixes from the PGL Bucharest 2026 test, released together with the 0.2.0 model update below. Each point was backtested; see `references/calibration.md`.

### Added
- `elo.py --extra fresh.json`: matches played after the last VRS update (collected from Liquipedia) go through the same Elo. VRS data lags by up to a month in 2026, and a month-old Elo loses ~60% of the correction's value. The report shows fresh matches per team and warns when data is more than a week old.
- Roster check: `"roster"` per team in the stage file (or `--roster`) is compared with the last roster in the data; new players → warning, `"sd": 40` and a note. Catches signings that haven't played yet.
- In-tournament updates: append results to `fresh.json` and refill (optional; neutral on Majors).
- Non-Major events: `elo_adj` also for VRS ranks 21-50 at half weight (LAN 2026: 0.647 → 0.639; not used at Majors, where it hurt).
- `assets/example_fresh_matches.json`.

### Fixed
- Team-name matching ignores organisation words (Team, Esports, Gaming, Clan): "FUT Esports" → FUT, "Team Spirit" → Spirit. Unknown teams in `--extra` produce a warning instead of silently becoming new 1400-rated teams.

### Not adopted
- Extra weight on recent matches (higher K, rating decay): worse at Majors or everywhere.
- Map-score weighting and automatic map pools: no map data to calibrate them yet.

## 0.2.0 - 2026-10-27 (not released separately; included in 0.4.0)

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

[Unreleased]: https://github.com/RomkaGP228/pick-em-skill/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/RomkaGP228/pick-em-skill/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/RomkaGP228/pick-em-skill/releases/tag/v0.1.0
