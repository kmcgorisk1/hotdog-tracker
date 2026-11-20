#!/usr/bin/env python3
"""
Hotdog Tracker: daily last-place odds for an ESPN fantasy football league.

Every morning GitHub Actions runs this script, which:
  1. Pulls standings and the schedule from ESPN's fantasy API
  2. Simulates the rest of the regular season thousands of times
  3. Writes site/index.html (built from template.html) and site/data.json

Settings come from environment variables, stored as GitHub Secrets:
  LEAGUE_ID   ESPN league id (required)
  ESPN_S2     espn_s2 cookie (needed for private leagues)
  ESPN_SWID   SWID cookie   (needed for private leagues)
  SEASON      optional; defaults to the current NFL season
  MOCK_FILE   optional; path to saved ESPN JSON, for testing without ESPN

Uses only the Python standard library, so there is nothing to install.
"""

import html
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- settings
SIMS = 20000            # simulations for today's odds
HISTORY_SIMS = 4000     # simulations per completed week for the trend chart
PRIOR_GAMES = 4         # pulls each team's scoring average toward the league average
RECENT_WEEKS = 4        # the last few weeks count extra (roster changes, injuries)
RECENT_WEIGHT = 2.0
DEFAULT_SD = 25.0       # weekly scoring spread used before there is enough data
MIN_SD = 15.0
SHOW_OWNER_NAMES = True # show "First L." under each team name

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
OUT_DIR = os.path.join(HERE, "site")

API = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
       "{season}/segments/0/leagues/{league}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def fail(msg):
    print(f"\n*** HOTDOG TRACKER ERROR ***\n{msg}\n", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- ESPN fetch
def fetch(season, league_id, s2, swid, views, extra=None):
    params = [("view", v) for v in views] + list(extra or [])
    url = API.format(season=season, league=league_id) + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if s2 and swid:
        headers["Cookie"] = f"espn_s2={s2}; SWID={swid}"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                fail("ESPN says this league is private and wouldn't share it.\n"
                     "Fix: copy fresh espn_s2 and SWID cookies from espn.com into the repo's\n"
                     "Settings -> Secrets and variables -> Actions (ESPN_S2 and ESPN_SWID).")
            if e.code == 404:
                fail(f"ESPN couldn't find league {league_id} for season {season}.\n"
                     "Check the LEAGUE_ID secret (the number after leagueId= in the league URL).")
            last_err = e
        except Exception as e:  # network hiccup: wait and retry
            last_err = e
        time.sleep(5 * (attempt + 1))
    fail(f"Couldn't reach ESPN after 3 tries: {last_err}")


def load_data():
    mock = os.environ.get("MOCK_FILE")
    if mock:
        with open(mock) as f:
            data = json.load(f)
        return data["league"], data.get("scoreboard"), True

    league_id = os.environ.get("LEAGUE_ID", "").strip()
    if not league_id:
        fail("LEAGUE_ID is missing. Add it under Settings -> Secrets and variables -> Actions.")
    now = datetime.now(timezone.utc)
    season = int(os.environ.get("SEASON") or (now.year if now.month >= 3 else now.year - 1))
    s2 = os.environ.get("ESPN_S2", "").strip()
    swid = os.environ.get("ESPN_SWID", "").strip()
    if swid and not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"

    league = fetch(season, league_id, s2, swid,
                   ["mTeam", "mSettings", "mMatchupScore", "mStandings"])
    scoreboard = None
    try:  # live scores and projections for the week in progress (nice to have)
        week = (league.get("status") or {}).get("currentMatchupPeriod")
        extra = [("scoringPeriodId", league.get("scoringPeriodId"))] if league.get("scoringPeriodId") else []
        if week:
            scoreboard = fetch(season, league_id, s2, swid, ["mScoreboard"], extra)
    except SystemExit:
        raise
    except Exception as e:
        print(f"(live scoreboard unavailable, continuing without it: {e})")
    return league, scoreboard, False


# ---------------------------------------------------------------- parsing
def parse(league, scoreboard):
    settings = league.get("settings") or {}
    reg_weeks = int((settings.get("scheduleSettings") or {}).get("matchupPeriodCount") or 14)
    status = league.get("status") or {}
    members = {m.get("id"): m for m in league.get("members") or []}

    teams = {}
    for t in league.get("teams") or []:
        tid = t["id"]
        name = (t.get("name") or f'{t.get("location", "")} {t.get("nickname", "")}').strip()
        owner = ""
        owners = t.get("owners") or ([t["primaryOwner"]] if t.get("primaryOwner") else [])
        if SHOW_OWNER_NAMES and owners and owners[0] in members:
            m = members[owners[0]]
            first = (m.get("firstName") or "").strip()
            last = (m.get("lastName") or "").strip()
            owner = f"{first} {last[:1]}." if first and last else (first or m.get("displayName") or "")
        teams[tid] = {"id": tid, "name": name or f"Team {tid}",
                      "abbrev": t.get("abbrev") or "", "owner": owner}
    if len(teams) < 2:
        fail("ESPN returned no teams. Is the LEAGUE_ID right and is the season set up?")

    # live numbers for the week in progress, keyed by matchup id
    live = {}
    for src in (league, scoreboard or {}):
        for m in src.get("schedule") or []:
            for side in ("home", "away"):
                s = m.get(side) or {}
                proj = s.get("totalProjectedPointsLive")
                pts = s.get("totalPointsLive")
                if proj is not None or pts is not None:
                    d = live.setdefault((m.get("id"), side), {})
                    if proj is not None:
                        d["proj"] = float(proj)
                    if pts is not None:
                        d["pts"] = float(pts)

    games = []
    for m in league.get("schedule") or []:
        wk = m.get("matchupPeriodId")
        if wk is None or wk > reg_weeks:
            continue
        if (m.get("playoffTierType") or "NONE") != "NONE":
            continue
        home, away = m.get("home"), m.get("away")
        if not home or not away:
            continue  # bye week
        if home.get("teamId") not in teams or away.get("teamId") not in teams:
            continue
        winner = m.get("winner") or "UNDECIDED"
        g = {"week": wk, "home": home["teamId"], "away": away["teamId"],
             "hs": float(home.get("totalPoints") or 0), "as": float(away.get("totalPoints") or 0),
             "winner": winner, "done": winner in ("HOME", "AWAY", "TIE")}
        if not g["done"]:
            hl, al = live.get((m.get("id"), "home"), {}), live.get((m.get("id"), "away"), {})
            g["hlive"], g["alive"] = hl.get("pts", g["hs"]), al.get("pts", g["as"])
            g["hproj"], g["aproj"] = hl.get("proj"), al.get("proj")
        games.append(g)
    games.sort(key=lambda g: g["week"])
    if not games:
        fail("No regular-season matchups found in ESPN's data.")

    return {
        "name": settings.get("name") or "Fantasy League",
        "season": league.get("seasonId"),
        "reg_weeks": reg_weeks,
        "current_week": status.get("currentMatchupPeriod"),
        "teams": teams,
        "games": games,
    }


# ---------------------------------------------------------------- model
def team_models(teams, done_games):
    """Expected weekly score for each team plus a league-wide weekly spread."""
    pts = {tid: [] for tid in teams}
    for g in done_games:
        pts[g["home"]].append((g["week"], g["hs"]))
        pts[g["away"]].append((g["week"], g["as"]))
    every = [p for lst in pts.values() for _, p in lst]
    if not every:
        return {tid: 110.0 for tid in teams}, DEFAULT_SD
    league_avg = sum(every) / len(every)
    latest = max(w for lst in pts.values() for w, _ in lst)

    means, sq, n_resid = {}, 0.0, 0
    for tid, lst in pts.items():
        if not lst:
            means[tid] = league_avg
            continue
        weights = [RECENT_WEIGHT if w > latest - RECENT_WEEKS else 1.0 for w, _ in lst]
        wavg = sum(wt * p for wt, (_, p) in zip(weights, lst)) / sum(weights)
        n = len(lst)
        means[tid] = (n * wavg + PRIOR_GAMES * league_avg) / (n + PRIOR_GAMES)
        raw = sum(p for _, p in lst) / n
        sq += sum((p - raw) ** 2 for _, p in lst)
        n_resid += n - 1
    sd = math.sqrt(sq / n_resid) if n_resid >= 12 else DEFAULT_SD
    return means, max(MIN_SD, sd)


def side_dist(mean, sd, live_pts, proj):
    """Use ESPN's live projection once a week is under way; shrink the spread as games finish."""
    if proj and proj > 0:
        frac_left = min(1.0, max(0.05, 1.0 - (live_pts or 0) / proj))
        return proj, sd * math.sqrt(frac_left)
    return mean, sd


def setup(league, upto_week=None, use_live=True):
    """Split the schedule into finished results and games still to simulate.

    upto_week: pretend only games through this week are finished (for the trend chart).
    """
    teams = league["teams"]
    tids = sorted(teams)
    idx = {t: i for i, t in enumerate(tids)}
    T = len(tids)

    def is_done(g):
        return g["done"] and (upto_week is None or g["week"] <= upto_week)

    done = [g for g in league["games"] if is_done(g)]
    means, sd = team_models(teams, done)

    base_w, base_pf, rem_n = [0.0] * T, [0.0] * T, [0] * T
    played = []                 # (i, j, wins for i) for finished games
    remaining = []              # (i, j, mean_i, sd_i, mean_j, sd_j, week)
    for g in league["games"]:
        i, j = idx[g["home"]], idx[g["away"]]
        if is_done(g):
            wi = 1.0 if g["winner"] == "HOME" else 0.0 if g["winner"] == "AWAY" else 0.5
            base_w[i] += wi
            base_w[j] += 1 - wi
            base_pf[i] += g["hs"]
            base_pf[j] += g["as"]
            played.append((i, j, wi))
        else:
            mi, si = means[g["home"]], sd
            mj, sj = means[g["away"]], sd
            if use_live and upto_week is None:
                mi, si = side_dist(mi, sd, g.get("hlive"), g.get("hproj"))
                mj, sj = side_dist(mj, sd, g.get("alive"), g.get("aproj"))
            remaining.append((i, j, mi, si, mj, sj, g["week"]))
            rem_n[i] += 1
            rem_n[j] += 1
    return {"tids": tids, "base_w": base_w, "base_pf": base_pf, "rem_n": rem_n,
            "played": played, "remaining": remaining, "means": means, "sd": sd}


def simulate(league, upto_week=None, n_sims=SIMS, seed=1, use_live=True):
    """Simulate unfinished regular-season games and count who finishes last."""
    s = setup(league, upto_week, use_live)
    tids, base_w, base_pf, rem_n = s["tids"], s["base_w"], s["base_pf"], s["rem_n"]
    played, remaining, means, sd = s["played"], s["remaining"], s["means"], s["sd"]
    T = len(tids)

    swing_week = min((r[6] for r in remaining), default=None)
    rng = random.Random(seed)
    last = [0] * T
    win_n, win_last, loss_n, loss_last = [0] * T, [0] * T, [0] * T, [0] * T
    total_w = [0.0] * T

    for _ in range(n_sims):
        w, pf = base_w[:], base_pf[:]
        sim_games = []
        swing_result = {}
        for (i, j, mi, si, mj, sj, wk) in remaining:
            x, y = rng.gauss(mi, si), rng.gauss(mj, sj)
            pf[i] += x
            pf[j] += y
            wi = 1.0 if x > y else 0.0
            w[i] += wi
            w[j] += 1 - wi
            sim_games.append((i, j, wi))
            if wk == swing_week:
                swing_result[i] = wi == 1.0
                swing_result[j] = wi == 0.0
        loser = find_last(w, pf, played, sim_games)
        last[loser] += 1
        for k in range(T):
            total_w[k] += w[k]
        for k, won in swing_result.items():
            if won:
                win_n[k] += 1
                win_last[k] += loser == k
            else:
                loss_n[k] += 1
                loss_last[k] += loser == k

    out = {}
    for k, tid in enumerate(tids):
        others = [o for o in range(T) if o != k]
        safe = any(base_w[o] + rem_n[o] < base_w[k] for o in others)
        doomed = all(base_w[k] + rem_n[k] < base_w[o] for o in others)
        out[tid] = {
            "pLast": last[k] / n_sims,
            "projWins": total_w[k] / n_sims,
            "ifWin": (win_last[k] / win_n[k]) if win_n[k] else None,
            "ifLose": (loss_last[k] / loss_n[k]) if loss_n[k] else None,
            "safe": safe,
            "doomed": doomed,
            "remaining": rem_n[k],
        }
    return out, swing_week, means, sd


def find_last(w, pf, played, sim_games):
    """Worst record; ties go to head-to-head among the tied teams, then fewest points scored."""
    low = min(w)
    tied = [k for k, v in enumerate(w) if v == low]
    if len(tied) == 1:
        return tied[0]
    tset = set(tied)
    h2h = {k: [0.0, 0] for k in tied}
    for games in (played, sim_games):
        for i, j, wi in games:
            if i in tset and j in tset:
                h2h[i][0] += wi
                h2h[i][1] += 1
                h2h[j][0] += 1 - wi
                h2h[j][1] += 1
    pct = {k: (h2h[k][0] / h2h[k][1]) if h2h[k][1] else 0.5 for k in tied}
    worst = min(pct.values())
    still = [k for k in tied if pct[k] == worst]
    return min(still, key=lambda k: pf[k])


# ---------------------------------------------------------------- build
def build(league, is_mock):
    teams, games = league["teams"], league["games"]
    done_weeks = sorted({g["week"] for g in games if g["done"]})
    # a week counts as complete only when every game in it is final
    complete = [wk for wk in done_weeks if all(g["done"] for g in games if g["week"] == wk)]
    last_complete = max(complete, default=0)

    now_odds, swing_week, means, sd = simulate(league, n_sims=SIMS, seed=2026)

    # trend chart: odds as they stood after each finished week
    history_labels, history = [], {tid: [] for tid in teams}
    for wk in range(1, last_complete + 1):
        odds, *_ = simulate(league, upto_week=wk, n_sims=HISTORY_SIMS, seed=wk)
        history_labels.append(f"Wk {wk}")
        for tid in teams:
            history[tid].append(round(odds[tid]["pLast"], 4))

    live_now = any((g.get("hlive") or g.get("alive")) for g in games
                   if not g["done"] and g["week"] == swing_week)
    if live_now:
        history_labels.append("Live")
        for tid in teams:
            history[tid].append(round(now_odds[tid]["pLast"], 4))
    elif history_labels:  # keep the chart's last point identical to the leaderboard
        for tid in teams:
            history[tid][-1] = round(now_odds[tid]["pLast"], 4)

    # records
    rec = {tid: {"w": 0, "l": 0, "t": 0, "pf": 0.0} for tid in teams}
    for g in games:
        if not g["done"]:
            continue
        h, a = rec[g["home"]], rec[g["away"]]
        h["pf"] += g["hs"]
        a["pf"] += g["as"]
        if g["winner"] == "HOME":
            h["w"] += 1; a["l"] += 1
        elif g["winner"] == "AWAY":
            a["w"] += 1; h["l"] += 1
        else:
            h["t"] += 1; a["t"] += 1

    # recent results and weekly scores for each team
    form = {tid: [] for tid in teams}
    scores = {tid: [] for tid in teams}
    for g in sorted((g for g in games if g["done"]), key=lambda g: g["week"]):
        hr = "W" if g["winner"] == "HOME" else "L" if g["winner"] == "AWAY" else "T"
        form[g["home"]].append(hr)
        form[g["away"]].append({"W": "L", "L": "W", "T": "T"}[hr])
        scores[g["home"]].append(round(g["hs"], 1))
        scores[g["away"]].append(round(g["as"], 1))
    every_score = [p for v in scores.values() for p in v]
    league_avg = round(sum(every_score) / len(every_score), 1) if every_score else None

    # everything the page needs to re-run the season in the browser for "what if" picks
    m = setup(league)
    model = {
        "ids": m["tids"],
        "w": m["base_w"],
        "pf": [round(x, 1) for x in m["base_pf"]],
        "played": [[i, j, wi] for i, j, wi in m["played"]],
        "games": [[i, j, round(mi, 1), round(si, 1), round(mj, 1), round(sj, 1), wk]
                  for i, j, mi, si, mj, sj, wk in m["remaining"]],
    }

    # this week's matchups
    matchups = []
    for g in games:
        if g["week"] != swing_week:
            continue
        sides = []
        for side, s_live, s_proj in (("home", "hlive", "hproj"), ("away", "alive", "aproj")):
            tid = g[side]
            o = now_odds[tid]
            sides.append({"id": tid, "pLast": round(o["pLast"], 4),
                          "ifWin": None if o["ifWin"] is None else round(o["ifWin"], 4),
                          "ifLose": None if o["ifLose"] is None else round(o["ifLose"], 4),
                          "score": round(g.get(s_live) or 0, 1),
                          "proj": round(g[s_proj], 1) if g.get(s_proj) else round(means[tid], 1)})
        stakes = sum(abs((s["ifLose"] or 0) - (s["ifWin"] or 0)) for s in sides)
        matchups.append({"home": sides[0], "away": sides[1], "stakes": round(stakes, 4)})
    matchups.sort(key=lambda m: -m["stakes"])

    next_opp = {}
    for g in games:
        if g["week"] == swing_week:
            next_opp[g["home"]] = g["away"]
            next_opp[g["away"]] = g["home"]

    rows = []
    for tid, t in teams.items():
        o, r = now_odds[tid], rec[tid]
        settled = o["remaining"] == 0 and swing_week is None
        doomed = o["doomed"] or (settled and o["pLast"] == 1.0)
        safe = o["safe"] or (o["pLast"] == 0.0 and (settled or o["remaining"] == 0))
        status = "doomed" if doomed else "safe" if safe else "alive"
        rows.append({
            "id": tid, "name": t["name"], "owner": t["owner"], "abbrev": t["abbrev"],
            "wins": r["w"], "losses": r["l"], "ties": r["t"], "pf": round(r["pf"], 1),
            "pLast": round(o["pLast"], 4), "projWins": round(o["projWins"], 2),
            "ifWin": None if o["ifWin"] is None else round(o["ifWin"], 4),
            "ifLose": None if o["ifLose"] is None else round(o["ifLose"], 4),
            "status": status, "remaining": o["remaining"],
            "nextOpp": next_opp.get(tid), "weeklyAvg": round(means[tid], 1),
            "form": form[tid][-5:], "scores": scores[tid],
        })
    rows.sort(key=lambda r: (-r["pLast"], r["wins"] + 0.5 * r["ties"], r["pf"]))

    phase = "final" if swing_week is None else ("preseason" if last_complete == 0 and not live_now else "season")
    return {
        "league": {"name": league["name"], "season": league["season"], "regWeeks": league["reg_weeks"],
                   "teamCount": len(teams)},
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "phase": phase,
        "week": swing_week,
        "lastCompleteWeek": last_complete,
        "weeksLeft": 0 if swing_week is None else league["reg_weeks"] - swing_week + 1,
        "live": live_now,
        "sims": SIMS,
        "weeklySpread": round(sd, 1),
        "teams": rows,
        "history": {"labels": history_labels,
                    "series": {str(tid): vals for tid, vals in history.items()}},
        "matchups": matchups,
        "leagueAvg": league_avg,
        "model": model,
        "mock": is_mock,
    }


def pct(p):
    return "<1%" if 0 < p < 0.005 else f"{round(p * 100)}%"


def render(data):
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read()
    top = data["teams"][0]
    if data["phase"] == "final":
        desc = f"Final: {top['name']} finished last and owes 70 hot dogs."
    else:
        desc = f"Frontrunner: {top['name']} ({pct(top['pLast'])}). Week {data['week']} of {data['league']['regWeeks']}."
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = page.replace("/*__HOTDOG_DATA__*/null", blob)
    page = page.replace("__OG_DESC__", html.escape(desc, quote=True))
    page = page.replace("__LEAGUE_NAME__", html.escape(data["league"]["name"], quote=True))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def main():
    t0 = time.time()
    raw, scoreboard, is_mock = load_data()
    league = parse(raw, scoreboard)
    data = build(league, is_mock)
    render(data)
    print(f"{data['league']['name']}: week {data['week']} of {data['league']['regWeeks']}"
          f" ({'live' if data['live'] else 'not live'}), {len(data['teams'])} teams,"
          f" {time.time() - t0:.1f}s")
    for r in data["teams"]:
        print(f"  {pct(r['pLast']):>5}  {r['wins']}-{r['losses']}{'-' + str(r['ties']) if r['ties'] else ''}"
              f"  {r['pf']:>7.1f}  {r['name']}  [{r['status']}]")


if __name__ == "__main__":
    main()
