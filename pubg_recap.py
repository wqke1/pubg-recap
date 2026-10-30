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
    rosters = [i for i in d["included"] if i["type"] == "roster"]
    squad_kills = {r["id"]: sum(parts[p["id"]]["kills"] for p in r["relationships"]["participants"]["data"] if p["id"] in parts)
                   for r in rosters}
    for r in rosters:
        members = [parts[p["id"]] for p in r["relationships"]["participants"]["data"] if p["id"] in parts]
        if len({alias(s["name"]) for s in members} & CIRCLE) < 2:
            continue
        created = int(datetime.fromisoformat(a["createdAt"].replace("Z", "+00:00")).timestamp())
        others = [k for rid, k in squad_kills.items() if rid != r["id"]]
        return {
            "top_kills": squad_kills[r["id"]] > max(others, default=0),  # plus de kills que toutes les autres squads
            "max_other_kills": max(others, default=0),
            "id": d["data"]["id"],
            "created_at": created,
            "duration": a["duration"],
            "map": MAPS.get(a["mapName"], a["mapName"]),
            "rank": r["attributes"]["stats"]["rank"],
            "telemetry": next((i["attributes"]["URL"] for i in d["included"] if i["type"] == "asset"), None),
            "players": [{
                "name": s["name"],
                "account": s["playerId"],
                "kills": s["kills"], "assists": s["assists"], "damage": s["damageDealt"],
                "hs": s["headshotKills"], "survived": s["timeSurvived"], "revives": s["revives"],
                "longest": s["longestKill"], "alive": s["deathType"] == "alive",
                "dbnos": s["DBNOs"], "heals": s["heals"], "boosts": s["boosts"], "vehicles": s["vehicleDestroys"],
                "walk": s["walkDistance"], "ride": s["rideDistance"], "swim": s["swimDistance"],
            } for s in members],
        }
    return None


# ---------------------------------------------------------------- Télémétrie (stats détaillées par match)

WEAPON_CLASS = {
    "AR": {"AK47", "M16A4", "M416", "HK416", "SCAR-L", "G36C", "QBZ95", "BerylM762", "Groza", "AUG", "Mk47Mutant", "K2", "ACE32", "FAMASG2", "FAMAS"},
    "DMR": {"Mini14", "SKS", "Mk14", "SLR", "QBU88", "VSS", "Mk12", "Dragunov"},
    "SR": {"Kar98k", "M24", "AWM", "MosinNagant", "Win1894", "L6"},
    "SMG": {"UMP", "Vector", "UZI", "Thompson", "MP5K", "BizonPP19", "P90", "JS9", "MP9"},
    "LMG": {"DP28", "M249", "MG3"},
    "SG": {"Saiga12", "Berreta686", "Winchester", "DP12", "O12"},
}


def weapon_class(causer):
    w = causer.replace("Weap", "").replace("_C", "")
    for cls, names in WEAPON_CLASS.items():
        if w in names:
            return cls
    return "other"


TELE_KEYS = ("dmg_ar", "dmg_dmr", "dmg_sr", "dmg_smg", "dmg_lmg", "dmg_sg", "dmg_other", "dmg_gren", "dmg_molo",
             "taken", "taken_zone", "stolen", "lost", "knocked", "revived", "gren", "smoke", "flash", "molo")


def telemetry_stats(url, accounts):
    """Stats télémétrie pour les comptes donnés (set d'account ids d'un même roster). Retourne {account: {...}}."""
    r = requests.get(url, headers={"Accept-Encoding": "gzip"}, timeout=120)
    r.raise_for_status()
    events = r.json()
    st = {a: dict.fromkeys(TELE_KEYS, 0) for a in accounts}

    def acc(c):
        return (c or {}).get("accountId")

    for ev in events:
        t = ev.get("_T")
        if t == "LogPlayerTakeDamage":
            a, v, dmg = acc(ev.get("attacker")), acc(ev.get("victim")), ev.get("damage", 0)
            cat = ev.get("damageTypeCategory", "")
            if a in st and v not in st and cat != "Damage_Groggy":  # on exclut les dégâts sur un joueur déjà au sol
                if cat == "Damage_Gun":
                    st[a]["dmg_" + weapon_class(ev.get("damageCauserName", "")).lower()] += dmg
                elif cat == "Damage_Explosion_Grenade":
                    st[a]["dmg_gren"] += dmg
                elif cat == "Damage_Molotov":
                    st[a]["dmg_molo"] += dmg
            if v in st:
                if cat == "Damage_BlueZone":
                    st[v]["taken_zone"] += dmg
                elif a and a not in st:
                    st[v]["taken"] += dmg
        elif t == "LogPlayerMakeGroggy":
            v = acc(ev.get("victim"))
            if v in st:
                st[v]["knocked"] += 1
        elif t == "LogPlayerKillV2":
            f, m = acc(ev.get("finisher")), acc(ev.get("dBNOMaker"))
            if f in st and m in st and f != m:
                st[f]["stolen"] += 1
                st[m]["lost"] += 1
        elif t == "LogPlayerRevive":
            v = acc(ev.get("victim"))
            if v in st:
                st[v]["revived"] += 1
        elif t == "LogPlayerUseThrowable":
            a = acc(ev.get("attacker"))
            if a in st:
                item = (ev.get("weapon") or {}).get("itemId", "")
                key = "gren" if "Grenade" in item else "smoke" if "Smoke" in item else "flash" if "Flash" in item else "molo" if "Molotov" in item else None
                if key:
                    st[a][key] += 1
    return {a: {k: round(v, 1) if isinstance(v, float) else v for k, v in d.items()} for a, d in st.items()}


def enrich_telemetry(m):
    """Ajoute p["tele"] à chaque joueur du match (si l'URL de télémétrie est connue)."""
    if not m.get("telemetry"):
        return False
    try:
        st = telemetry_stats(m["telemetry"], {p["account"] for p in m["players"]})
    except Exception as e:
        print("  télémétrie :", repr(e))
        return False
    for p in m["players"]:
        p["tele"] = st.get(p["account"])
    return True


def backfill_telemetry(days=7):
    """Complète la télémétrie des matchs récents stockés sans (une fois)."""
    since = time.time() - days * 86400
    rows = [(i, json.loads(d)) for i, d in DB.execute("SELECT id, data FROM matches WHERE created_at > ?", (since,))]
    todo = [(i, m) for i, m in rows if any("tele" not in p for p in m["players"])]
    print(f"télémétrie : {len(todo)} matchs à compléter")
    for k, (mid, m) in enumerate(todo, 1):
        if not m.get("telemetry"):
            try:
                d = api(f"/matches/{mid}")
                m["telemetry"] = next((i["attributes"]["URL"] for i in d["included"] if i["type"] == "asset"), None)
                parts = {i["attributes"]["stats"]["playerId"]: i["attributes"]["stats"] for i in d["included"] if i["type"] == "participant"}
                for p in m["players"]:
                    s = parts.get(p["account"], {})
                    p.setdefault("dbnos", s.get("DBNOs", p["kills"])); p.setdefault("heals", s.get("heals", 0)); p.setdefault("boosts", s.get("boosts", 0))
                    p.setdefault("vehicles", s.get("vehicleDestroys", 0)); p.setdefault("walk", s.get("walkDistance", 0)); p.setdefault("ride", s.get("rideDistance", 0)); p.setdefault("swim", s.get("swimDistance", 0))
            except Exception as e:
                print("  ", mid[:8], repr(e)); continue
        if enrich_telemetry(m):
            DB.execute("UPDATE matches SET data=? WHERE id=?", (json.dumps(m), mid))
            DB.commit()
            print(f"  {k}/{len(todo)} ok")


def poll():
    ids = [m for m in recent_match_ids() if not DB.execute("SELECT 1 FROM seen WHERE id=?", (m,)).fetchone()]
    print(f"{len(ids)} nouveaux matchs à analyser...")
    for i, mid in enumerate(ids, 1):
        m = parse_match(api(f"/matches/{mid}"))  # /matches ne compte pas dans le rate limit
        print(f"  {i}/{len(ids)}", "ranked ✓" if m else "ignoré")
        if m:
            enrich_telemetry(m)
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
                               "survived": 0, "revives": 0, "longest": 0.0, "first_death": 0, "account": None,
                               "dbnos": 0, "heals": 0, "boosts": 0, "vehicles": 0, "dist": 0.0,
                               "death_order": [0, 0, 0, 0], "tele_games": 0, "tele": dict.fromkeys(TELE_KEYS, 0)})
    for m in group:
        dead = sorted((p for p in m["players"] if not p["alive"]), key=lambda p: p["survived"])
        first = alias(dead[0]["name"]) if dead else None
        order = {p["account"]: i for i, p in enumerate(dead[:4])}
        for p in m["players"]:
            s = agg[alias(p["name"])]
            s["games"] += 1
            for k in ("kills", "assists", "damage", "hs", "survived", "revives"):
                s[k] += p[k]
            for k in ("dbnos", "heals", "boosts", "vehicles"):
                s[k] += p.get(k, 0)
            s["dist"] += p.get("walk", 0) + p.get("ride", 0) + p.get("swim", 0)
            s["longest"] = max(s["longest"], p["longest"])
            s["account"] = p["account"]
            if alias(p["name"]) == first:
                s["first_death"] += 1
            if p["account"] in order:
                s["death_order"][order[p["account"]]] += 1
            if p.get("tele"):
                s["tele_games"] += 1
                for k in TELE_KEYS:
                    s["tele"][k] += p["tele"].get(k, 0)
    return dict(sorted(agg.items(), key=lambda kv: -kv[1]["games"]))


def fmt_time(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def fmt_dt(ts):
    d = datetime.fromtimestamp(ts)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}, {d:%Hh%M}"


def split_randoms(agg, n):
    """Hors cercle et moins de la moitié des parties → random. Retourne (joueurs, randoms)."""
    players, randoms = {}, {}
    for name, s in agg.items():
        if name in CIRCLE or s["games"] * 2 >= n:
            players[name] = s
        else:
            randoms[name] = s
    return players, randoms


# Rating par joueur (1.00 = joueur moyen en ranked squad). Poids et valeurs de référence par partie.
RATING_W = {"kills": 0.41, "assists": 0.18, "damage": 0.41}
RATING_REF = {"kills": 2.0, "assists": 1.5, "damage": 300.0, "survived": 15 * 60}


def rating(st, n=None):
    """Moyenne par partie ; si le joueur n'a pas fait toutes les parties (n), on le ramène vers 1.00
    au prorata de sa présence (3 bonnes games ne valent pas 6)."""
    r = sum(RATING_W[k] * (st[k] / st["games"]) / RATING_REF[k] for k in RATING_W)
    if n:
        r = 1 + (r - 1) * st["games"] / n
    return r


def rating_color(r):
    return "#57f287" if r >= 1.15 else "#f2a900" if r >= 0.85 else "#ed4245"


TIER_COLORS = {"Bronze": "#b0703a", "Silver": "#9aa5b1", "Gold": "#e3b341", "Platinum": "#4fd1c5",
               "Diamond": "#7aa2ff", "Master": "#d16bff", "Lunar": "#ffb3ec", "Survivor": "#ff6b6b"}


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def recap_html(group, prev_group, rp_now, rp_prev):
    agg_all = aggregate(group)
    n = len(group)
    agg, randoms = split_randoms(agg_all, n)
    start = group[0]["created_at"]
    end = group[-1]["created_at"] + group[-1]["duration"]
    dur = sum(m["duration"] for m in group)
    ranks = [m["rank"] for m in group]
    wins = sum(r == 1 for r in ranks)
    top10 = sum(r <= 10 for r in ranks)
    first_death = max(agg.items(), key=lambda kv: kv[1]["first_death"])
    max_dmg = max(v["damage"] / v["games"] for v in agg.values()) or 1

    rp_by_alias = defaultdict(list)
    for acc, raw, r in rp_now:
        if r and alias(raw) not in randoms:
            delta = r["rp"] - rp_prev[acc] if acc in rp_prev else None
            rp_by_alias[alias(raw)].append((raw, r["tier"], r["rp"], delta))

    def badge(tier, rp, delta, raw, name):
        col = TIER_COLORS.get(tier.split()[0], "#aaa")
        d = "" if delta is None else f'<span class="delta {"up" if delta >= 0 else "down"}">{delta:+d}</span>'
        who = f'<span class="acc">{esc(raw)}</span>' if raw != name else ""
        return f'<div class="badge" style="--c:{col}"><span class="dot"></span>{esc(tier)}<b>{rp} RP</b>{d}{who}</div>'

    rows = ""
    ranked_players = sorted(agg.items(), key=lambda kv: -rating(kv[1], n))
    for pos, (name, st) in enumerate(ranked_players, 1):
        hs = f"{100 * st['hs'] / st['kills']:.0f}%" if st["kills"] else "–"
        dpp = st["damage"] / st["games"]
        badges = "".join(badge(t, rp, d, raw, name) for raw, t, rp, d in rp_by_alias.get(name, []))
        games = f'<span class="games">{st["games"]}/{n}</span>' if st["games"] < n else ""
        rt = rating(st, n)
        rows += f"""
        <div class="row">
          <div class="pos">{pos}</div>
          <div class="who"><div class="name">{esc(name)}{games}</div><div class="badges">{badges}</div></div>
          <div class="stat rating" style="--c:{rating_color(rt)}"><b>{rt:.2f}</b><i>rating</i></div>
          <div class="stat"><b>{st['kills']}</b><i>kills</i></div>
          <div class="stat"><b>{st['assists']}</b><i>assists</i></div>
          <div class="stat dmg"><b>{dpp:.0f}</b><i>dég/p</i><div class="bar"><div style="width:{100 * dpp / max_dmg:.0f}%"></div></div></div>
          <div class="stat"><b>{hs}</b><i>HS</i></div>
          <div class="stat"><b>{fmt_time(st['survived'] / st['games'])}</b><i>survie</i></div>
          <div class="stat"><b>{st['revives']}</b><i>revives</i></div>
        </div>"""
    if randoms:
        r = {k: sum(v[k] for v in randoms.values()) for k in ("games", "kills", "assists", "damage", "hs", "survived", "revives")}
        hs = f"{100 * r['hs'] / r['kills']:.0f}%" if r["kills"] else "–"
        rows += f"""
        <div class="row random">
          <div class="pos"></div>
          <div class="who"><div class="name">Randoms <span class="games">×{len(randoms)}</span></div></div>
          <div class="stat rating"><b>{rating(r):.2f}</b><i>rating</i></div>
          <div class="stat"><b>{r['kills']}</b><i>kills</i></div>
          <div class="stat"><b>{r['assists']}</b><i>assists</i></div>
          <div class="stat dmg"><b>{r['damage'] / r['games']:.0f}</b><i>dég/p</i></div>
          <div class="stat"><b>{hs}</b><i>HS</i></div>
          <div class="stat"><b>{fmt_time(r['survived'] / r['games'])}</b><i>survie</i></div>
          <div class="stat"><b>{r['revives']}</b><i>revives</i></div>
        </div>"""

    sniper = max(agg.items(), key=lambda kv: kv[1]["longest"])
    mvp = max(agg.items(), key=lambda kv: kv[1]["damage"] / kv[1]["games"])
    killer = max(agg.items(), key=lambda kv: kv[1]["kills"])
    medic = max(agg.items(), key=lambda kv: kv[1]["revives"])
    titles = [("💥", "Dégâts", mvp[0], f"{mvp[1]['damage'] / mvp[1]['games']:.0f} dég/p"),
              ("🔪", "Tueur", killer[0], f"{killer[1]['kills']} kills"),
              ("🎯", "Sniper", sniper[0], f"{sniper[1]['longest']:.0f} m"),
              ("🚑", "Médecin", medic[0], f"{medic[1]['revives']} revives"),
              ("💀", "Premier mort", first_death[0], f"{first_death[1]['first_death']}/{first_death[1]['games']}")]
    titles_html = "".join(f'<div class="title"><span class="ico">{i}</span><div><i>{t}</i><b>{esc(w)}</b><s>{v}</s></div></div>' for i, t, w, v in titles)

    prev_html = ""
    if prev_group:
        pa, _ = split_randoms(aggregate(prev_group), len(prev_group))
        common = [k for k in agg if k in pa]
        if common:
            def avg(a, key):
                return sum(a[k][key] / a[k]["games"] for k in common) / len(common)
            dmg = avg(agg, "damage") / avg(pa, "damage") - 1
            place = sum(m["rank"] for m in group) / n - sum(m["rank"] for m in prev_group) / len(prev_group)
            kills = avg(agg, "kills") - avg(pa, "kills")
            def tr(v, good_if_positive=True, fmt="{:+.1f}"):
                good = (v >= 0) == good_if_positive
                return f'<span class="{"up" if good else "down"}">{fmt.format(v)}</span>'
            prev_html = f'<div class="prev">vs session précédente &nbsp; dégâts {tr(dmg, True, "{:+.0%}")} · placement {tr(place, False)} · kills/p {tr(kills)}</div>'

    games_html = ""
    for i, m in enumerate(group, 1):
        k = sum(p["kills"] for p in m["players"])
        dmg = sum(p["damage"] for p in m["players"])
        top = '<span class="top">🔥</span>' if m.get("top_kills") else ""
        cls = "win" if m["rank"] == 1 else "good" if m["rank"] <= 10 else ""
        surv = max(p["survived"] for p in m["players"])
        games_html += f"""<div class="game {cls}"><span class="gi">{i}</span><span class="gt">{datetime.fromtimestamp(m['created_at']):%Hh%M}</span><span class="gm">{esc(m['map'])}</span>
            <span class="gs">{k} kills</span><span class="gs">{dmg:,.0f} dég</span><span class="gs">⏱ {fmt_time(surv)}</span>{top}<span class="gr">#{m['rank']}</span></div>""".replace(",", " ")
    names = ", ".join(agg.keys())

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #1e1f22; font-family: Inter, "Segoe UI", Roboto, sans-serif; color: #dbdee1; width: 960px; }}
    .card {{ padding: 28px 32px 22px; border-left: 6px solid #f2a900; }}
    h1 {{ margin: 0; font-size: 26px; font-weight: 800; color: #fff; letter-spacing: -.3px; }}
    .sub {{ color: #949ba4; font-size: 15px; margin-top: 4px; }}
    .kpis {{ display: flex; gap: 12px; margin: 20px 0 22px; }}
    .kpi {{ flex: 1; background: #2b2d31; border-radius: 10px; padding: 12px 14px; }}
    .kpi b {{ display: block; font-size: 24px; font-weight: 800; color: #fff; white-space: nowrap; }}
    .kpi i {{ font-style: normal; font-size: 12px; color: #949ba4; text-transform: uppercase; letter-spacing: .5px; }}
    .kpi.win b {{ color: #f2a900; }}
    .kpi small {{ font-size: 15px; font-weight: 600; color: #949ba4; }}
    .row {{ display: flex; align-items: center; gap: 10px; padding: 11px 12px; border-radius: 10px; }}
    .row:nth-child(odd) {{ background: #26282c; }}
    .row.random {{ opacity: .6; }}
    .pos {{ flex: 0 0 26px; font-size: 20px; font-weight: 800; color: #6d7079; text-align: center; }}
    .row:first-child .pos {{ color: #f2a900; }}
    .who {{ flex: 0 0 250px; }}
    .stat.rating b {{ color: var(--c, #949ba4); }}
    .name {{ font-size: 18px; font-weight: 800; color: #fff; }}
    .games {{ font-size: 12px; color: #949ba4; font-weight: 600; margin-left: 8px; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 5px; }}
    .badge {{ display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: var(--c);
              background: color-mix(in srgb, var(--c) 14%, transparent); border-radius: 999px; padding: 3px 9px 3px 7px; }}
    .badge .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--c); }}
    .badge b {{ color: #fff; font-weight: 700; }}
    .badge .acc {{ color: #949ba4; font-weight: 500; }}
    .delta.up {{ color: #57f287; }} .delta.down {{ color: #ed4245; }}
    .stat {{ flex: 1; text-align: center; }}
    .stat b {{ display: block; font-size: 19px; font-weight: 800; color: #fff; }}
    .stat i {{ font-style: normal; font-size: 11px; color: #949ba4; text-transform: uppercase; letter-spacing: .4px; }}
    .stat.dmg {{ flex: 1.4; }}
    .bar {{ height: 4px; background: #3a3c42; border-radius: 2px; margin: 6px 10px 0; overflow: hidden; }}
    .bar div {{ height: 100%; background: linear-gradient(90deg, #f2a900, #ff6b35); border-radius: 2px; }}
    h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #949ba4; margin: 22px 0 10px; }}
    .titles {{ display: flex; gap: 10px; }}
    .title {{ flex: 1; background: #2b2d31; border-radius: 10px; padding: 10px 12px; display: flex; gap: 10px; align-items: center; }}
    .title .ico {{ font-size: 22px; }}
    .title i {{ display: block; font-style: normal; font-size: 11px; color: #949ba4; text-transform: uppercase; }}
    .title b {{ display: block; font-size: 15px; color: #fff; }}
    .title s {{ display: block; text-decoration: none; font-size: 12px; color: #b5bac1; }}
    .prev {{ margin-top: 18px; font-size: 14px; color: #949ba4; }}
    .prev .up {{ color: #57f287; font-weight: 700; }} .prev .down {{ color: #ed4245; font-weight: 700; }}
    .games {{ display: grid; grid-template-columns: 1fr 1fr; grid-auto-flow: column; gap: 6px 12px; }}
    .gt {{ color: #6d7079; font-size: 12px; width: 40px; }}
    .game {{ display: flex; align-items: center; gap: 8px; background: #26282c; border-radius: 8px; padding: 7px 10px; font-size: 13px; border-left: 3px solid transparent; }}
    .game.good {{ border-left: 3px solid #4fd1c5; }}
    .game.win {{ border-left: 3px solid #f2a900; background: #2e2a1e; }}
    .gi {{ color: #6d7079; font-weight: 700; width: 16px; }}
    .gm {{ font-weight: 700; color: #fff; width: 64px; font-size: 14px; }}
    .gs {{ color: #b5bac1; width: 62px; white-space: nowrap; }}
    .top {{ margin-left: auto; font-size: 15px; }}
    .top + .gr {{ margin-left: 0; }}
    .gr {{ margin-left: auto; font-weight: 800; color: #fff; font-size: 15px; }}
    .game.win .gr {{ color: #f2a900; }}
    .foot {{ margin-top: 14px; font-size: 13px; color: #6d7079; display: flex; justify-content: space-between; }}
    </style></head><body><div class="card">
      <h1>Session ranked — {esc(fmt_dt(start))} → {datetime.fromtimestamp(end):%Hh%M}</h1>
      <div class="sub">{n} partie{'s' if n > 1 else ''} · {esc(names)} · {int(dur // 3600)}h{int(dur % 3600 // 60):02d} en game</div>
      <div class="kpis">
        <div class="kpi"><b>#{sum(ranks) / n:.1f}</b><i>placement</i></div>
        <div class="kpi{' win' if wins else ''}"><b>{'🏆 ' if wins else ''}#{min(ranks)}{f' <small>×{wins}</small>' if wins > 1 else ''}</b><i>meilleur</i></div>
        <div class="kpi"><b>{top10}/{n}</b><i>top 10</i></div>
        <div class="kpi"><b>{sum(v['kills'] for v in agg_all.values())}</b><i>kills</i></div>
        <div class="kpi"><b>{f"{sum(v['damage'] for v in agg_all.values()):,.0f}".replace(",", " ")}</b><i>dégâts</i></div>
      </div>
      <div class="rows">{rows}</div>
      <h2>Titres</h2>
      <div class="titles">{titles_html}</div>
      {prev_html}
      <h2>Parties</h2>
      <div class="games" style="grid-template-rows: repeat({(n + 1) // 2}, auto)">{games_html}</div>
      <div class="foot"><span></span><span>mis à jour {datetime.now():%Hh%M}</span></div>
    </div></body></html>"""


def render_png(html, width=960):
    """HTML → PNG via Chromium headless (Playwright)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": 200}, device_scale_factor=2)
        pg.set_content(html, wait_until="networkidle")
        png = pg.screenshot(full_page=True)
        b.close()
    return png


def build_recap(group, prev_group, rp_now, rp_prev):
    """Retourne (embed, png_bytes)."""
    start = group[0]["created_at"]
    end = group[-1]["created_at"] + group[-1]["duration"]
    embed = {"title": f"🐔  Session ranked — {fmt_dt(start)} → {datetime.fromtimestamp(end):%Hh%M}",
             "color": 0xF2A900, "image": {"url": "attachment://recap.png"}}
    return embed, render_png(recap_html(group, prev_group, rp_now, rp_prev))


# ---------------------------------------------------------------- Discord (webhook)

def webhook_post(embed, png, fname="recap.png"):
    payload = {"embeds": [embed], "attachments": []}
    r = requests.post(WEBHOOK, params={"wait": "true"}, data={"payload_json": json.dumps(payload)},
                      files={"files[0]": (fname, png, "image/png")}, timeout=60)
    r.raise_for_status()
    return r.json()["id"]


def webhook_edit(message_id, embed, png, fname="recap.png"):
    payload = {"embeds": [embed], "attachments": []}  # attachments vide = on remplace l'ancienne image
    r = requests.patch(f"{WEBHOOK}/messages/{message_id}", data={"payload_json": json.dumps(payload)},
                       files={"files[0]": (fname, png, "image/png")}, timeout=60)
    if r.status_code == 404:  # message supprimé à la main → on reposte
        return webhook_post(embed, png, fname)
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
    # dernier RP connu de chaque compte, toutes sessions précédentes confondues
    rp_prev = {}
    for (rp_json,) in DB.execute("SELECT rp FROM sessions WHERE first_match!=? AND started_at<? ORDER BY started_at DESC",
                                 (key, group[0]["created_at"])):
        for acc, rp in (json.loads(rp_json) if rp_json else {}).items():
            rp_prev.setdefault(acc, rp)
    prev_group = next((g for g in reversed(groups[:idx]) if lineup(g[0]) == lineup(group[0])), None)
    embed, png = build_recap(group, prev_group, rp_now, rp_prev)

    msg_id = webhook_edit(row[0], embed, png) if row and row[0] else webhook_post(embed, png)
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
    if "--backfill" in sys.argv:
        backfill_telemetry()
    elif "--reports-test" in sys.argv:
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
