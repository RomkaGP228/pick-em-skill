"""
Shared strength model for all cs2-pickem scripts.

Calibrated on all 318 matches of the Austin 2025, Budapest 2025 and Cologne 2026 Majors,
leave-one-Major-out cross-validated (see references/calibration.md):
  * rating = 1700 - RANK_SCALE * ln(global VRS rank)   (recommended base)
  * rating = 1500 + VRS_SCALE * (VRS points - 1500)     (raw points are over-confident)
  * map win probability = 1 / (1 + 10^(-(Ra - Rb)/400))
  * Bo1: probability pulled toward 50/50 by BO1_SHRINK; Bo3/Bo5 computed from the map probability.
After recalibrating (scripts/calibrate.py) change the constants HERE only.
"""
import math

RANK_SCALE = 90.0
VRS_SCALE = 0.37
BO1_SHRINK = 0.8


def rating_from_rank(rank):
    return 1700.0 - RANK_SCALE * math.log(max(1, int(rank)))


def rating_from_vrs(points):
    return 1500.0 + VRS_SCALE * (float(points) - 1500.0)


def base_rating(team):
    """Rating from rank / vrs_points / explicit rating, WITHOUT the manual 'adj'."""
    if "rating" in team:
        return float(team["rating"])
    if "rank" in team:
        return rating_from_rank(team["rank"])
    if "vrs_points" in team:
        return rating_from_vrs(team["vrs_points"])
    raise ValueError(f"Team {team.get('name')} needs 'rank', 'vrs_points' or 'rating'")


def team_rating(team):
    return base_rating(team) + float(team.get("adj", 0))


def map_prob(ra, rb):
    return 1.0 / (1.0 + 10 ** (-(ra - rb) / 400.0))


def series_prob(p, bo, bo1_shrink=BO1_SHRINK):
    """P(win the series) given map win probability p."""
    bo = int(bo)
    if bo == 1:
        return 0.5 + (p - 0.5) * bo1_shrink
    need = bo // 2 + 1
    return sum(math.comb(need - 1 + l, l) * p ** need * (1 - p) ** l for l in range(need))


def parse_overrides(raw, known=None):
    """{"A|B": 0.45} -> {(A,B): 0.45, (B,A): 0.55}; validates names if `known` given."""
    out = {}
    for k, v in (raw or {}).items():
        a, b = [x.strip() for x in k.split("|")]
        for n in (a, b):
            if known is not None and n not in known:
                raise ValueError(f"Unknown team '{n}' in map_prob_overrides")
        out[(a, b)] = float(v)
        out[(b, a)] = 1.0 - float(v)
    return out


class Ratings:
    """Holds ratings + overrides; gives map and series probabilities."""

    def __init__(self, teams, overrides=None, bo1_shrink=BO1_SHRINK, use_adj=True):
        self.r = {t["name"]: (team_rating(t) if use_adj else base_rating(t)) for t in teams}
        self.over = parse_overrides(overrides, set(self.r)) if use_adj else {}
        self.bo1_shrink = float(bo1_shrink)

    def p_map(self, a, b):
        if (a, b) in self.over:
            return self.over[(a, b)]
        return map_prob(self.r[a], self.r[b])

    def p_series(self, a, b, bo):
        return series_prob(self.p_map(a, b), bo, self.bo1_shrink)
