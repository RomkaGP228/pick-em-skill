"""
Shared strength model for all cs2-pickem scripts.

Calibrated on the 318 matches of the Austin 2025, Budapest 2025 and Cologne 2026 Majors and
~1,600 LAN matches between VRS top-40 teams in 2025-2026 (see references/calibration.md):
  * base rating   = 1700 - RANK_SCALE * ln(global VRS rank)
  * + elo_adj     = recent-form correction for VRS top-20 teams (top-50 at half weight on
                    non-Major events), from scripts/elo.py:
                    ELO_DEV_COEF * (Elo - (alpha + ELO_BETA*ln rank)), capped at ±ELO_DEV_CAP,
                    where alpha is re-centred on the current VRS top-20 at every date (Elo drifts
                    upward over time; without re-centring top-20 teams would get a free bonus)
  * + adj         = manual research adjustment (stand-in, injury, map-pool matchup, ...)
  * non-Major events: rating differences x NON_MAJOR_SCALE (they are a bit less predictable)
  * map win probability = 1 / (1 + 10^(-(Ra - Rb)/400)); Bo1 pulled toward 50/50 by BO1_SHRINK;
    Bo3/Bo5 computed from the map probability.
  * optional per-team "sd" (rating uncertainty, e.g. right after a roster change): the
    simulators redraw the team's rating every run; predict.py integrates over it.
After recalibrating change the constants HERE only.
"""
import math

RANK_SCALE = 90.0
VRS_SCALE = 0.37
BO1_SHRINK = 0.8
NON_MAJOR_SCALE = 0.9

# Elo deviation correction (fitted on 2025 non-Major LAN matches between VRS top-20 teams)
ELO_BETA = -100.0
ELO_DEV_COEF = 0.412
ELO_DEV_CAP = 100.0
ELO_TOP_N = 20
# non-Major events only: half the correction for VRS ranks 21-50 (helped on 2025-26 LAN matches,
# hurt at Majors, where ranks 21-50 are mostly regional Stage 1 teams with inflated Elo)
ELO_EXT_TOP_N = 50
ELO_EXT_FACTOR = 0.5

# Gauss-Hermite nodes/weights (probabilists', normalised) for integrating over rating noise
_GH = [(-2.856970013872806, 0.011257411327720691), (-1.355626179974266, 0.2220759220056126),
       (0.0, 0.5333333333333334), (1.355626179974266, 0.2220759220056126),
       (2.856970013872806, 0.011257411327720691)]


def rating_from_rank(rank):
    return 1700.0 - RANK_SCALE * math.log(max(1, int(rank)))


def rating_from_vrs(points):
    return 1500.0 + VRS_SCALE * (float(points) - 1500.0)


def elo_alpha(top_teams):
    """Centre of the Elo-vs-rank line from [(rank, elo), ...] of the current VRS top-20."""
    pts = [(r, e) for r, e in top_teams if r is not None and e is not None and int(r) <= ELO_TOP_N]
    if not pts:
        return None
    return sum(e - ELO_BETA * math.log(max(1, int(r))) for r, e in pts) / len(pts)


def elo_adjustment(rank, elo, alpha, event_type="major"):
    """Recent-form correction: VRS top-20 always; ranks 21-50 at half weight on non-Major events."""
    if rank is None or elo is None or alpha is None:
        return 0.0
    rank = int(rank)
    if rank <= ELO_TOP_N:
        factor = 1.0
    elif event_type == "other" and rank <= ELO_EXT_TOP_N:
        factor = ELO_EXT_FACTOR
    else:
        return 0.0
    dev = float(elo) - (alpha + ELO_BETA * math.log(max(1, rank)))
    return max(-ELO_DEV_CAP, min(ELO_DEV_CAP, factor * ELO_DEV_COEF * dev))


def base_rating(team):
    """Automatic model rating: rank / vrs_points / rating + elo_adj (no manual 'adj')."""
    if "rating" in team:
        r = float(team["rating"])
    elif "rank" in team and team["rank"] is not None:
        r = rating_from_rank(team["rank"])
    elif "vrs_points" in team:
        r = rating_from_vrs(team["vrs_points"])
    else:
        raise ValueError(f"Team {team.get('name')} needs 'rank', 'vrs_points' or 'rating'")
    return r + float(team.get("elo_adj", 0) or 0)


def team_rating(team):
    return base_rating(team) + float(team.get("adj", 0) or 0)


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


def event_scale(event_type):
    if event_type in (None, "", "major"):
        return 1.0
    if event_type == "other":
        return NON_MAJOR_SCALE
    raise ValueError("event_type must be 'major' or 'other'")


class Ratings:
    """Holds ratings + overrides; gives map and series probabilities.
    use_adj=False gives the automatic model (rank + elo_adj), without research adjustments."""

    def __init__(self, teams, overrides=None, bo1_shrink=BO1_SHRINK, use_adj=True,
                 event_type="major", rating_sd=0.0):
        k = event_scale(event_type)
        raw = {t["name"]: (team_rating(t) if use_adj else base_rating(t)) for t in teams}
        self.r = {n: 1500.0 + k * (v - 1500.0) for n, v in raw.items()}
        self.base = dict(self.r)
        self.over = parse_overrides(overrides, set(self.r)) if use_adj else {}
        self.bo1_shrink = float(bo1_shrink)
        g = float(rating_sd or 0)
        self.sd = {t["name"]: math.sqrt(g ** 2 + (float(t.get("sd", 0) or 0) if use_adj else 0) ** 2)
                   for t in teams}

    @property
    def noisy(self):
        return any(v > 0 for v in self.sd.values())

    def redraw(self, rng):
        """New random ratings for one simulation run (no-op without uncertainty)."""
        for t, s in self.sd.items():
            self.r[t] = self.base[t] + (rng.gauss(0, s) if s > 0 else 0.0)

    def p_map(self, a, b):
        if (a, b) in self.over:
            return self.over[(a, b)]
        return map_prob(self.r[a], self.r[b])

    def p_series(self, a, b, bo):
        return series_prob(self.p_map(a, b), bo, self.bo1_shrink)

    def p_series_expected(self, a, b, bo):
        """Series probability averaged over rating uncertainty (equals p_series when sd = 0)."""
        if (a, b) in self.over or not (self.sd[a] or self.sd[b]):
            return self.p_series(a, b, bo)
        s = math.sqrt(self.sd[a] ** 2 + self.sd[b] ** 2)
        d = self.base[a] - self.base[b]
        return sum(w * series_prob(1 / (1 + 10 ** (-(d + s * x) / 400)), bo, self.bo1_shrink) for x, w in _GH)
