# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org).

Sections: **Added**, **Changed**, **Fixed**, **Removed**, and **Data** (new Majors, recalibration, history updates).

## [Unreleased]

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

[Unreleased]: https://github.com/RomkaGP228/pick-em-skill/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/RomkaGP228/pick-em-skill/releases/tag/v0.1.0
