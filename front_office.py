#!/usr/bin/env python3
"""Fantasy Front Office engine.

Runs on a schedule in GitHub Actions. Pulls your ESPN league, Sleeper news/injury
data, and FantasyCalc trade values, then writes:
  docs/data.json    -> powers the dashboard (index.html)
  docs/snapshot.md  -> plain-text briefing Claude reads for commentary
  data/history.json -> value/team history used to spot movers and team changes
"""
import datetime as dt
import itertools
import json
import os
import re
import sys
import urllib.request

# ---------------------------------------------------------------- settings
MY_TEAM = os.environ.get("MY_TEAM", "Get Shucked")
OUT_DIR = "docs"
HIST_FILE = "data/history.json"
HISTORY_DAYS = 28
TRADE_POOL = 10          # top players per team considered in trade search
MAX_TRADES = 12
MAX_TRADES_PER_TEAM = 3
FA_POOL = 150
KEEPERS_FILE = "keepers.txt"   # one player name per line: never suggested as drops
HOT_GAME = 18                   # last-game points that make a player undroppable this week

# How much an injury designation dents rest-of-season and this-week value
ROS_MULT = {"QUESTIONABLE": 0.97, "DOUBTFUL": 0.85, "OUT": 0.8,
            "INJURY_RESERVE": 0.45, "IR": 0.45, "SUSPENSION": 0.5}
WEEK_OUT = {"OUT", "INJURY_RESERVE", "IR", "SUSPENSION"}
WEEK_MULT = {"QUESTIONABLE": 0.85, "DOUBTFUL": 0.25}

SLOT_ELIG = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"},
    "RB/WR": {"RB", "WR"}, "WR/TE": {"WR", "TE"}, "RB/WR/TE": {"RB", "WR", "TE"},
    "OP": {"QB", "RB", "WR", "TE"}, "K": {"K"}, "D/ST": {"D/ST"},
}
SKILL = {"QB", "RB", "WR", "TE"}


# ---------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def get_json(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-front-office"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def norm(name):
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", (name or "").lower())
    return re.sub(r"[^a-z]", "", name)


def season_year(today):
    return today.year if today.month >= 3 else today.year - 1


def r1(x):
    return round(float(x or 0), 1)


# ---------------------------------------------------------------- lineup math
def optimal_lineup(players, slots, key):
    """Greedy best lineup: fill strict slots first, then flex slots."""
    pool = sorted(players, key=lambda p: p[key], reverse=True)
    used, lineup = set(), []
    order = sorted((s for s in slots if s in SLOT_ELIG), key=lambda s: len(SLOT_ELIG[s]))
    for s in order:
        for _ in range(slots[s]):
            for p in pool:
                if p["id"] not in used and p["pos"] in SLOT_ELIG[s]:
                    used.add(p["id"])
                    lineup.append((s, p))
                    break
    return sum(p[key] for _, p in lineup), lineup


def lineup_score(players, slots, key="ros"):
    return optimal_lineup(players, slots, key)[0]


def evaluate_trade(roster_a, roster_b, send_ids, get_ids, slots):
    """Return deltas from team A's point of view and team B's."""
    send = [p for p in roster_a if p["id"] in send_ids]
    get = [p for p in roster_b if p["id"] in get_ids]
    new_a = [p for p in roster_a if p["id"] not in send_ids] + get
    new_b = [p for p in roster_b if p["id"] not in get_ids] + send
    a_delta = lineup_score(new_a, slots) - lineup_score(roster_a, slots)
    b_delta = lineup_score(new_b, slots) - lineup_score(roster_b, slots)
    v_send = sum(p["value"] for p in send)
    v_get = sum(p["value"] for p in get)
    return {"a_lineup": round(a_delta, 2), "b_lineup": round(b_delta, 2),
            "a_value": round(v_get - v_send), "v_send": round(v_send), "v_get": round(v_get)}


def verdict(lineup_delta, value_delta, value_sent):
    pct = value_delta / value_sent if value_sent else 0
    if lineup_delta >= 1.0 and pct >= -0.15:
        return "Accept"
    if lineup_delta >= 0.3 and pct >= -0.25:
        return "Lean accept"
    if lineup_delta <= -0.5 and pct >= 0.2:
        return "Value play"
    if lineup_delta <= -0.5 or pct <= -0.3:
        return "Decline"
    return "Neutral"


# ---------------------------------------------------------------- data pulls
def fetch_espn(league_id, year, s2, swid):
    from espn_api.football import League
    kwargs = {"league_id": int(league_id), "year": year}
    if s2 and swid:
        kwargs.update(espn_s2=s2, swid=swid)
    league = League(**kwargs)
    week = league.current_week
    slots = {k: v for k, v in league.settings.position_slot_counts.items()
             if v and k in SLOT_ELIG}
    bench = league.settings.position_slot_counts.get("BE", 0)
    rec_pts = 0
    for item in league.settings.scoring_format:
        if item.get("id") == 53:
            rec_pts = item.get("points", 0)
    teams = []
    for t in league.teams:
        teams.append({
            "id": t.team_id, "name": t.team_name, "abbrev": t.team_abbrev,
            "record": f"{t.wins}-{t.losses}" + (f"-{t.ties}" if t.ties else ""),
            "pf": r1(t.points_for), "standing": t.standing,
            "raw": t.roster,
        })
    weekly = {}
    ids = [p.playerId for t in league.teams for p in t.roster]
    for i in range(0, len(ids), 50):
        try:
            res = league.player_info(playerId=ids[i:i + 50]) or []
            for p in (res if isinstance(res, list) else [res]):
                weekly[str(p.playerId)] = {
                    w: s.get("points", 0) for w, s in p.stats.items()
                    if isinstance(w, int) and 0 < w <= week and "points" in s}
        except Exception as e:
            log(f"Weekly stats pull failed: {e}")
    fas = []
    try:
        fas = league.free_agents(size=FA_POOL)
        for pos in ("D/ST", "K"):
            fas += league.free_agents(size=15, position=pos)
    except Exception as e:  # free agents are a bonus, not fatal
        log(f"Free agent pull failed: {e}")
    matchup = None
    try:
        for b in league.box_scores(week):
            for side, other, mine_p, opp_p in (
                    (b.home_team, b.away_team, b.home_projected, b.away_projected),
                    (b.away_team, b.home_team, b.away_projected, b.home_projected)):
                if getattr(side, "team_name", "") == MY_TEAM:
                    matchup = {"opponent": getattr(other, "team_name", "Bye"),
                               "my_proj": r1(mine_p), "opp_proj": r1(opp_p)}
    except Exception as e:
        log(f"Matchup pull failed: {e}")
    return {"league": league, "week": week, "slots": slots, "bench": bench,
            "ppr": rec_pts, "teams": teams, "weekly": weekly, "fas": fas, "matchup": matchup,
            "name": league.settings.name, "team_count": len(league.teams)}


def fetch_sleeper():
    out = {"by_espn": {}, "trending_add": {}, "trending_drop": {}}
    try:
        players = get_json("https://api.sleeper.app/v1/players/nfl", timeout=180)
    except Exception as e:
        log(f"Sleeper players failed: {e}")
        return out
    sid_to_espn = {}
    for sid, p in players.items():
        eid = p.get("espn_id")
        if not eid:
            continue
        sid_to_espn[sid] = str(eid)
        out["by_espn"][str(eid)] = {
            "team": p.get("team"),
            "injury_status": p.get("injury_status"),
            "injury_body_part": p.get("injury_body_part"),
            "injury_notes": p.get("injury_notes"),
            "practice": p.get("practice_participation"),
            "depth": p.get("depth_chart_order"),
            "depth_pos": p.get("depth_chart_position"),
        }
    for kind in ("add", "drop"):
        try:
            rows = get_json(f"https://api.sleeper.app/v1/players/nfl/trending/{kind}"
                            f"?lookback_hours=48&limit=60")
            for row in rows:
                eid = sid_to_espn.get(str(row.get("player_id")))
                if eid:
                    out[f"trending_{kind}"][eid] = row.get("count", 0)
        except Exception as e:
            log(f"Sleeper trending {kind} failed: {e}")
    return out


def fetch_fantasycalc(team_count, ppr, superflex):
    url = ("https://api.fantasycalc.com/values/current?isDynasty=false"
           f"&numQbs={2 if superflex else 1}&numTeams={team_count}&ppr={ppr}")
    by_espn, by_name = {}, {}
    try:
        for row in get_json(url):
            pl = row.get("player", {})
            rec = {"value": row.get("value", 0), "trend30": row.get("trend30Day", 0),
                   "pos_rank": row.get("positionRank")}
            if pl.get("espnId"):
                by_espn[str(pl["espnId"])] = rec
            by_name[(norm(pl.get("name")), pl.get("position"))] = rec
    except Exception as e:
        log(f"FantasyCalc failed: {e}")
    return by_espn, by_name


# ---------------------------------------------------------------- player model
def build_player(p, week, fc_espn, fc_name, sleeper, owner, weekly=None):
    status = (p.injuryStatus or "ACTIVE").upper()
    wk = p.stats.get(week, {}) or {}
    week_proj = wk.get("projected_points", 0) or 0
    proj_avg = p.projected_avg_points or 0
    avg = p.avg_points or 0
    games = [pts for w, pts in sorted((weekly or {}).items())
             if w < week or (w == week and pts)]
    last3 = games[-3:]
    recent = sum(last3) / len(last3) if last3 else 0
    last = games[-1] if games else 0
    if proj_avg and avg and last3:
        ros = 0.5 * proj_avg + 0.25 * avg + 0.25 * recent
    elif proj_avg and avg:
        ros = 0.65 * proj_avg + 0.35 * avg
    else:
        ros = proj_avg or avg or week_proj
    eid = str(p.playerId)
    fc = fc_espn.get(eid) or fc_name.get((norm(p.name), p.position))
    sl = sleeper["by_espn"].get(eid, {})
    sched = getattr(p, "schedule", {}) or {}
    game = sched.get(week) or sched.get(str(week))
    locked = bool(game and game.get("date") and game["date"] < dt.datetime.now())
    on_bye = bool(sched) and not game
    return {
        "id": eid, "name": p.name, "pos": p.position, "nfl": p.proTeam,
        "status": status, "slot": getattr(p, "lineupSlot", "") or "",
        "owner": owner,
        "week": r1(0 if status in WEEK_OUT else week_proj * WEEK_MULT.get(status, 1)),
        "ros": r1(ros * ROS_MULT.get(status, 1)),
        "avg": r1(avg), "proj_avg": r1(proj_avg),
        "recent": r1(recent), "last": r1(last), "games": [r1(g) for g in last3],
        "fc": fc["value"] if fc else None,
        "trend30": fc["trend30"] if fc else 0,
        "pos_rank": fc["pos_rank"] if fc else None,
        "owned": p.percent_owned,
        "bye": on_bye, "locked": locked,
        "injury": " ".join(x for x in (sl.get("injury_body_part"), sl.get("injury_notes")) if x) or None,
        "practice": sl.get("practice"),
        "depth": sl.get("depth"),
        "adds48": sleeper["trending_add"].get(eid, 0),
        "drops48": sleeper["trending_drop"].get(eid, 0),
    }


def fill_values(all_players):
    """Use FantasyCalc market value when available; estimate the rest
    from projected points so kickers, defenses, and deep players still count."""
    ratios = {}
    for p in all_players:
        if p["fc"] and p["ros"] > 3:
            ratios.setdefault(p["pos"], []).append(p["fc"] / p["ros"] ** 1.5)
    med = {pos: sorted(v)[len(v) // 2] for pos, v in ratios.items() if v}
    overall = sorted(sum(ratios.values(), []))
    fallback = overall[len(overall) // 2] if overall else 60
    for p in all_players:
        if p["fc"]:
            p["value"] = int(p["fc"] * ROS_MULT.get(p["status"], 1))
        else:
            k = med.get(p["pos"], fallback * (0.25 if p["pos"] in ("K", "D/ST") else 1))
            p["value"] = int(k * max(p["ros"], 0) ** 1.5)


# ---------------------------------------------------------------- protections
def load_keepers():
    try:
        with open(KEEPERS_FILE) as f:
            return {norm(line) for line in f if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


def tag_protections(me, slots, keepers):
    """Mark handcuffs, keepers, and hot players so they are never suggested as drops."""
    rbs = sorted((p for p in me if p["pos"] == "RB"), key=lambda p: -p["value"])
    core_rbs = rbs[:3]
    for p in me:
        p["protect"] = None
        if norm(p["name"]) in keepers:
            p["protect"] = "Keeper list"
        elif p["pos"] == "RB" and any(c["id"] != p["id"] and c["nfl"] == p["nfl"]
                                      and c["value"] > p["value"] for c in core_rbs):
            starter = next(c for c in core_rbs if c["nfl"] == p["nfl"] and c["id"] != p["id"])
            p["protect"] = f"Handcuff to {starter['name']}"
        elif p["last"] >= HOT_GAME:
            p["protect"] = f"Scored {p['last']} last game"


def keep_score(p):
    """Value adjusted for recent form, so a hot player isn't cut on stale market value."""
    base = max(p["ros"], 5)
    form = max(-0.3, min(0.5, (p["recent"] - base) / base)) if p["games"] else 0
    return p["value"] * (1 + form)


def drop_candidates(me, slots):
    starters = {p["id"] for _, p in optimal_lineup(me, slots, "ros")[1]}
    bench = [p for p in me if p["id"] not in starters and p["slot"] != "IR" and not p.get("protect")]
    return sorted(bench, key=keep_score)


# ---------------------------------------------------------------- recommendations
def lineup_moves(me, slots):
    pool = [p for p in me if p["slot"] != "IR"]
    best_total, best = optimal_lineup(pool, slots, "week")
    best_ids = {p["id"] for _, p in best}
    current = [p for p in pool if p["slot"] not in ("BE", "IR", "")]
    cur_ids = {p["id"] for p in current}
    cur_total = sum(p["week"] for p in current)
    start = sorted([p for _, p in best if p["id"] not in cur_ids], key=lambda p: -p["week"])
    sit = sorted([p for p in current if p["id"] not in best_ids], key=lambda p: p["week"])
    moves = []
    for a, b in itertools.zip_longest(start, sit):
        moves.append({"start": a, "sit": b})
    alerts = [p for p in current if p["status"] in WEEK_OUT or p["bye"]
              or p["status"] in ("QUESTIONABLE", "DOUBTFUL")]
    ir_eligible = [p for p in me if p["slot"] != "IR" and p["pos"] in SKILL
                   and p["status"] in ("INJURY_RESERVE", "IR")]
    ir_ids = {p["id"] for p in ir_eligible}
    alerts = [p for p in alerts if p["id"] not in ir_ids]
    display = list(SLOT_ELIG)
    best.sort(key=lambda sp: display.index(sp[0]) if sp[0] in display else 99)
    return {"optimal": [{**p, "slot": s} for s, p in best],
            "gain": round(best_total - cur_total, 1), "moves": moves,
            "alerts": alerts, "ir_candidates": ir_eligible}


def waiver_targets(me, fas, slots):
    drops = drop_candidates(me, slots)
    drop = drops[0] if drops else None
    base_ros = lineup_score(me, slots, "ros")
    base_wk = lineup_score([p for p in me if p["slot"] != "IR"], slots, "week")
    out = []
    for fa in fas:
        if fa["status"] in WEEK_OUT and fa["ros"] < 8:
            continue
        drop = drops[0] if drops else None
        if fa["pos"] in ("K", "D/ST"):   # streaming a K or D/ST: swap out your current one
            same = sorted((p for p in me if p["pos"] == fa["pos"] and not p.get("protect")
                           and not p["locked"]), key=lambda p: p["week"])
            if same:
                drop = same[0]
        new = [p for p in me if not drop or p["id"] != drop["id"]] + [fa]
        ros_gain = lineup_score(new, slots, "ros") - base_ros
        wk_gain = lineup_score([p for p in new if p["slot"] != "IR"], slots, "week") - base_wk
        if drop and fa["value"] <= drop["value"] and ros_gain < 0.3 and wk_gain < 2:
            continue
        if ros_gain >= 0.3 or wk_gain >= 2 or (fa["adds48"] > 5000 and fa["value"] > (drop or {}).get("value", 0)):
            kind = "Stream this week" if ros_gain < 0.3 else "Add"
            out.append({"player": fa, "drop": drop, "alt_drops": drops[1:3], "ros_gain": round(ros_gain, 2),
                        "week_gain": round(wk_gain, 1), "kind": kind,
                        "score": ros_gain + 0.3 * wk_gain + fa["adds48"] / 50000})
    out.sort(key=lambda x: -x["score"])
    return out[:12]


def trade_ideas(me_team, teams, slots):
    me = me_team["players"]
    mine = [p for p in sorted(me, key=lambda p: -p["value"]) if p["pos"] in SKILL][:TRADE_POOL]
    ideas = []
    for t in teams:
        if t["id"] == me_team["id"]:
            continue
        theirs = [p for p in sorted(t["players"], key=lambda p: -p["value"]) if p["pos"] in SKILL][:TRADE_POOL]
        shapes = [(1, 1), (2, 1), (1, 2)]
        found = []
        for ns, ng in shapes:
            for send in itertools.combinations(mine, ns):
                for get in itertools.combinations(theirs, ng):
                    sids = {p["id"] for p in send}
                    gids = {p["id"] for p in get}
                    e = evaluate_trade(me, t["players"], sids, gids, slots)
                    fair = e["v_send"] >= 0.9 * e["v_get"]       # they get fair market value
                    they_ok = e["b_lineup"] >= -0.25 or (e["b_lineup"] >= -1.0 and e["a_value"] < 0)
                    v = verdict(e["a_lineup"], e["a_value"], e["v_send"])
                    if e["a_lineup"] >= 0.5 and fair and they_ok and v in ("Accept", "Lean accept"):
                        found.append({"partner": t["name"], "partner_id": t["id"],
                                      "send": list(send), "get": list(get), **e, "verdict": v,
                                      "score": e["a_lineup"] + 0.5 * max(e["b_lineup"], 0)})
        found.sort(key=lambda x: -x["score"])
        ideas += found[:MAX_TRADES_PER_TEAM]
    ideas.sort(key=lambda x: -x["score"])
    return ideas[:MAX_TRADES]


# ---------------------------------------------------------------- history
def load_history():
    try:
        with open(HIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {"values": {}, "teams": {}}


def update_history(hist, players, today):
    hist.setdefault("values", {})[today] = {p["id"]: p["value"] for p in players}
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    hist["values"] = {d: v for d, v in hist["values"].items() if d >= cutoff}
    changes = []
    teams = hist.setdefault("teams", {})
    for p in players:
        old = teams.get(p["id"])
        if p["nfl"] and p["nfl"] != "None":
            if old and old != "None" and old != p["nfl"] and p["pos"] in SKILL:
                changes.append({"player": p, "from": old, "to": p["nfl"]})
            teams[p["id"]] = p["nfl"]
    return changes


def movers(hist, players, today):
    week_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    dates = sorted(d for d in hist["values"] if d < today)
    if not dates:
        return []
    ref_date = max([d for d in dates if d <= week_ago] or [dates[0]])
    ref = hist["values"][ref_date]
    rows = []
    for p in players:
        old = ref.get(p["id"])
        if old and old > 300 and p["pos"] in SKILL:
            pct = (p["value"] - old) / old
            if abs(pct) >= 0.08:
                rows.append({"player": p, "old": old, "new": p["value"], "pct": round(pct * 100), "since": ref_date})
    rows.sort(key=lambda r: -abs(r["pct"]))
    return rows[:20]


def situation_changes(changes, players):
    group = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR", "TE"}, "TE": {"WR", "TE"}}
    out = []
    for c in changes:
        p = c["player"]
        hurt = [q["name"] for q in players if q["nfl"] == c["to"] and q["id"] != p["id"]
                and q["pos"] in group.get(p["pos"], set()) and q["owner"] != "FA"]
        helped = [q["name"] for q in players if q["nfl"] == c["from"]
                  and q["pos"] in group.get(p["pos"], set()) and q["owner"] != "FA"]
        out.append({"player": p["name"], "pos": p["pos"], "from": c["from"], "to": c["to"],
                    "new_teammates": hurt[:8], "old_teammates": helped[:8]})
    return out


# ---------------------------------------------------------------- snapshot
def fmt(p):
    bits = [f"{p['name']} ({p['pos']}, {p['nfl']})", f"wk {p['week']}", f"ROS {p['ros']}/g",
            f"val {p['value']}"]
    if p["status"] not in ("ACTIVE", "NORMAL"):
        bits.append(f"STATUS {p['status']}" + (f" - {p['injury']}" if p["injury"] else ""))
    if p.get("games"):
        bits.append("last games " + "/".join(str(g) for g in p["games"]))
    if p["bye"]:
        bits.append("BYE")
    if p.get("protect"):
        bits.append(f"PROTECTED: {p['protect']}")
    return " | ".join(bits)


def write_snapshot(d):
    L = []
    m = d["meta"]
    L.append(f"# Fantasy Front Office briefing: {m['team']}")
    L.append(f"League: {m['league']} | Week {m['week']} | Updated {m['updated']} UTC")
    L.append(f"Scoring: {m['scoring']} | Starting slots: " +
             ", ".join(f"{k} x{v}" for k, v in d["slots"].items()) + f" | Bench {m['bench']}")
    L.append("Values: 'val' = market trade value (FantasyCalc, injury-adjusted). "
             "'ROS' = projected points per game rest of season. 'wk' = this week's projection.\n")
    if d["matchup"]:
        mu = d["matchup"]
        L.append(f"## This week's matchup\nvs {mu['opponent']}: projected {mu['my_proj']} to {mu['opp_proj']}\n")
    L.append("## Standings")
    for t in sorted(d["teams"], key=lambda t: t["standing"] or 99):
        L.append(f"{t['standing']}. {t['name']} {t['record']} (PF {t['pf']})")
    lm = d["lineup"]
    L.append(f"\n## Lineup recommendations (gain {lm['gain']} pts)")
    for mv in lm["moves"]:
        s, b = mv["start"], mv["sit"]
        L.append(f"- Start {s['name'] if s else '-'} ({s['week'] if s else ''}) "
                 f"over {b['name'] if b else '-'} ({b['week'] if b else ''})")
    for p in lm["alerts"]:
        L.append(f"- Alert: {fmt(p)}")
    for p in lm["ir_candidates"]:
        L.append(f"- IR candidate: {p['name']} ({p['status']})")
    L.append("\n## Waiver targets")
    for w in d["waivers"]:
        L.append(f"- {w['kind']}: {fmt(w['player'])} | adds 48h {w['player']['adds48']} | "
                 f"drop {w['drop']['name'] if w['drop'] else 'n/a'} | ROS gain {w['ros_gain']} | wk gain {w['week_gain']}")
    L.append("\n## Trade ideas (engine-generated)")
    for t in d["trades"]:
        L.append(f"- [{t['verdict']}] with {t['partner']}: send {', '.join(p['name'] for p in t['send'])} "
                 f"for {', '.join(p['name'] for p in t['get'])} | my lineup {t['a_lineup']:+} ppg | "
                 f"their lineup {t['b_lineup']:+} ppg | value {t['a_value']:+}")
    if d["situations"]:
        L.append("\n## NFL team changes detected")
        for s in d["situations"]:
            L.append(f"- {s['player']} ({s['pos']}) {s['from']} -> {s['to']}. New teammates affected: "
                     f"{', '.join(s['new_teammates']) or 'none rostered'}. Old teammates: "
                     f"{', '.join(s['old_teammates']) or 'none rostered'}")
    if d["movers"]:
        L.append("\n## Value movers (rostered players)")
        for r in d["movers"]:
            L.append(f"- {r['player']['name']} ({r['player']['owner']}): {r['old']} -> {r['new']} ({r['pct']:+}%) since {r['since']}")
    L.append("\n## All rosters")
    for t in d["teams"]:
        L.append(f"\n### {t['name']} ({t['record']})")
        for p in sorted(t["players"], key=lambda p: -p["value"]):
            L.append(f"- [{p['slot']}] {fmt(p)}")
    L.append("\n## Top free agents")
    for p in sorted(d["fas"], key=lambda p: -p["value"])[:40]:
        L.append(f"- {fmt(p)} | owned {p['owned']}% | adds 48h {p['adds48']}")
    with open(os.path.join(OUT_DIR, "snapshot.md"), "w") as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------- main
def main():
    league_id = os.environ.get("LEAGUE_ID")
    if not league_id:
        sys.exit("LEAGUE_ID secret is missing. Add it under Settings > Secrets and variables > Actions.")
    now = dt.datetime.now(dt.timezone.utc)
    year = int(os.environ.get("SEASON") or season_year(now))
    log(f"Pulling ESPN league {league_id}, season {year}")
    espn = fetch_espn(league_id, year, os.environ.get("ESPN_S2"), os.environ.get("SWID"))
    week, slots = espn["week"], espn["slots"]
    superflex = slots.get("OP", 0) > 0
    ppr = espn["ppr"] if espn["ppr"] in (0, 0.5, 1) else 1
    log("Pulling Sleeper and FantasyCalc")
    sleeper = fetch_sleeper()
    fc_espn, fc_name = fetch_fantasycalc(espn["team_count"], ppr, superflex)

    teams, all_players, seen = [], [], set()
    for t in espn["teams"]:
        players = [build_player(p, week, fc_espn, fc_name, sleeper, t["name"],
                                espn["weekly"].get(str(p.playerId))) for p in t["raw"]]
        seen.update(p["id"] for p in players)
        all_players += players
        teams.append({k: v for k, v in t.items() if k != "raw"} | {"players": players})
    fas = []
    for p in espn["fas"]:
        if str(p.playerId) in seen:
            continue
        seen.add(str(p.playerId))
        fas.append(build_player(p, week, fc_espn, fc_name, sleeper, "FA"))
    fill_values(all_players + fas)

    me_team = next((t for t in teams if t["name"].strip().lower() == MY_TEAM.strip().lower()), None)
    if not me_team:
        sys.exit(f"Team '{MY_TEAM}' not found. Teams in league: {[t['name'] for t in teams]}")

    tag_protections(me_team["players"], slots, load_keepers())
    today = now.date().isoformat()
    hist = load_history()
    changes = update_history(hist, all_players + fas, today)
    data = {
        "meta": {"league": espn["name"], "team": me_team["name"], "team_id": me_team["id"],
                 "week": week, "updated": now.strftime("%Y-%m-%d %H:%M"),
                 "scoring": {0: "Standard", 0.5: "Half PPR", 1: "PPR"}.get(ppr, f"{ppr} PPR"),
                 "bench": espn["bench"], "superflex": superflex},
        "slots": slots, "matchup": espn["matchup"], "teams": teams, "fas": fas,
        "lineup": lineup_moves(me_team["players"], slots),
        "waivers": waiver_targets(me_team["players"], fas, slots),
        "trades": trade_ideas(me_team, teams, slots),
        "movers": movers(hist, all_players, today),
        "situations": situation_changes(changes, all_players + fas),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
    with open(os.path.join(OUT_DIR, "data.json"), "w") as f:
        json.dump(data, f, default=str)
    with open(HIST_FILE, "w") as f:
        json.dump(hist, f)
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()
    write_snapshot(data)
    log(f"Done: {len(teams)} teams, {len(fas)} free agents, {len(data['trades'])} trade ideas, "
        f"{len(data['waivers'])} waiver targets")


if __name__ == "__main__":
    main()
