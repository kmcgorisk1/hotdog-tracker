// Hotdog Tracker live-scores relay (Cloudflare Worker).
//
// The league is private, so the tracker page can't ask ESPN for scores itself without
// showing everyone the login cookies. This small program keeps the cookies as Cloudflare
// secrets, fetches this week's live scores and ESPN projections at most about once a
// minute, and hands the page just the numbers it needs at /live.

const ALLOWED_ORIGINS = [
  "https://poolorpondhotdogs.com",
  "https://www.poolorpondhotdogs.com",
  "https://kmcgorisk1.github.io",
];
const API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/";
const FRESH_MS = 55 * 1000;

let memo = { at: 0, body: null };

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0],
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Vary": "Origin",
    };
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (url.pathname !== "/live") {
      return new Response("Hotdog Tracker live scores. The numbers are at /live.\n", {
        status: 404,
        headers: { ...cors, "content-type": "text/plain; charset=utf-8" },
      });
    }

    const now = Date.now();
    if (memo.body && now - memo.at < FRESH_MS) return reply(memo.body, cors, "cached");
    try {
      const body = await getLive(env);
      memo = { at: now, body };
      return reply(body, cors, "fresh");
    } catch (err) {
      if (memo.body) return reply({ ...memo.body, stale: true }, cors, "stale");
      return reply({ error: String((err && err.message) || err) }, cors, "error", 502);
    }
  },
};

function reply(body, cors, source, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...cors,
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=30",
      "x-hotdog-source": source,
    },
  });
}

function seasonOf(env) {
  if (env.SEASON) return String(env.SEASON);
  const d = new Date();
  return String(d.getUTCMonth() >= 2 ? d.getUTCFullYear() : d.getUTCFullYear() - 1);
}

async function espn(env, query, filter) {
  let swid = String(env.ESPN_SWID || "").trim();
  if (swid && !swid.startsWith("{")) swid = "{" + swid.replace(/[{}]/g, "") + "}";
  const headers = { Accept: "application/json", "User-Agent": "Mozilla/5.0 (Hotdog Tracker live relay)" };
  if (env.ESPN_S2 && swid) headers.Cookie = "espn_s2=" + String(env.ESPN_S2).trim() + "; SWID=" + swid;
  if (filter) headers["x-fantasy-filter"] = JSON.stringify(filter);
  const res = await fetch(API + seasonOf(env) + "/segments/0/leagues/" + env.LEAGUE_ID + query, { headers });
  if (res.status === 401 || res.status === 403) throw new Error("ESPN refused the login cookies. They may have expired.");
  if (!res.ok) throw new Error("ESPN answered with status " + res.status);
  return res.json();
}

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? Math.round(v * 100) / 100 : null);
const DONE = ["HOME", "AWAY", "TIE"];

async function getLive(env) {
  const league = await espn(env, "?view=mMatchupScore");
  const regular = (league.schedule || []).filter(
    (m) => (m.playoffTierType || "NONE") === "NONE" && m.home && m.away
  );
  // Same rule as tracker.py: the week in play is the earliest one with an undecided game.
  const open = regular.filter((m) => !DONE.includes(m.winner));
  const week = open.length
    ? Math.min(...open.map((m) => m.matchupPeriodId))
    : (league.status && league.status.currentMatchupPeriod) || null;
  if (!week) return { week: null, fetchedAt: new Date().toISOString(), games: [] };

  let board = null;
  try {
    const sp = league.scoringPeriodId ? "&scoringPeriodId=" + league.scoringPeriodId : "";
    board = await espn(env, "?view=mScoreboard" + sp, { schedule: { filterMatchupPeriodIds: { value: [week] } } });
  } catch (err) {
    board = null; // the scores in mMatchupScore are still good enough
  }

  const live = {};
  for (const src of [league, board || {}]) {
    for (const m of src.schedule || []) {
      for (const side of ["home", "away"]) {
        const s = m[side] || {};
        if (s.totalPointsLive == null && s.totalProjectedPointsLive == null) continue;
        const d = live[m.id + side] || (live[m.id + side] = {});
        if (s.totalPointsLive != null) d.pts = s.totalPointsLive;
        if (s.totalProjectedPointsLive != null) d.proj = s.totalProjectedPointsLive;
      }
    }
  }

  const games = regular
    .filter((m) => m.matchupPeriodId === week)
    .map((m) => {
      const h = live[m.id + "home"] || {};
      const a = live[m.id + "away"] || {};
      return {
        home: m.home.teamId,
        away: m.away.teamId,
        hs: num(h.pts != null ? h.pts : m.home.totalPoints),
        as: num(a.pts != null ? a.pts : m.away.totalPoints),
        hp: num(h.proj),
        ap: num(a.proj),
        winner: m.winner || "UNDECIDED",
      };
    });

  return { week, scoringPeriodId: league.scoringPeriodId || null, fetchedAt: new Date().toISOString(), games };
}
