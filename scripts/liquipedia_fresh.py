#!/usr/bin/env python3
"""
Collect fresh matches (played after the last VRS update) from Liquipedia team match pages into
the --extra file for elo.py.

Why: VRS data lags by up to a month, and the web pages of Liquipedia refuse automated fetches
(HTTP 403), but its MediaWiki API (action=parse) serves the same rendered match tables.
Liquipedia's API terms: at most one parse request per 30 seconds and a descriptive User-Agent,
so 16 teams take ~8 minutes. Run it in the background. Pages are cached in --cache.

Usage:
  python liquipedia_fresh.py --from 2026-10-04 --to 2026-10-24 --out fresh.json \\
      "Team_Spirit=Spirit" "Natus_Vincere" "FUT_Esports=FUT" ...
  Each team is a Liquipedia page name (spaces as _), optionally "=<name>" to set the name written
  into the file (use the names from your stage file). The opponent name is taken from the link
  title on the page; elo.py matches it against the VRS data and warns about unknown teams.
  --from is inclusive, --to exclusive (the tournament start). Draws and walkovers are skipped;
  a series listed on both teams' pages is written once.
Options: --cache DIR (default: ./liquipedia_cache), --delay 31, --refresh (ignore the cache).
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://liquipedia.net/counterstrike/api.php"
UA = "cs2-pickem-skill/0.3 (fresh-match collector; respects 1 parse request / 30 s)"


def fetch(page, cache, delay, refresh):
    path = os.path.join(cache, re.sub(r"[^A-Za-z0-9_.-]", "_", page) + ".json")
    if os.path.exists(path) and not refresh:
        return json.load(open(path, encoding="utf-8")), False
    q = urllib.parse.urlencode({"action": "parse", "page": f"{page}/Matches", "prop": "text", "format": "json"})
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            break
        except (OSError, ValueError) as e:  # truncated body / network error: wait and retry
            print(f"# {page}: {e}; retrying", file=sys.stderr)
            time.sleep(delay)
    else:
        sys.exit(f"{page}: failed 3 times")
    if "error" in data:
        sys.exit(f"{page}: {data['error'].get('info')}")
    os.makedirs(cache, exist_ok=True)
    json.dump(data, open(path, "w", encoding="utf-8"))
    return data, True


def text(cell):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell))).replace("\xa0", " ").strip()


def parse(data, me, lo, hi):
    """Rows of a team's /Matches table: date, tier, LAN/online, event, score, opponent."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", data["parse"]["text"]["*"], re.S):
        ts = re.search(r'data-timestamp="(\d+)"', row)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if not ts or len(tds) < 9:
            continue
        date = dt.datetime.fromtimestamp(int(ts.group(1)), dt.timezone.utc).strftime("%Y-%m-%d")
        if not lo <= date < hi:
            continue
        sc = re.match(r"(\d+)\s*:\s*(\d+)", text(tds[7]))
        if not sc or sc.group(1) == sc.group(2):
            continue
        opp = re.search(r'title="([^"]+)"', tds[8])
        out.append({"date": date, "team1": me, "team2": html.unescape(opp.group(1)) if opp else text(tds[8]),
                    "score": f"{sc.group(1)}-{sc.group(2)}", "tier": (text(tds[1])[:1] or "C").upper(),
                    "event": text(tds[5]), "lan": text(tds[2]).lower().startswith("off")})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("teams", nargs="+")
    ap.add_argument("--from", dest="lo", required=True)
    ap.add_argument("--to", dest="hi", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default="liquipedia_cache")
    ap.add_argument("--delay", type=float, default=31.0)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    teams = [t.split("=", 1) if "=" in t else (t, t.replace("_", " ")) for t in a.teams]
    alias = {page.replace("_", " "): name for page, name in teams}  # opponents that are also entrants
    seen, matches, need_wait = set(), [], False
    for page, name in teams:
        if need_wait:
            time.sleep(a.delay)
        data, need_wait = fetch(page, a.cache, a.delay, a.refresh)
        rows = parse(data, name, a.lo, a.hi)
        for m in rows:
            m["team2"] = alias.get(m["team2"], m["team2"])
            key = (m["date"], frozenset((m["team1"], m["team2"])), m["event"])
            if key not in seen:
                seen.add(key)
                matches.append(m)
        print(f"# {name}: {len(rows)} series {a.lo}..{a.hi}", file=sys.stderr)
    matches.sort(key=lambda m: m["date"])
    json.dump({"source": f"Liquipedia team match pages via API, collected {dt.date.today()}", "matches": matches},
              open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"# wrote {len(matches)} series to {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
