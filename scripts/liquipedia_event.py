#!/usr/bin/env python3
"""
Build skill input files from a Liquipedia Counter-Strike tournament page.

Reads the page's wikitext (and its subpages: stages, playoffs) through the MediaWiki API
(api.php) and writes:
  event.json     - infobox data, participants with rosters, every match with scores
  matches.json   - finished matches in the elo.py --extra / predict.py results format
  swiss_<n>.json - Swiss stage file for swiss_sim.py: teams in seed order with rosters, format,
                   played rounds (live mode). Seeds: round-1 pairs from Liquipedia, order by VRS
                   rank on the newest snapshot before the event that reproduces the played rounds
                   (needs the VRS repo; --no-vrs keeps the Liquipedia order); pairing rule verified
  playoffs.json  - 8-team single-elimination bracket for bracket_sim.py (with results)
and prints a summary with everything that needs a human check.

Usage:
  python liquipedia_event.py "PGL/2026/Masters" --out event_dir
  python liquipedia_event.py --wikitext page.wiki [stage.wiki ...] --out event_dir   # offline
Options:
  --no-subpages     only the given page
  --throttle SEC    pause between API requests (default 30; Liquipedia rate-limits api.php)
  --cache DIR       keep raw wikitext here (default: <out>/wikitext); reruns reuse it
  --refresh         ignore the cache and download again (e.g. after a match day)
  --with-qualifiers also read qualifier / regional subpages (skipped by default)
  --team tag="Name" map a team-template tag (vit, tl, navi ...) to a participant by hand

Liquipedia rules: identify yourself (User-Agent below), send gzip, keep to their rate limits.
Content is CC BY-SA 3.0 — credit Liquipedia when you share results.
Requires only the Python standard library (swiss pairing check uses swiss_sim.py if numpy exists).
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vrs_ranks import core, norm  # noqa: E402

API = "https://liquipedia.net/counterstrike/api.php"
USER_AGENT = "cs2-pickem-skill/0.4.0 (https://github.com/RomkaGP228/pick-em-skill)"
QUALIFIER_RE = re.compile(r"(?i)qualif|/(open|closed)\b|/(asia|europe|americas|north america|south america|oceania|"
                          r"china|mena|cis|east asia|sea)(/|$)|/statistics|/map statistics|/player statistics")
TIER_LETTER = {"1": "S", "2": "A", "3": "B", "4": "C", "5": "C"}


# ------------------------------------------------------------------ fetching
class Fetcher:
    def __init__(self, throttle, cache_dir, refresh):
        self.throttle, self.cache, self.refresh = throttle, cache_dir, refresh
        self.last = 0.0
        os.makedirs(cache_dir, exist_ok=True)

    def _get(self, params):
        wait = self.throttle - (time.time() - self.last)
        if self.last and wait > 0:
            print(f"# waiting {wait:.0f}s (Liquipedia rate limit)", file=sys.stderr)
            time.sleep(wait)
        url = API + "?" + urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except urllib.error.HTTPError as e:
            hint = " (rate limited: raise --throttle and retry later)" if e.code == 429 else ""
            sys.exit(f"Liquipedia API error {e.code}{hint}: {url}")
        except urllib.error.URLError as e:
            sys.exit(f"Cannot reach Liquipedia ({e.reason}). Save the wikitext manually and use --wikitext.")
        finally:
            self.last = time.time()
        return json.loads(raw.decode("utf-8"))

    def _cache_path(self, title):
        return os.path.join(self.cache, re.sub(r"[^A-Za-z0-9._-]+", "_", title) + ".wiki")

    def expand_teams(self, tags):
        """One request: expand {{Team|tag}} for each unresolved tag and read the linked team page."""
        if not tags:
            return {}
        text = "\n".join(f"{t}:::{{{{Team|{t}}}}}" for t in tags)
        try:
            data = self._get({"action": "expandtemplates", "text": text, "prop": "wikitext"})
        except SystemExit as e:
            print(f"# team tag lookup failed ({e}); continuing without it", file=sys.stderr)
            return {}
        out = {}
        for line in data.get("expandtemplates", {}).get("wikitext", "").split("\n"):
            tag, _, rest = line.partition(":::")
            links = [l for l in re.findall(r"\[\[([^\]|]+)", rest) if not re.match(r"(?i)(file|image|category):", l)]
            if tag in tags and links:
                out[tag] = links[0].strip()
        return out

    def subpages(self, title):
        data = self._get({"action": "query", "list": "allpages", "apprefix": title + "/", "aplimit": "50"})
        return [p["title"] for p in data.get("query", {}).get("allpages", [])]

    def wikitext(self, titles):
        out, need = {}, []
        for t in titles:
            p = self._cache_path(t)
            if os.path.exists(p) and not self.refresh:
                out[t] = open(p, encoding="utf-8").read()
            else:
                need.append(t)
        for i in range(0, len(need), 50):
            chunk = need[i:i + 50]
            data = self._get({"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                              "redirects": "1", "titles": "|".join(chunk)})
            q = data.get("query", {})
            back = {r["to"]: r["from"] for r in q.get("redirects", [])}
            for page in q.get("pages", []):
                if page.get("missing"):
                    print(f"# WARNING: page not found: {page.get('title')}", file=sys.stderr)
                    continue
                text = page["revisions"][0]["slots"]["main"]["content"]
                title = back.get(page["title"], page["title"])
                out[title] = text
                open(self._cache_path(title), "w", encoding="utf-8").write(text)
        return out


# ------------------------------------------------------------------ wikitext parsing
def clean(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>/]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    return text


def templates(text):
    """Top-level templates in text: list of (start, end, raw_inner)."""
    out, i, n = [], 0, len(text)
    while i < n - 1:
        if text.startswith("{{", i) and not text.startswith("{{{", i):
            depth, j = 0, i
            while j < n - 1:
                if text.startswith("{{", j):
                    depth += 1
                    j += 2
                elif text.startswith("}}", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    j += 1
            out.append((i, j, text[i + 2:j - 2]))
            i = j
        else:
            i += 1
    return out


def split_args(inner):
    """Split template inner text at top-level pipes."""
    parts, depth_t, depth_l, cur, i = [], 0, 0, [], 0
    while i < len(inner):
        two = inner[i:i + 2]
        if two == "{{":
            depth_t += 1; cur.append(two); i += 2; continue
        if two == "}}":
            depth_t -= 1; cur.append(two); i += 2; continue
        if two == "[[":
            depth_l += 1; cur.append(two); i += 2; continue
        if two == "]]":
            depth_l -= 1; cur.append(two); i += 2; continue
        if inner[i] == "|" and depth_t == 0 and depth_l == 0:
            parts.append("".join(cur)); cur = []; i += 1; continue
        cur.append(inner[i]); i += 1
    parts.append("".join(cur))
    return parts


def parse_template(inner):
    parts = split_args(inner)
    name = parts[0].strip().replace("_", " ").lower()
    pos, named = [], {}
    for p in parts[1:]:
        m = re.match(r"\s*([^=\{\}\[\]]+?)\s*=(.*)$", p, flags=re.S)
        if m:
            named[m.group(1).strip().lower()] = m.group(2).strip()
        else:
            pos.append(p.strip())
    return name, pos, named


def first_template(value, names):
    for _, _, inner in templates(value or ""):
        nm, pos, named = parse_template(inner)
        if nm in names:
            return nm, pos, named
    return None


def plain(value):
    """Wikitext value -> plain text (links, simple templates removed)."""
    v = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", value or "")
    v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    return v.strip()


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"], 1)}


def parse_date(value):
    v = plain(value)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", v)
    if m and m.group(1).lower() in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    return None


def truthy(v):
    return str(v or "").strip().lower() in ("true", "1", "yes", "y", "finished")


def to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ extraction
def parse_infobox(text):
    for _, _, inner in templates(text):
        nm, pos, named = parse_template(inner)
        if nm.startswith("infobox league") or nm.startswith("infobox tournament"):
            return {"name": plain(named.get("name", "")), "tier": named.get("liquipediatier", "").strip(),
                    "type": plain(named.get("type", "")), "sdate": parse_date(named.get("sdate", "")),
                    "edate": parse_date(named.get("edate", "")), "organizer": plain(named.get("organizer", ""))}
    return {}


def parse_participants(text):
    teams = {}

    def add(team, players, subs=(), coach=None):
        team = plain(team)
        if team:
            teams[team] = {"players": [p for p in players if p], "subs": [s for s in subs if s], "coach": coach}

    def walk(t):
        for _, _, inner in templates(t):
            nm, pos, named = parse_template(inner)
            if nm == "teamcard":
                ps = [plain(named.get(f"p{i}", "")) for i in range(1, 6)]
                subs = [plain(named.get(f"p{i}", "")) for i in range(6, 9)]
                add(named.get("team", "") or (pos[0] if pos else ""), ps, subs, plain(named.get("c", "")) or None)
            elif nm in ("opponent", "teamopponent") and ("players" in named or any("{{" in p for p in pos)):
                team = pos[0] if pos else named.get("team", named.get("template", ""))
                players, staff = [], []
                for _, _, pin in templates(named.get("players", "") + " ".join(pos[1:])):
                    pn, ppos, pnamed = parse_template(pin)
                    if pn in ("persons", "players"):
                        for _, _, qin in templates(pin.split("|", 1)[1] if "|" in pin else ""):
                            qn, qpos, qnamed = parse_template(qin)
                            if qn in ("person", "player"):
                                nick = plain(qpos[0] if qpos else qnamed.get("name", qnamed.get("id", "")))
                                if qnamed.get("type", "").lower() == "staff" or qnamed.get("role", "").lower() in ("coach", "head coach", "analyst", "manager"):
                                    staff.append(nick)
                                elif qnamed.get("status", "").lower() not in ("former", "inactive"):
                                    players.append(nick)
                add(team, players[:5], players[5:], staff[0] if staff else None)
            else:
                walk(inner.split("|", 1)[1] if "|" in inner else "")
    walk(text)
    return teams


STATUS_WIN = {"W"}
STATUS_LOSS = {"FF", "DQ", "L"}


def score_or_status(v):
    """'2' -> (2, None); 'W' / 'FF' / 'DQ' / 'L' -> (None, code)."""
    raw = plain(v or "").strip()
    if not raw:
        return None, None
    n = to_int(raw)
    if n is not None:
        return n, None
    up = raw.upper()
    return (None, up) if up in STATUS_WIN | STATUS_LOSS | {"D"} else (None, None)


def team_of(value):
    """{{TeamOpponent|vit|score=2}} -> ('vit', 2, None); score=W/FF -> status."""
    tpl = first_template(value, ("teamopponent", "team opponent", "opponent", "teamopp"))
    if not tpl:
        return (plain(value) or None), None, None
    _, pos, named = tpl
    name = pos[0] if pos else named.get("template", named.get("team", named.get("1", "")))
    sc, st = score_or_status(named.get("score"))
    return (plain(name) or None), sc, st


def map_scores(mn):
    """Map score from score1/score2, or the sum of halves (t1ct + t1t) plus overtimes (o1t1ct ...)."""
    s1, s2 = to_int(mn.get("score1")), to_int(mn.get("score2"))
    if s1 is not None or s2 is not None:
        return s1, s2
    tot1 = tot2 = 0
    seen = False
    for pre in [""] + [f"o{i}" for i in range(1, 10)]:
        vals = [to_int(mn.get(f"{pre}t{t}{side}")) for t in (1, 2) for side in ("ct", "t")]
        if all(v is None for v in vals):
            continue  # a missing overtime block doesn't end the sum (pages sometimes skip empty ones)
        seen = True
        a1, a2, b1, b2 = [v or 0 for v in vals]
        tot1 += a1 + a2
        tot2 += b1 + b2
    return (tot1, tot2) if seen else (None, None)


def parse_match(inner, ctx):
    _, _, named = parse_template(inner)
    t1, sc1, st1 = team_of(named.get("opponent1", ""))
    t2, sc2, st2 = team_of(named.get("opponent2", ""))
    maps = []
    for i in range(1, 8):
        mv = named.get(f"map{i}")
        if mv is None:
            continue
        tpl = first_template(mv, ("map",))
        if not tpl:
            continue
        _, _, mn = tpl
        m1, m2 = map_scores(mn)
        mw = to_int(mn.get("winner"))
        done = truthy(mn.get("finished")) or mw in (1, 2) or (
            m1 is not None and m2 is not None and m1 != m2 and max(m1, m2) >= 13)
        if mw not in (1, 2) and done and m1 is not None and m2 is not None and m1 != m2:
            mw = 1 if m1 > m2 else 2
        maps.append({"map": plain(mn.get("map", "")), "score1": m1, "score2": m2,
                     "winner": mw if done else None, "finished": done})
    bo = to_int(named.get("bestof")) or (len(maps) if maps else None)
    w1 = sum(1 for m in maps if m["winner"] == 1)
    w2 = sum(1 for m in maps if m["winner"] == 2)
    if sc1 is None and sc2 is None and (w1 or w2):
        sc1, sc2 = w1, w2
    winner = to_int(named.get("winner"))
    walk = False
    # forfeits: score=W on one side, FF / DQ / L on the other; or walkover=1|2|FF|DQ|L
    if st1 in STATUS_WIN or st2 in STATUS_LOSS:
        winner, walk = 1, True
    elif st2 in STATUS_WIN or st1 in STATUS_LOSS:
        winner, walk = 2, True
    wo = plain(named.get("walkover") or named.get("wo") or "").upper()
    if wo:
        walk = True
        if to_int(wo) in (1, 2):
            winner = to_int(wo)
    need = (bo // 2 + 1) if bo else None
    finished = truthy(named.get("finished")) or winner in (1, 2) or (
        need is not None and sc1 is not None and sc2 is not None and max(sc1, sc2) >= need)
    if winner not in (1, 2) and finished and sc1 is not None and sc2 is not None and sc1 != sc2:
        winner = 1 if sc1 > sc2 else 2
    done = bool(finished and winner in (1, 2))
    return {**ctx, "date": parse_date(named.get("date", "")), "team1": t1, "team2": t2,
            "walkover": walk and not any(m["finished"] for m in maps),
            "score1": sc1, "score2": sc2, "bo": bo, "finished": done,
            "winner": (t1 if winner == 1 else t2) if done else None,
            "maps": [m for m in maps if m["map"] or m["score1"] is not None]}


def round_of(*texts):
    for t in texts:
        m = re.search(r"round\s*(\d+)", t or "", flags=re.I)
        if m:
            return int(m.group(1))
    return None


def parse_matches(text, page):
    heads = [(m.start(), len(m.group(1)), plain(m.group(2)))
             for m in re.finditer(r"^(=+)\s*(.*?)\s*\1\s*$", text, flags=re.M)]

    def sections_at(pos):
        stack = {}
        for p, lvl, title in heads:
            if p > pos:
                break
            stack = {k: v for k, v in stack.items() if k < lvl}
            stack[lvl] = title
        return [stack[k] for k in sorted(stack)]

    out = []
    for start, _, inner in templates(text):
        nm, pos, named = parse_template(inner)
        is_list = nm == "matchlist"
        is_bracket = nm == "bracket"
        if not (is_list or is_bracket):
            # wrappers (tabs, boxes) may contain match groups
            if "{{" in inner and nm not in ("match",):
                for m in parse_matches(inner.split("|", 1)[1] if "|" in inner else "", page):
                    m["section"] = " / ".join(sections_at(start) + ([m["section"]] if m["section"] else []))
                    out.append(m)
            continue
        secs = sections_at(start)
        title = plain(named.get("title", ""))
        gid = named.get("id", "").strip()
        bracket_type = pos[0] if (is_bracket and pos) else None
        inherited = None
        args = split_args(inner)[1:]
        auto = 0
        for a in args:
            m = re.match(r"\s*([A-Za-z0-9]+)\s*=(.*)$", a, flags=re.S)
            key, val = (m.group(1), m.group(2)) if m else (None, a)
            if key and key.lower().endswith("header"):
                inherited = plain(val)
                continue
            if "{{" not in (val or "") or not first_template(val, ("match",)):
                continue
            auto += 1
            key = key or f"M{auto}"
            hdr = named.get(f"{key.lower()}header")
            if hdr is not None:
                inherited = plain(hdr)
            ctx = {"page": page, "section": " / ".join(secs), "group": gid, "group_title": title,
                   "group_type": "bracket" if is_bracket else "matchlist", "bracket": bracket_type,
                   "key": key, "header": inherited or ""}
            inner_match = val[val.index("{{") + 2:val.rindex("}}")]
            rec = parse_match(inner_match, ctx)
            rec["round"] = round_of(inherited, title, secs[-1] if secs else "")
            out.append(rec)
    return out


# ------------------------------------------------------------------ assembling
# Common Liquipedia team-template tags -> name core (as produced by vrs_ranks.core)
TAG_ALIASES = {
    "vit": "vitality", "tl": "liquid", "liq": "liquid", "navi": "natusvincere", "mongolz": "themongolz",
    "tmz": "themongolz", "c9": "cloud9", "col": "complexity", "nip": "ninjasinpyjamas", "gl": "gamerlegion",
    "bb": "betboom", "pv": "parivision", "fq": "flyquest", "fal": "falcons", "fur": "furia",
    "vp": "virtuspro", "imp": "imperial", "lvg": "lynnvision", "wc": "wildcard", "ast": "astralis",
    "hero": "heroic", "eg": "evilgeniuses", "mibr": "mibr", "spirit": "spirit", "faze": "faze",
    "g2": "g2", "mouz": "mouz", "big": "big", "ence": "ence", "fnc": "fnatic", "og": "og",
    "puaa": "passionua", "pua": "passionua", "sinners": "sinners", "tyloo": "tyloo", "rare": "rareatom",
}


def initials(name):
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "".join(w[0] for w in words).lower()


def subseq(short, long):
    it = iter(long)
    return all(ch in it for ch in short)


def tag_score(tag, name):
    """How well a team-template tag ('vit', 'tl') matches a participant name ('Team Vitality')."""
    t, n, c = norm(tag), norm(name), core(name)
    if not t:
        return 0
    if t == n or t == c:
        return 100
    if TAG_ALIASES.get(t) in (n, c):
        return 95
    if len(t) >= 2 and t in (initials(name), initials(re.sub(r"(?i)\b(team|esports?|gaming|clan)\b", " ", name))):
        return 85
    if len(t) >= 3 and (c.startswith(t) or n.startswith(t)):
        return 80
    if len(t) >= 4 and (t in c or t in n):
        return 70
    if len(t) >= 3 and t[0] == c[:1] and subseq(t, c):
        return 50
    return 0


def resolve_tags(tags, participants, manual):
    """Map match tags to participant names; returns (mapping, unresolved)."""
    mapping, unresolved = {}, []
    used = set()
    pairs = []
    for tag in tags:
        if tag in manual:
            mapping[tag] = manual[tag]
            continue
        if tag in participants:
            mapping[tag] = tag
            used.add(tag)
            continue
        scored = sorted(((tag_score(tag, p), p) for p in participants), reverse=True)
        if scored and scored[0][0] >= 50 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            pairs.append((scored[0][0], tag, scored[0][1]))
        else:
            unresolved.append(tag)
    for sc, tag, p in sorted(pairs, reverse=True):
        if p in used:
            unresolved.append(tag)
            continue
        mapping[tag] = p
        used.add(p)
    return mapping, unresolved


def stage_key(m):
    s = (m["section"] + " " + m["group_title"]).lower()
    if m["group_type"] == "bracket" or "playoff" in s:
        return "playoffs"
    sec = m["section"].split(" / ")
    for part in sec:
        if re.search(r"swiss|stage|group|opening|elimination", part, flags=re.I):
            return part
    return sec[-1] if sec and sec[-1] else "main"


def verify_swiss(seeds, rounds):
    """Which pairing rule reproduces the played rounds 2+? Needs swiss_sim (numpy)."""
    import importlib.util
    if importlib.util.find_spec("numpy") is None:
        return None, "pairing check skipped (numpy not installed)"
    import swiss_sim as S
    played = [r for r in rounds if r]
    if len(played) < 2:
        return None, "only round 1 known — pairing rule cannot be verified yet"
    results = []
    for pairing in ("valve", "high_low"):
        for r1 in ("1v9", "1v16"):
            order = seeds if r1 == "1v9" else seeds[:8] + list(reversed(seeds[8:]))
            st = S.Stage({"teams": [{"name": t, "rating": 1500} for t in order], "pairing": pairing, "round1": r1})
            W = {t: 0 for t in order}; L = {t: 0 for t in order}; op = {t: [] for t in order}
            ok = True
            for i, rnd in enumerate(played, 1):
                if any(m[2] is None for m in rnd) and i < len(played):
                    ok = False
                    break
                alive = [t for t in order if W[t] < 3 and L[t] < 3]
                calc = {frozenset(p) for p in st.make_pairings(i, W, L, op, alive)}
                if calc != {frozenset(m[:2]) for m in rnd}:
                    ok = False
                    break
                for a, b, w in rnd:
                    if w is None:
                        continue
                    lo = b if w == a else a
                    W[w] += 1; L[lo] += 1; op[a].append(b); op[b].append(a)
            results.append((ok, pairing, r1))
    good = [r for r in results if r[0]]
    if not good:
        return None, "WARNING: no pairing rule reproduces the played rounds — check seeds manually"
    return good[0], "reproduces rounds 1-%d: %s" % (len(played), ", ".join(f"{p}/{r}" for _, p, r in good))


def vrs_seeds(r1, rounds, sdate, repo):
    """Seed order from VRS: Liquipedia lists round 1 in schedule order, not seed order.
    Keeps the round-1 pairs and sides (opponent1 = upper half) and sorts the pairs by the VRS rank
    of the upper-half team, trying snapshots before the event start, newest first, until one
    reproduces the played rounds (PGL Masters Bucharest 2026: 2026-10-05; PGL Bucharest 2026: 2026-03-02).
    Returns (seeds or None, verify result or None, note)."""
    import vrs_ranks as V
    try:
        repo = V.ensure_repo(repo)
    except (OSError, subprocess.CalledProcessError) as e:
        return None, None, f"VRS data unavailable ({e.__class__.__name__}) — seeds from round-1 order"
    snaps = {}
    for d, p in V.snapshots(repo, live=True):
        if not sdate or d < sdate:
            snaps.setdefault(d, p)
    import importlib.util
    numpy = importlib.util.find_spec("numpy") is not None
    checkable = len([r for r in rounds if r]) >= 2 and numpy
    why = "until round 2 is played" if numpy else "pairing check needs numpy"
    for d in sorted(snaps, reverse=True)[:12]:
        table = V.parse(snaps[d])
        rank = {a: (V.match(a, table) or [None])[0] for a, _b, _w in r1}
        if None in rank.values():
            continue
        pairs = sorted(r1, key=lambda m: rank[m[0]])
        seeds = [m[0] for m in pairs] + [m[1] for m in pairs]
        if not checkable:                         # nothing to verify against yet: newest snapshot
            return seeds, None, f"seeds by VRS snapshot {d} (unverified: {why})"
        best, msg = verify_swiss(seeds, rounds)
        if best:
            return seeds, (best, msg), f"seeds by VRS snapshot {d}"
    return None, None, "no VRS snapshot reproduces the rounds either — seeds from round-1 order"


def final_order(seeds, rounds):
    """Swiss final standings (record, Difficulty Score, initial seed) once every team is 3-x or x-3."""
    W = {t: 0 for t in seeds}; L = {t: 0 for t in seeds}; op = {t: [] for t in seeds}
    for rnd in rounds:
        for a, b, w in rnd:
            if w is None or a not in W or b not in W:
                return None
            lo = b if w == a else a
            W[w] += 1; L[lo] += 1; op[a].append(b); op[b].append(a)
    if any(W[t] < 3 and L[t] < 3 for t in seeds):
        return None
    bu = {t: sum(W[o] - L[o] for o in op[t]) for t in seeds}
    return sorted(seeds, key=lambda t: (-(W[t] - L[t]), -bu[t], seeds.index(t)))


KEEP_TEAM = ("rank", "vrs_points", "rating", "elo_adj", "adj", "sd", "note")
KEEP_TOP = ("map_prob_overrides", "locked_picks", "advance_counts_3_0", "rating_sd", "bo1_shrink", "target_correct")


def merge_previous(path, new):
    """Re-running after a match day must not wipe ranks, elo_adj, manual adjustments or locked picks:
    copy them from the existing file; only rounds, rosters and seeds come from Liquipedia."""
    if not os.path.exists(path):
        return ""
    try:
        old = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    by_name = {t["name"]: t for t in old.get("teams", [])}
    n = 0
    for t in new["teams"]:
        o = by_name.get(t["name"])
        if o:
            for k in KEEP_TEAM:
                if k in o:
                    t[k] = o[k]
                    n += 1
    for k in KEEP_TOP:
        if old.get(k):
            new[k] = old[k]
    auto_old = old.get("_auto", {})
    for k in ("pairing", "round1", "event_type", "format"):
        # keep the old value only if someone edited it by hand (it differs from what the script wrote)
        if k in old and k in auto_old and old[k] != auto_old[k]:
            new[k] = old[k]
    return f"{n} team fields (rank, elo_adj, adj, sd, note), manual settings"


def build(pages, out_dir, manual_teams=None, expander=None, vrs_repo=False):
    """vrs_repo: False = no VRS seeding, None = default clone, or a path to the VRS repository."""
    if not pages:
        sys.exit("No page content: the page does not exist or is empty. Check the title on Liquipedia "
                 "(e.g. 'PGL/2026/Masters', not 'PGL/2026/Masters/Bucharest').")
    infobox, participants, matches = {}, {}, []
    for title, text in pages.items():
        t = clean(text)
        infobox = infobox or parse_infobox(t)
        participants.update(parse_participants(t))
        matches.extend(parse_matches(t, title))
    tags = sorted({x for m in matches for x in (m["team1"], m["team2"]) if x})
    mapping, unresolved = resolve_tags(tags, participants, manual_teams or {})
    if unresolved and expander:
        full = expander(unresolved)               # tag -> team page title via {{Team|tag}}
        for tag, page in full.items():
            hit = [p for p in participants if norm(p) == norm(page) or core(p) == core(page)]
            mapping[tag] = hit[0] if hit else page
        unresolved = [t for t in unresolved if t not in mapping]
    resolved_note = {k: v for k, v in mapping.items() if k != v}
    for m in matches:
        m["team1"], m["team2"] = mapping.get(m["team1"], m["team1"]), mapping.get(m["team2"], m["team2"])
        if m["winner"]:
            m["winner"] = mapping.get(m["winner"], m["winner"])
    # de-duplicate (main page may transclude subpages)
    seen, uniq = set(), []
    for m in matches:
        k = (m["group"], m["key"], m["team1"], m["team2"]) if m["group"] else (m["date"], m["team1"], m["team2"], m["key"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(m)
    bracket_all = [m for m in uniq if m["group_type"] == "bracket"]
    matches = [m for m in uniq if m["team1"] and m["team2"]]
    name = infobox.get("name") or next(iter(pages))
    tier = TIER_LETTER.get(infobox.get("tier", ""), "A")
    lan = infobox.get("type", "").lower() != "online" if infobox.get("type") else True
    event_type = "major" if "major" in name.lower() else "other"
    os.makedirs(out_dir, exist_ok=True)
    report = [f"Event: {name} | tier {infobox.get('tier') or '?'} ({tier}) | {infobox.get('type') or 'type ?'} | "
              f"{infobox.get('sdate') or '?'} - {infobox.get('edate') or '?'} | event_type={event_type}",
              f"Participants with rosters: {len(participants)}; matches found: {len(matches)} "
              f"({sum(m['finished'] for m in matches)} finished)"]
    if resolved_note:
        report.append("Team tags matched to participants: " + ", ".join(f"{k} → {v}" for k, v in sorted(resolved_note.items())))
    if unresolved:
        report.append("WARNING: team tags not matched to any participant (no roster): " + ", ".join(unresolved)
                      + " — pass --team tag=\"Full Name\" to fix")
    short = [t for t, v in participants.items() if len(v["players"]) < 5]
    if short:
        report.append("WARNING: fewer than 5 players parsed for: " + ", ".join(short))
    unknown = sorted({t for m in matches for t in (m["team1"], m["team2"]) if participants and t not in participants})
    if unknown:
        report.append("WARNING: teams in matches but not in participants (TBD or name mismatch): " + ", ".join(unknown))
    json.dump({"infobox": infobox, "participants": participants, "matches": matches},
              open(os.path.join(out_dir, "event.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    fresh = [{"date": m["date"], "team1": m["team1"], "team2": m["team2"], "winner": m["winner"],
              "score": f"{m['score1']}-{m['score2']}" if m["score1"] is not None else None,
              "bo": m["bo"], "tier": tier, "lan": lan, "event": name, "stage": stage_key(m), "id": m["key"]}
             for m in matches if m["finished"] and not m["walkover"]]
    wo = sum(1 for m in matches if m["finished"] and m["walkover"])
    if wo:
        report.append(f"{wo} walkover(s) left out of matches.json (no game was played); they still count in swiss rounds")
    missing_dates = sum(1 for f in fresh if not f["date"])
    if missing_dates:
        report.append(f"WARNING: {missing_dates} finished matches without a date — fill them in before elo.py --extra")
    json.dump({"source": f"Liquipedia: {', '.join(pages)} (CC BY-SA 3.0)", "matches": fresh},
              open(os.path.join(out_dir, "matches.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    report.append(f"matches.json: {len(fresh)} finished matches (for elo.py --extra and predict.py results)")

    # Swiss stages
    stages = {}
    for m in matches:
        if m["group_type"] == "matchlist" and m["round"]:
            stages.setdefault(stage_key(m), []).append(m)
    n = 0
    swiss_final = None
    for sname, ms in stages.items():
        r1 = [m for m in ms if m["round"] == 1]
        if len(r1) != 8:
            report.append(f"Stage '{sname}': {len(r1)} round-1 matches (need 8 for a 16-team Swiss) — skipped")
            continue
        n += 1
        seeds = [m["team1"] for m in r1] + [m["team2"] for m in r1]
        rounds = []
        for r in range(1, 6):
            rr = [m for m in ms if m["round"] == r]
            if rr:
                rounds.append([[m["team1"], m["team2"], m["winner"]] for m in rr])
        bo_r1 = {m["bo"] for m in r1}
        later = [m["bo"] for m in ms if m["round"] and m["round"] >= 2 and m["bo"]]
        fmt = "all_bo3" if bo_r1 == {3} else ("all_bo1" if bo_r1 == {1} and later and set(later) == {1} else "standard")
        best, msg = verify_swiss(seeds, rounds)
        seed_note = "seeds from round-1 order"
        if not best and vrs_repo is not False:
            vseeds, vbest, seed_note = vrs_seeds(rounds[0], rounds, infobox.get("sdate"), vrs_repo)
            if vseeds:
                seeds = vseeds
                best, msg = vbest or (None, msg)
        pairing, round1 = (best[1], best[2]) if best else ("valve" if event_type == "major" else "high_low", "1v9")
        if round1 == "1v16":
            seeds = seeds[:8] + list(reversed(seeds[8:]))
        stage = {"stage": f"{name} — {sname}", "format": fmt, "event_type": event_type,
                 "pairing": pairing, "round1": round1,
                 "teams": [{"name": t, "roster": participants.get(t, {}).get("players", [])} for t in seeds],
                 "rounds": rounds, "map_prob_overrides": {}, "target_correct": 5,
                 "_source": f"{seed_note}; round-1 pairs and sides from Liquipedia (opponent1 = upper half); "
                            "check against the official seeding"}
        standings = final_order(seeds, rounds)
        if standings:
            swiss_final = standings
            stage["final_standings"] = standings
        stage["_auto"] = {k: stage[k] for k in ("pairing", "round1", "event_type", "format")}
        path = os.path.join(out_dir, f"swiss_{n}.json")
        kept = merge_previous(path, stage)
        json.dump(stage, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        if kept:
            report.append(f"{os.path.basename(path)}: kept from the previous file: {kept}")
        played = sum(1 for r in rounds for m in r if m[2])
        report.append(f"{os.path.basename(path)}: '{sname}', format {fmt}, {played} of {sum(len(r) for r in rounds)} "
                      f"listed matches played; {seed_note}; {msg}")

    # 8-team playoff bracket (Bracket/8 on Liquipedia: R1M1..R1M4 quarterfinals, R2 semis, R3M1 final)
    qf_all = {m["key"]: m for m in bracket_all if re.fullmatch(r"R1M[1-4]", m["key"] or "")}
    if qf_all:
        q = [qf_all.get(f"R1M{i}") for i in range(1, 5)]
        teams = []
        for m in q:
            teams += [m["team1"], m["team2"]] if m else [None, None]
        source = "Liquipedia bracket order R1M1..R1M4 (top half first)"
        if None in teams and swiss_final:
            seeds = swiss_final[:8]
            teams = [seeds[0], seeds[7], seeds[3], seeds[4], seeds[1], seeds[6], seeds[2], seeds[5]]
            source = ("bracket still TBD on Liquipedia: seeded from the Swiss final standings "
                      "(record, Difficulty Score, initial seed; QF 1v8, 4v5, 2v7, 3v6 as at PGL Masters 2025) — verify")
        if None in teams:
            report.append("Playoffs: Bracket/8 found but quarterfinal teams are TBD and the Swiss stage is not finished — "
                          "use swiss_sim.py --playoffs for champion odds")
        else:
            rmap = {"R1M1": "QF1", "R1M2": "QF2", "R1M3": "QF3", "R1M4": "QF4", "R2M1": "SF1", "R2M2": "SF2", "R3M1": "GF"}
            results = {rmap[m["key"]]: m["winner"] for m in matches if m["group_type"] == "bracket"
                       and m["key"] in rmap and m["winner"]}
            final_bo = next((m["bo"] for m in bracket_all if m["key"] == "R3M1" and m["bo"]), 5)
            qbo = [(m["bo"] if m and m["bo"] else 3) for m in q]
            bracket = {"event": f"{name} — Playoffs", "event_type": event_type,
                       "teams": [{"name": t, "roster": participants.get(t, {}).get("players", [])} for t in teams],
                       "map_prob_overrides": {},
                       "matches": [{"id": "QF1", "a": teams[0], "b": teams[1], "bo": qbo[0]},
                                   {"id": "QF2", "a": teams[2], "b": teams[3], "bo": qbo[1]},
                                   {"id": "QF3", "a": teams[4], "b": teams[5], "bo": qbo[2]},
                                   {"id": "QF4", "a": teams[6], "b": teams[7], "bo": qbo[3]},
                                   {"id": "SF1", "a": "W:QF1", "b": "W:QF2", "bo": 3},
                                   {"id": "SF2", "a": "W:QF3", "b": "W:QF4", "bo": 3},
                                   {"id": "GF", "a": "W:SF1", "b": "W:SF2", "bo": final_bo}],
                       "results": results,
                       "placements": {"Champion": ["W:GF"], "Runner-up": ["L:GF"]},
                       "_source": source}
            path = os.path.join(out_dir, "playoffs.json")
            kept = merge_previous(path, bracket)
            json.dump(bracket, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            report.append(f"playoffs.json: 8-team bracket ({'seeded from Swiss standings' if 'TBD' in source else 'from Liquipedia'}), "
                          f"{len(results)} matches played, final Bo{final_bo}" + (f"; kept: {kept}" if kept else ""))
    elif any(m["group_type"] == "bracket" for m in uniq):
        report.append("Playoffs: a bracket was found but not in the 8-team R1M1..R1M4 layout — use bracket_sim.py by hand")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("page", nargs="?", help='Liquipedia page title, e.g. "PGL/2026/Masters"')
    ap.add_argument("--wikitext", nargs="+", help="parse saved wikitext files instead of downloading")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-subpages", action="store_true")
    ap.add_argument("--throttle", type=float, default=30.0)
    ap.add_argument("--cache")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--with-qualifiers", action="store_true", help="also read qualifier / regional subpages")
    ap.add_argument("--vrs-repo", help="path to a clone of Valve's VRS repository (default: clone to the temp dir)")
    ap.add_argument("--no-vrs", action="store_true",
                    help="keep the Liquipedia round-1 order as seeds (no VRS lookup)")
    ap.add_argument("--team", action="append", default=[], metavar='TAG="Full Name"',
                    help="map a team-template tag to a participant, e.g. --team vit=\"Team Vitality\"")
    a = ap.parse_args()
    manual = {}
    for it in a.team:
        k, _, v = it.partition("=")
        if v:
            manual[k.strip()] = v.strip().strip('"')
    expander = None
    if a.wikitext:
        pages = {os.path.basename(p): open(p, encoding="utf-8").read() for p in a.wikitext}
    elif a.page:
        title = a.page.strip().replace(" ", "_").split("liquipedia.net/counterstrike/")[-1].replace("_", " ")
        f = Fetcher(a.throttle, a.cache or os.path.join(a.out, "wikitext"), a.refresh)
        listed = os.path.join(f.cache, ".titles")
        if os.path.exists(listed) and not a.refresh:
            titles = [t for t in open(listed, encoding="utf-8").read().split("\n") if t]
        else:
            subs = [] if a.no_subpages else f.subpages(title)
            if not a.with_qualifiers:
                skipped = [t for t in subs if QUALIFIER_RE.search(t)]
                subs = [t for t in subs if not QUALIFIER_RE.search(t)]
                if skipped:
                    print(f"# skipped {len(skipped)} qualifier/regional subpages (use --with-qualifiers)", file=sys.stderr)
            titles = [title] + subs
        print(f"# pages: {', '.join(titles)}", file=sys.stderr)
        pages = f.wikitext(titles)
        if title not in pages:
            sys.exit(f"Page '{title}' not found on Liquipedia. Check the exact title in the page URL "
                     "(e.g. 'PGL/2026/Masters').")
        open(listed, "w", encoding="utf-8").write("\n".join(titles))
        expander = f.expand_teams
    else:
        ap.error("give a page title or --wikitext files")
    for line in build(pages, a.out, manual, expander, vrs_repo=False if a.no_vrs else a.vrs_repo):
        print(line)


if __name__ == "__main__":
    main()
