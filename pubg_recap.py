#!/usr/bin/env python3
"""Récap de session PUBG ranked (TPP squad) posté sur Discord via webhook.

- Toutes les `poll_minutes`, récupère les matchs récents du cercle.
- Garde uniquement les matchs ranked squad où >= 2 joueurs du cercle sont dans le même roster.
- Une session = une journée de jeu (bascule à 6h, `day_cutoff_hour`) × un lineup du cercle.
- `--recap` poste (ou met à jour) toutes les sessions de la journée.
- Un message par session, posté dès le premier match et édité après chaque match.

    python3 pubg_recap.py           # boucle (laisser tourner)
    python3 pubg_recap.py --recap   # force le recap de la session en cours maintenant
    python3 pubg_recap.py --once    # un seul passage (pour cron)
    python3 pubg_recap.py --reports # poste les nouveaux clips pubg.report (salon dédié)
    python3 pubg_recap.py --reports-test 6  # reposte les 6 derniers clips (test du rendu)
"""
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())
# Les secrets peuvent venir de l'environnement (GitHub Actions) plutôt que de config.json
for env, key in (("PUBG_API_KEY", "pubg_api_key"), ("DISCORD_WEBHOOK", "discord_webhook"),
                 ("DISCORD_WEBHOOK_REPORTS", "discord_webhook_reports")):
    if os.environ.get(env):
        CONFIG[key] = os.environ[env]
API = "https://api.pubg.com/shards/" + CONFIG.get("shard", "steam")
HEADERS = {"Authorization": f"Bearer {CONFIG['pubg_api_key']}", "Accept": "application/vnd.api+json"}
WEBHOOK = CONFIG["discord_webhook"].split("?")[0]
IDLE = CONFIG.get("idle_minutes", 45) * 60

DB = sqlite3.connect(Path(__file__).with_name("recap.db"))
DB.executescript("""
CREATE TABLE IF NOT EXISTS seen     (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS matches  (id TEXT PRIMARY KEY, created_at INTEGER, duration INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS sessions (first_match TEXT PRIMARY KEY, started_at INTEGER, message_id INTEGER, n_matches INTEGER, rp TEXT);
CREATE TABLE IF NOT EXISTS kv       (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS reports  (id TEXT PRIMARY KEY);
""")

MAPS = {
    "Baltic_Main": "Erangel", "Desert_Main": "Miramar", "Savage_Main": "Sanhok", "DihorOtok_Main": "Vikendi",
    "Tiger_Main": "Taego", "Kiki_Main": "Deston", "Summerland_Main": "Karakin", "Chimera_Main": "Paramo",
    "Heaven_Main": "Haven", "Neon_Main": "Rondo",
}
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def alias(name):
    return CONFIG.get("aliases", {}).get(name, name)


CIRCLE = {alias(n) for n in CONFIG["circle"]}


# ---------------------------------------------------------------- API PUBG

def api(path, **params):
    r = requests.get(API + path, headers=HEADERS, params=params, timeout=30)
    if r.status_code == 429:
        print("rate limit PUBG, pause 60s...")
        time.sleep(61)
        return api(path, **params)
    r.raise_for_status()
    return r.json()


def recent_match_ids():
    names = CONFIG["circle"]
    ids = set()
    for i in range(0, len(names), 10):  # 10 pseudos max par requête
        d = api("/players", **{"filter[playerNames]": ",".join(names[i:i + 10])})
        for p in d["data"]:
            ids.update(m["id"] for m in p["relationships"]["matches"]["data"])
    return ids


def parse_match(d):
    """Retourne le match simplifié si c'est un ranked squad avec >= 2 du cercle, sinon None."""
    a = d["data"]["attributes"]
    if a.get("matchType") != "competitive" or a.get("gameMode") != "squad":
        return None
    parts = {i["id"]: i["attributes"]["stats"] for i in d["included"] if i["type"] == "participant"}
    for r in (i for i in d["included"] if i["type"] == "roster"):
        members = [parts[p["id"]] for p in r["relationships"]["participants"]["data"] if p["id"] in parts]
        if len({alias(s["name"]) for s in members} & CIRCLE) < 2:
            continue
        created = int(datetime.fromisoformat(a["createdAt"].replace("Z", "+00:00")).timestamp())
        return {
            "id": d["data"]["id"],
            "created_at": created,
            "duration": a["duration"],
            "map": MAPS.get(a["mapName"], a["mapName"]),
            "rank": r["attributes"]["stats"]["rank"],
            "players": [{
                "name": s["name"],
                "account": s["playerId"],
                "kills": s["kills"], "assists": s["assists"], "damage": s["damageDealt"],
                "hs": s["headshotKills"], "survived": s["timeSurvived"], "revives": s["revives"],
                "longest": s["longestKill"], "alive": s["deathType"] == "alive",
            } for s in members],
        }
    return None


def poll():
    ids = [m for m in recent_match_ids() if not DB.execute("SELECT 1 FROM seen WHERE id=?", (m,)).fetchone()]
    print(f"{len(ids)} nouveaux matchs à analyser...")
    for i, mid in enumerate(ids, 1):
        m = parse_match(api(f"/matches/{mid}"))  # /matches ne compte pas dans le rate limit
        print(f"  {i}/{len(ids)}", "ranked ✓" if m else "ignoré")
        if m:
            DB.execute("INSERT OR IGNORE INTO matches VALUES (?,?,?,?)",
                       (m["id"], m["created_at"], m["duration"], json.dumps(m)))
        DB.execute("INSERT OR IGNORE INTO seen VALUES (?)", (mid,))
    DB.commit()


def current_season():
    row = DB.execute("SELECT v FROM kv WHERE k='season'").fetchone()
    if row:
        s = json.loads(row[0])
        if time.time() - s["t"] < 86400:
            return s["id"]
    d = api("/seasons")
    sid = next(x["id"] for x in d["data"] if x["attributes"]["isCurrentSeason"])
    DB.execute("INSERT OR REPLACE INTO kv VALUES ('season', ?)", (json.dumps({"id": sid, "t": time.time()}),))
    DB.commit()
    return sid


def ranked_stats(account):
    try:
        d = api(f"/players/{account}/seasons/{current_season()}/ranked")
        s = d["data"]["attributes"]["rankedGameModeStats"].get("squad")
        if not s:
            return None
        t = s["currentTier"]
        return {"tier": f"{t['tier']} {t['subTier']}".strip(), "rp": s["currentRankPoint"]}
    except Exception as e:
        print("ranked:", e)
        return None


# ---------------------------------------------------------------- Sessions

def lineup(m):
    return frozenset(alias(p["name"]) for p in m["players"] if alias(p["name"]) in CIRCLE)


def sessions():
    """Une session = une journée de jeu (bascule à `day_cutoff_hour`) × un lineup du cercle."""
    cutoff = CONFIG.get("day_cutoff_hour", 6) * 3600
    rows = [json.loads(r[0]) for r in DB.execute("SELECT data FROM matches ORDER BY created_at")]
    groups = {}
    for m in rows:
        day = datetime.fromtimestamp(m["created_at"] - cutoff).date()
        groups.setdefault((day, lineup(m)), []).append(m)
    return sorted(groups.values(), key=lambda g: g[0]["created_at"])


def aggregate(group):
    agg = defaultdict(lambda: {"games": 0, "kills": 0, "assists": 0, "damage": 0.0, "hs": 0,
                               "survived": 0, "revives": 0, "longest": 0.0, "first_death": 0, "account": None})
    for m in group:
        dead = [p for p in m["players"] if not p["alive"]]
        first = alias(min(dead, key=lambda p: p["survived"])["name"]) if dead else None
        for p in m["players"]:
            s = agg[alias(p["name"])]
            s["games"] += 1
            for k in ("kills", "assists", "damage", "hs", "survived", "revives"):
                s[k] += p[k]
            s["longest"] = max(s["longest"], p["longest"])
            s["account"] = p["account"]
            if alias(p["name"]) == first:
                s["first_death"] += 1
    return dict(sorted(agg.items(), key=lambda kv: -kv[1]["games"]))


def fmt_time(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def fmt_dt(ts):
    d = datetime.fromtimestamp(ts)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}, {d:%Hh%M}"


def build_recap(group, prev_group, rp_now, rp_prev):
    agg = aggregate(group)
    n = len(group)
    start = group[0]["created_at"]
    end = group[-1]["created_at"] + group[-1]["duration"]
    dur = sum(m["duration"] for m in group)  # temps réellement passé en partie

    header_names = ", ".join(f"{k} ({v['games']})" if v["games"] < n else k for k, v in agg.items())
    L = [f"{n} partie{'s' if n > 1 else ''} · {header_names} · {int(dur // 3600)}h{int(dur % 3600 // 60):02d} en game", ""]

    ranks = [m["rank"] for m in group]
    wins = sum(r == 1 for r in ranks)
    top10 = sum(r <= 10 for r in ranks)
    first_death = max(agg.items(), key=lambda kv: kv[1]["first_death"])
    L += ["SQUAD",
          f"Placement moyen   #{sum(ranks) / n:<6.1f} Meilleur   #{min(ranks)}" + (f" 🏆 ×{wins}" if wins else ""),
          f"Kills             {sum(v['kills'] for v in agg.values()):<7} Dégâts     {sum(v['damage'] for v in agg.values()):,.0f}".replace(",", " "),
          f"Top 10            {top10}/{n:<5} Mort 1er   {first_death[0]} ×{first_death[1]['first_death']}", ""]

    L += ["PAR JOUEUR", f"{'':12} Parties  K   A   Dég/p   HS%   Surv   Rev"]
    for name, s in agg.items():
        hs = f"{100 * s['hs'] / s['kills']:.0f}%" if s["kills"] else "–"
        L.append(f"{name:12} {s['games']:>5}  {s['kills']:>3} {s['assists']:>3}   {s['damage'] / s['games']:>5.0f}  {hs:>4}  {fmt_time(s['survived'] / s['games']):>5}  {s['revives']:>3}")
    L.append("")

    if any(r for _, _, r in rp_now):
        L.append("RANKED")
        for acc, raw, r in rp_now:
            if not r:
                continue
            delta = f"({r['rp'] - rp_prev[acc]:+d})" if acc in rp_prev else ""
            L.append(f"{raw:12} {r['tier']:<13} {r['rp']:>5} RP   {delta}")
        L.append("")

    sniper = max(agg.items(), key=lambda kv: kv[1]["longest"])
    mvp = max(agg.items(), key=lambda kv: kv[1]["damage"] / kv[1]["games"])
    killer = max(agg.items(), key=lambda kv: kv[1]["kills"])
    medic = max(agg.items(), key=lambda kv: kv[1]["revives"])
    L += ["TITRES",
          f"💥 MVP            {mvp[0]} — {mvp[1]['damage'] / mvp[1]['games']:.0f} dég/partie",
          f"🔪 Tueur          {killer[0]} — {killer[1]['kills']} kills",
          f"🎯 Sniper         {sniper[0]} — kill à {sniper[1]['longest']:.0f} m",
          f"🚑 Médecin        {medic[0]} — {medic[1]['revives']} revives",
          f"💀 Premier mort   {first_death[0]} — {first_death[1]['first_death']} fois sur {first_death[1]['games']}", ""]

    if prev_group:
        pa = aggregate(prev_group)
        common = [k for k in agg if k in pa]
        if common:
            def avg(a, key):
                return sum(a[k][key] / a[k]["games"] for k in common) / len(common)
            dmg = avg(agg, "damage") / avg(pa, "damage") - 1
            place = sum(m["rank"] for m in group) / n - sum(m["rank"] for m in prev_group) / len(prev_group)
            kills = avg(agg, "kills") - avg(pa, "kills")
            L += [f"vs session précédente : dégâts {dmg:+.0%} · placement {place:+.1f} · kills/p {kills:+.1f}", ""]

    maps = defaultdict(int)
    for m in group:
        maps[m["map"]] += 1
    L.append(" · ".join(f"{k} ×{v}" for k, v in sorted(maps.items(), key=lambda kv: -kv[1])))

    return {
        "title": f"🐔  Session ranked — {fmt_dt(start)} → {datetime.fromtimestamp(end):%Hh%M}",
        "description": "```\n" + "\n".join(L) + "\n```",
        "color": 0xF2A900,
    }


# ---------------------------------------------------------------- Discord (webhook)

def webhook_post(embed):
    r = requests.post(WEBHOOK, params={"wait": "true"}, json={"embeds": [embed]}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def webhook_edit(message_id, embed):
    r = requests.patch(f"{WEBHOOK}/messages/{message_id}", json={"embeds": [embed]}, timeout=30)
    if r.status_code == 404:  # message supprimé à la main → on reposte
        return webhook_post(embed)
    r.raise_for_status()
    return message_id


def publish(groups, idx, force=False):
    group = groups[idx]
    key = group[0]["id"]
    row = DB.execute("SELECT message_id, n_matches FROM sessions WHERE first_match=?", (key,)).fetchone()
    if not force and row and row[1] == len(group):
        return  # rien de nouveau

    accounts = {}  # account id -> pseudo brut, ordre d'apparition
    for m in group:
        for p in m["players"]:
            accounts.setdefault(p["account"], p["name"])
    rp_now = [(acc, raw, ranked_stats(acc)) for acc, raw in accounts.items()]
    prev = DB.execute("SELECT rp FROM sessions WHERE first_match!=? AND started_at<? ORDER BY started_at DESC LIMIT 1",
                      (key, group[0]["created_at"])).fetchone()
    rp_prev = json.loads(prev[0]) if prev and prev[0] else {}
    prev_group = next((g for g in reversed(groups[:idx]) if lineup(g[0]) == lineup(group[0])), None)
    embed = build_recap(group, prev_group, rp_now, rp_prev)

    msg_id = webhook_edit(row[0], embed) if row and row[0] else webhook_post(embed)
    rp_store = {acc: r["rp"] for acc, _, r in rp_now if r}
    DB.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
               (key, group[0]["created_at"], msg_id, len(group), json.dumps(rp_store)))
    DB.commit()
    print("recap", "édité" if row else "posté", f"({len(group)} matchs)")


# ---------------------------------------------------------------- PUBG Report (clips Twitch)

def weapon(dc):
    return dc.replace("Weap", "").replace("Proj", "").replace("_C", "") or "?"


def circle_accounts():
    """{account id: pseudo brut} pour les comptes du cercle vus dans les matchs stockés."""
    acc = {}
    for (d,) in DB.execute("SELECT data FROM matches"):
        for p in json.loads(d)["players"]:
            if alias(p["name"]) in CIRCLE:
                acc[p["account"]] = p["name"]
    return acc


def md(t):
    """Échappe les caractères Markdown de Discord (pseudos avec _ ou *)."""
    for c in "*_~`|":
        t = t.replace(c, "\\" + c)
    return t


def twitch_name(twitch_id):
    """Nom du streamer à partir de son ID Twitch (api.ivr.fi, public), mis en cache."""
    key = f"twitch:{twitch_id}"
    row = DB.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    if row:
        return row[0]
    try:
        r = requests.get("https://api.ivr.fi/v2/twitch/user", params={"id": twitch_id},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        name = r.json()[0]["login"]
    except Exception:
        return None
    DB.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, name))
    DB.commit()
    return name


def twitch_url(ev):
    h, m, s_ = ev["TimeDiff"].split(":")
    return f"https://www.twitch.tv/videos/{ev['VideoID'].lstrip('v')}?t={int(h)}h{int(m)}m{int(s_)}s"


def streamer_was_dead(match_id, events, ev):
    """Le streamer était-il déjà mort au moment de l'événement `ev` (donc en spectate) ?"""
    # 1. nom PUBG du streamer : dans un événement LogPlayer* du même match, c'est celui qui n'est pas du cercle
    streamer = None
    for e in events:
        if e["Event"].startswith("LogPlayer"):
            for n in (e["Killer"], e["Victim"]):
                if alias(n) not in CIRCLE:
                    streamer = n
    if not streamer:
        return False
    # 2. son temps de survie dans ce match (API PUBG, /matches n'est pas rate-limité), en cache
    key = f"death:{match_id}:{streamer}"
    row = DB.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    if row:
        death_ts = float(row[0])
    else:
        try:
            d = api(f"/matches/{match_id}")
            created = datetime.fromisoformat(d["data"]["attributes"]["createdAt"].replace("Z", "+00:00")).timestamp()
            st = next(i["attributes"]["stats"] for i in d["included"]
                      if i["type"] == "participant" and i["attributes"]["stats"]["name"] == streamer)
            death_ts = created + st["timeSurvived"] if st["deathType"] != "alive" else 1e12
        except Exception as e:
            print("streamer_was_dead:", repr(e))
            return False
        DB.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, str(death_ts)))
        DB.commit()
    ev_ts = datetime.fromisoformat(ev["TimeEvent"].replace("Z", "+00:00")).timestamp()
    return ev_ts > death_ts


def pubg_report(test=0):
    """Poste un message par nouveau clip pubg.report (streamers qui nous ont tués / qu'on a tués).
    test=N : reposte les N clips les plus récents même s'ils ont déjà été postés (pour tester le rendu)."""
    hook = CONFIG.get("discord_webhook_reports", WEBHOOK).split("?")[0]
    headers = {"User-Agent": "Mozilla/5.0"}
    clips = []
    for acc, name in circle_accounts().items():
        try:
            r = requests.get(f"https://api.pubg.report/v1/players/{acc}/streams", headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json() or {}
        except Exception as e:
            print("pubg.report:", name, repr(e))
            continue
        for match_id, events in data.items():
            # un seul clip par (match, killer, victim) : on garde le knock (arme + distance) sinon la mort
            best = {}
            for ev in events:
                # LogPlayer* = le streamer est le tueur ou la victime.
                # LogTeammate* = c'est son coéquipier : on garde seulement si le streamer était déjà mort (il spectate).
                if ev["Event"].startswith("LogTeammate"):
                    if not streamer_was_dead(match_id, events, ev):
                        continue
                    ev["_spectate"] = True
                k = (ev["Killer"], ev["Victim"])
                if k not in best or ev["Distance"] > best[k]["Distance"]:
                    best[k] = ev
            for ev in best.values():
                rid = f"{match_id}/{ev['Killer']}/{ev['Victim']}"
                if test or not DB.execute("SELECT 1 FROM reports WHERE id=?", (rid,)).fetchone():
                    clips.append((match_id, rid, ev))
    clips.sort(key=lambda c: c[2]["TimeEvent"])
    if test:
        clips = clips[-test:]
    if not clips:
        print("pubg.report : rien de nouveau")
        return
    for match_id, rid, ev in clips:
        us_victim = alias(ev["Victim"]) in CIRCLE
        title = (f"💀  {ev['Victim']} tué par {ev['Killer']}" if us_victim
                 else f"🔫  {ev['Killer']} a tué {ev['Victim']}")
        if ev.get("_spectate"):
            title += "  (spectate)"
        when = datetime.fromisoformat(ev["TimeEvent"].replace("Z", "+00:00")).astimezone()
        arme = weapon(ev["DamageCauser"]) + (f" · {ev['Distance']} m" if ev["Distance"] else "") if ev["DamageCauser"] else "—"
        streamer = twitch_name(ev["TwitchID"])
        stream_line = (f"🎥 Streamer : [{md(streamer)}](https://www.twitch.tv/{streamer})\n" if streamer else "")
        desc = (f"**{md(arme)}**\n{MAPS.get(ev['Map'], ev['Map'])} · {fmt_dt(when.timestamp())}\n{stream_line}\n"
                f"[Voir sur pubg.report](https://pubg.report/streams/{match_id}/{ev['AttackID']}) · "
                f"[VOD Twitch]({twitch_url(ev)})")
        embed = {"title": md(title), "description": desc, "color": 0xE74C3C if us_victim else 0x2ECC71}
        r = requests.post(hook, json={"embeds": [embed]}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            r = requests.post(hook, json={"embeds": [embed]}, timeout=30)
        r.raise_for_status()
        DB.execute("INSERT OR IGNORE INTO reports VALUES (?)", (rid,))
        DB.commit()
        time.sleep(0.6)  # rate limit webhook Discord
    print(f"pubg.report : {len(clips)} clip(s) posté(s)")


def run_once(force=False):
    poll()
    groups = sessions()
    if not groups:
        print("Aucun match ranked trouvé.")
        return
    cutoff = CONFIG.get("day_cutoff_hour", 6) * 3600
    today = datetime.fromtimestamp(time.time() - cutoff).date()
    todays = [i for i, g in enumerate(groups)
              if datetime.fromtimestamp(g[0]["created_at"] - cutoff).date() == today]
    if not todays:
        todays = [len(groups) - 1]  # rien aujourd'hui → dernière session connue
    for i in todays:
        publish(groups, i, force=force)


if __name__ == "__main__":
    if "--reports-test" in sys.argv:
        i = sys.argv.index("--reports-test")
        n = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 3
        pubg_report(test=n)
    elif "--reports" in sys.argv:
        pubg_report()
    elif "--recap" in sys.argv:
        run_once(force=True)
    elif "--once" in sys.argv:
        run_once()
        pubg_report()
    else:
        while True:
            try:
                run_once()
            except Exception as e:
                print("erreur:", repr(e))
            time.sleep(CONFIG.get("poll_minutes", 5) * 60)
