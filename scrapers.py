"""Collects FM27 tactic test results from several sources.

Most sources have no public API, so each function reads HTML (or JSON where one exists, or the YouTube Data API)
and returns records normalised by rec(). Metrics are always stored as TOTALS
(matches, W/D/L, GF, GA, GD, points); the dashboard converts to per-game or per-38 itself.
"""
import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests
from lxml import etree, html

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
TIMEOUT = 30
FM_ARENA_MATCHES_PER_TEST = 450  # verified: "Patched Autumn" 5,400 matches = "12 test result(s)"

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


class Blocked(Exception):
    """The source blocks automated access (captcha / anti-bot)."""


class Skipped(Exception):
    """The source is not configured (for example a missing API key)."""


# Records of the previous update, keyed by (source, url). app.refresh() fills it so slow detail pages
# (FMAX formations, FMDelicious W/D/L) are only fetched again for tactics that are new or changed.
PREVIOUS = {}


def previous(source, url):
    return PREVIOUS.get((source, url)) or {}


# ---------- helpers ----------

def get_doc(url, retries=3):
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except requests.ConnectionError:  # server dropped the connection -> retry more slowly
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    time.sleep(0.3)  # space out requests so we do not hammer the server
    return html.fromstring(r.text)


def text(el):
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def num(s):
    """'5,400' -> 5400.0, '+36' -> 36.0, '59.2%' -> 59.2, '31K' -> 31000.0"""
    if not s:
        return None
    m = re.search(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*([KkMm](?![a-z]))?", s.replace("−", "-"))
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)


def parse_date(s, fmt):
    try:
        return datetime.strptime(s.strip(), fmt).isoformat()
    except (ValueError, AttributeError):
        return None


LINES = ("D", "DM", "M", "AM", "ST")


def _line(pos):
    p = pos.strip().upper()
    if not p or p.startswith("GK"):
        return None
    if p.startswith("WB"):
        return "D"  # FM naming convention: wing-backs count as defenders
    for pre in ("DM", "AM", "ST", "D", "M"):
        if p.startswith(pre):
            return pre
    return None


def formation_from_positions(positions):
    counts = dict.fromkeys(LINES, 0)
    for p in positions:
        if _line(p):
            counts[_line(p)] += 1
    if sum(counts.values()) != 10:
        return None
    return "-".join(str(counts[k]) for k in LINES if counts[k])


def positions_in(s):
    """'In possession: GK, DCL, DC' -> ['GK', 'DCL', 'DC']"""
    m = re.search(r"((?:[A-Z]{2,4},\s*)+[A-Z]{2,4})", s or "")
    return [p.strip() for p in m.group(1).split(",")] if m else []


_FORMATION_RE = re.compile(r"(?<![\w.])([2-5])-?([0-5])-?([0-5])(?:-?([0-5]))?(?:-?([0-5]))?(?![\w.])")


def formation_from_title(title):
    """Guess a formation from a title: '4-2-3-1', '433', '33211' (10 outfield players)."""
    for m in _FORMATION_RE.finditer(title or ""):
        digits = [int(g) for g in m.groups() if g is not None]
        if sum(digits) == 10 and all(digits[:-1]):
            return "-".join(str(d) for d in digits if d)
    return None


# ---------- Team tiers (Elite / Top / Sub-top / Underdog) ----------
# No source publishes a tier for the clubs testers pick, so this is OUR OWN rough grouping by real-world
# strength, written for the clubs seen so far. Unknown clubs get no tier. FM-Tactic-Hub's own tiers map
# onto Top / Sub-top / Underdog. Standard-test sources (FM-Arena, FMAX, metaFM) mix opponents: no tier.
# A manual.csv row may set its own tier in a `tier` column.
TIER_ORDER = ["Elite", "Top", "Sub-top", "Underdog"]
_TIER_CLUBS = {
    "Elite": ["R. Madrid", "Real Madrid", "Barcelona", "Manchester City", "Liverpool", "FC Bayern München", "Paris Saint-Germain",
              "Arsenal", "Chelsea", "Manchester United", "Juventus", "Inter", "Internazionale", "AC Milan", "Atlético de Madrid"],
    "Top": ["Tottenham Hotspur", "Newcastle United", "Borussia Dortmund", "RB Leipzig", "Bayer 04 Leverkusen", "Atalanta",
            "Associazione Sportiva Roma", "S.S. Lazio", "Sport Lisboa e Benfica", "Sporting Clube de Portugal",
            "Futebol Clube do Porto", "AFC Ajax", "Galatasaray A.Ş.", "Beşiktaş A.Ş.", "VfB Stuttgart", "ACF Fiorentina",
            "Bologna FC 1909", "Aston Villa", "Napoli"],
    "Sub-top": ["Brighton & Hove Albion", "Brentford", "Crystal Palace", "AFC Bournemouth", "Everton", "Leeds United",
                "Como 1907", "Genoa", "Parma Calcio 1913", "Sport-Club Freiburg", "SV Werder Bremen",
                "Sporting Clube de Braga", "LOSC", "Slavia Prague", "FK Partizan Beograd", "Alavés",
                "Leicester City", "Associazione Calcio Monza", "Sparta Rotterdam", "sc Heerenveen", "Stade de Reims",
                "Hertha BSC", "FC Schalke 04", "1. FC Köln", "AJ Auxerre", "FC Metz", "Ipswich Town", "Coventry City"],
    "Underdog": ["Hull City", "Kilmarnock", "Heart Of Midlothian", "Dunfermline Athletic", "Elgin City", "Stockport County",
                 "Pardubice", "IK Start", "K Sint-Truidense VV", "SpVgg Greuther Fürth", "1. FC Nürnberg",
                 "F.C. Hansa Rostock", "FC Energie Cottbus", "Rot-Weiss Essen", "Sportclub Verl", "SV Elversberg",
                 "SV Meppen", "TSV Alemannia Aachen", "TSV Havelse 1912", "US Saint-Malo", "União Sport Clube Paredes",
                 "Haymana Spor Kulübü", "Çorum Futbol Kulübü A.Ş.", "S. Gijón", "Paris FC", "London City Lionesses"],
}
_TIER_OF = {c.lower(): t for t, clubs in _TIER_CLUBS.items() for c in clubs}
_TIER_OF.update({"top teams (hub)": "Top", "mid-table (hub)": "Sub-top", "relegation (hub)": "Underdog"})


def tier_of(team):
    return _TIER_OF.get((team or "").strip().lower())


def rec(source, **kw):
    r = dict(source=source, tactic=None, url=None, author=None, patch=None, team=None, tester=None,
             formation_ip=None, formation_oop=None, positions_ip=None, positions_oop=None,
             formation_guessed=False, tests=None, matches=None, w=None, d=None, l=None,
             gf=None, ga=None, gd=None, points=None, downloads=None, views=None,
             published=None, note=None, origin="scrape", tier=None)
    r.update(kw)
    if not r["tier"]:
        r["tier"] = tier_of(r["team"])
    if r["gd"] is None and r["gf"] is not None and r["ga"] is not None:
        r["gd"] = r["gf"] - r["ga"]
    if r["points"] is None and r["w"] is not None and r["d"] is not None:
        r["points"] = 3 * r["w"] + r["d"]
    r["has_stats"] = bool(r["matches"])
    r["key"] = f"{source}|{r['url'] or r['tactic']}"
    return r


# ---------- 1. FM-Arena: standard test table per patch ----------

ARENA = "https://fm-arena.com"


def fm_arena():
    out = []
    versions = get_doc(f"{ARENA}/tactic-testing/versions/27/")
    for a in versions.xpath('//h3[contains(@class,"contentRow-title")]/a'):
        patch = text(a).replace("Patch", "").strip()  # "Patch 27.1.0" -> "27.1.0"
        url = urljoin(ARENA, a.get("href"))
        for _ in range(50):  # page cap to avoid an endless loop
            page = get_doc(url)
            for tr in page.xpath('//div[contains(@class,"fmtResultsTable")]//tr[starts-with(@id,"fmt-tactic-")]'):
                out.append(_arena_row(tr, patch))
            nxt = page.xpath('//a[contains(@class,"pageNav-jump--next")]/@href')
            if not nxt:
                break
            url = urljoin(ARENA, nxt[0])
    return out


def _arena_row(tr, patch):
    td = tr.xpath("./td")
    link = td[0].xpath(".//a")[-1]
    titles = td[1].xpath(".//@title")
    pos_ip = positions_in(next((t for t in titles if t.startswith("In possession")), ""))
    pos_oop = positions_in(next((t for t in titles if t.startswith("Out of possession")), ""))
    pld = num(text(td[3]))
    seasons = pld / 38  # FM-Arena publishes PTS/GF/GA/GD normalised to one 38-game season
    users = td[2].xpath(".//a")
    return rec(
        "fmarena",
        tactic=text(link), url=urljoin(ARENA, link.get("href")),
        author=text(users[-1]) if users else None, patch=patch,
        team="FM-Arena test league",
        formation_ip=formation_from_positions(pos_ip), formation_oop=formation_from_positions(pos_oop),
        positions_ip=pos_ip, positions_oop=pos_oop,
        tests=round(pld / FM_ARENA_MATCHES_PER_TEST), matches=pld,
        points=num(text(td[4]).split("/")[0]) * seasons,
        gd=num(text(td[5])) * seasons, gf=num(text(td[6])) * seasons, ga=num(text(td[7])) * seasons,
        downloads=num(text(td[11])),
        published=(td[12].xpath(".//time/@datetime") or [None])[0],
        note="Points/GF/GA converted from the published per-38 figures (rounded); FM-Arena does not publish W/D/L",
    )


# ---------- 2. Sortitoutsi: individual user tests (team, W/D/L) ----------

SIO = "https://sortitoutsi.net"


def sortitoutsi():
    rows = []
    for page in range(1, 30):
        trs = get_doc(f"{SIO}/football-manager-2027-tactics?page={page}").xpath("//table/tbody/tr")
        if not trs:
            break
        rows += trs
    out, todo = [], []
    for tr in rows:
        a = tr.xpath("./th/a")[0]
        small = text(tr.xpath('./th/div[contains(@class,"small")]')[0])
        dl = re.search(r"([\d,.]+\s*[KkMm]?)\s*Downloads", small)
        users = tr.xpath('./th/div[contains(@class,"small")]/a')
        base = dict(tactic=a.get("title") or text(a), url=a.get("href"),
                    author=text(users[0]) if users else None, downloads=num(dl.group(1)) if dl else None)
        if tr.xpath("./td[@colspan]"):
            out.append(rec("sortitoutsi", **base, note="No tests yet"))
        else:
            todo.append(base)
    with ThreadPoolExecutor(4) as ex:
        for recs in ex.map(_sio_detail, todo):
            out += recs
    return out


def _sio_detail(base):
    d = get_doc(base["url"])

    def dd(label):
        v = d.xpath(f'//dt[normalize-space()="{label}"]/following-sibling::dd[1]')
        return text(v[0]) if v else None

    labels = d.xpath('//svg[contains(@class,"tactic-pitch")]/@aria-label')
    pos_ip = positions_in(next((x for x in labels if x.startswith("In Possession")), ""))
    pos_oop = positions_in(next((x for x in labels if x.startswith("Out of Possession")), ""))
    f_ip = formation_from_positions(pos_ip) or formation_from_title(dd("In Possession Formation"))
    declared = None if f_ip else formation_from_title(dd("Formation"))  # formation declared by the author
    shared = dict(
        base,
        formation_ip=f_ip or declared, formation_guessed=bool(declared),
        formation_oop=formation_from_positions(pos_oop) or formation_from_title(dd("Out of Possession Formation")),
        positions_ip=pos_ip, positions_oop=pos_oop,
        published=parse_date(dd("Date Created"), "%d %b %Y %H:%M:%S"),
    )
    table = d.xpath('//table[thead/tr/th[normalize-space()="Tester"]]')
    out = []
    for tr in table[0].xpath("./tbody/tr") if table else []:
        c = tr.xpath("./td|./th")  # '', Tester, Team, ME, Win%, PPG, AGF, AGA, GD, PL, W, D, L
        if len(c) != 13:
            continue  # "Total" row when a tactic has several tests
        mode = text((c[2].xpath(".//small") or [None])[0])  # e.g. "Holiday"
        tips = " ".join(tr.xpath(".//abbr/@title"))
        gf = re.search(r"(\d+) Total Goals", tips)
        ga = re.search(r"(\d+) Total Conceded", tips)
        pl = num(text(c[9]))
        out.append(rec(
            "sortitoutsi", **shared,
            tester=text(c[1]), team=text((c[2].xpath(".//a") or [c[2]])[0]),
            patch=text(c[3]) or None,  # typed in by the tester, may be non-standard
            note=f"Test mode: {mode}" if mode else None,
            tests=1, matches=pl, w=num(text(c[10])), d=num(text(c[11])), l=num(text(c[12])),
            gf=num(gf.group(1)) if gf else num(text(c[6])) * pl,
            ga=num(ga.group(1)) if ga else num(text(c[7])) * pl,
        ))
    return out or [rec("sortitoutsi", **shared, note="Could not read the test table")]


# ---------- 3. FM-Tactic-Hub (fm-helper.de): ranking per team tier ----------

HUB = "http://www.fm-helper.de/tactics/index.php"
HUB_TIERS = {"upper": "Top teams (Hub)", "middle": "Mid-table (Hub)", "lower": "Relegation (Hub)"}


def fm_tactic_hub():
    first = get_doc(f"{HUB}?r=rangliste&category=all")
    patch_m = re.search(r"FM\s*(\d+\.\d+(?:\.\d+)?)", first.findtext(".//title") or "")
    patch = patch_m.group(1) if patch_m else None
    # The unfiltered table may show only the first few rows -> loop over each formation to be safe
    formations = [v for v in first.xpath('//select[@name="formation"]/option/@value') if v]
    jobs = [(cat, team, f) for cat, team in HUB_TIERS.items() for f in formations]
    with ThreadPoolExecutor(2) as ex:  # small site, drops connections under bursts
        pages = list(ex.map(lambda j: get_doc(
            f"{HUB}?r=rangliste&category={j[0]}&formation={j[2]}&sort=pts&direction=desc"), jobs))
    out, seen = [], set()
    for (cat, team, _), page in zip(jobs, pages):
        heads = page.xpath("//table//tr[1]/th")
        games = num(text(heads[3])) if len(heads) > 3 else None  # "Overall · 380 games"
        for tr in page.xpath("//table//tr[td]"):
            td = tr.xpath("./td")
            links = td[1].xpath(".//a")
            url = urljoin(HUB, links[0].get("href"))
            if (url, cat) in seen or not games:
                continue
            seen.add((url, cat))
            cell = text(td[2])
            pos = re.findall(r"Positions:\s*((?:[A-Z]{2,4},\s*)+[A-Z]{2,4})", cell)
            pos_ip = positions_in(pos[0]) if pos else []
            pos_oop = positions_in(pos[1]) if len(pos) > 1 else []
            points, win = num(text(td[3])), num(text(td[5]))
            w = round(win / 100 * games) if win is not None else None
            d = points - 3 * w if w is not None else None
            l = games - w - d if d is not None else None
            ok = d is not None and d >= 0 and l >= 0
            date = re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4} \d{2}:\d{2}", text(td[1]))
            dl = re.search(r"(\d+)\s*downloads", text(td[9]))
            out.append(rec(
                "fmtactichub", tactic=text(links[0]), url=url,
                author=text(links[1]) if len(links) > 1 else None, patch=patch, team=team,
                formation_ip=formation_from_positions(pos_ip), formation_oop=formation_from_positions(pos_oop),
                positions_ip=pos_ip, positions_oop=pos_oop,
                matches=games, points=points, gd=num(text(td[6])),
                w=w if ok else None, d=d if ok else None, l=l if ok else None,
                downloads=num(dl.group(1)) if dl else None,
                published=parse_date(date.group(0), "%b %d, %Y %H:%M") if date else None,
                note="W/D/L derived from Win % and points; the Hub does not publish GF/GA",
            ))
    return out


# ---------- FMAX Tactic Lab: public JSON API ----------

FMAX = "https://getfmax.com"


def fmax():
    versions = _session.get(f"{FMAX}/api/lab/versions", timeout=TIMEOUT)
    versions.raise_for_status()
    items = []
    for ver in versions.json()["versions"]:  # every match-engine version, not only the latest
        board = _session.get(f"{FMAX}/api/lab/board", params={"paper": ver["paper"]}, timeout=TIMEOUT)
        board.raise_for_status()
        items += [(ver["name"], it) for it in board.json()["ranked"]]

    def page_url(it):
        slug = re.sub(r"[^a-z0-9]+", "-", it["name"].lower()).strip("-")
        return f"{FMAX}/en/lab/tactic/{slug}-{it['tactic'][:12]}/"

    def formation(it):
        # only on the detail page; a tactic file keeps its formation, so reuse the value of the last update
        url = page_url(it)
        form = previous("fmax", url).get("formation_ip")
        if not form:
            try:
                m = re.search(r"Formation\s*(\d(?:-\d)+)", text(get_doc(url).xpath("//body")[0]))
                form = formation_from_title(m.group(1)) if m else None
            except requests.RequestException:
                pass
        return form

    with ThreadPoolExecutor(3) as ex:
        forms = list(ex.map(lambda x: formation(x[1]), items))
    out = []
    for (patch, it), form in zip(items, forms):
        w, d, l, gf, ga = it["record"]
        out.append(rec(
            "fmax", tactic=it["name"], url=page_url(it), author=it.get("registrant") or None, patch=patch,
            team="FMAX standard test", formation_ip=form,
            matches=w + d + l, w=w, d=d, l=l, gf=gf, ga=ga,  # "matches" in the API can be lower than W+D+L; the record matches the published points
            downloads=it.get("downloads"),
            published=datetime.fromtimestamp(it["published"]).isoformat() if it.get("published") else None,
            note=f"{it.get('independentAccounts', 0)} independent testers",
        ))
    return out


# ---------- metaFM: JSON embedded in the Next.js page ----------

METAFM = "https://metafm.net"


def metafm():
    """The leaderboard holds the current patch; /versions links to the leaderboards of older patches."""
    r = _session.get(f"{METAFM}/leaderboard", timeout=TIMEOUT)
    r.raise_for_status()
    title = re.search(r"<title>(.*?)</title>", r.text, re.S)
    patch = (re.search(r"FM\d+ (\d+\.\d+\.\d+)", title.group(1)) or [None, None])[1] if title else None
    out = _metafm_rows(r.text, patch)
    v = _session.get(f"{METAFM}/versions", timeout=TIMEOUT)
    v.raise_for_status()
    for old in sorted(set(re.findall(r"/versions/(\d+\.\d+\.\d+)", v.text)) - {patch}):
        r = _session.get(f"{METAFM}/versions/{old}", timeout=TIMEOUT)
        r.raise_for_status()
        out += _metafm_rows(r.text, old)
    return out


def _metafm_rows(page, patch):
    rows = None
    for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page, re.S):
        if '\\"rows\\":[' not in chunk:  # quotes are still escaped in the raw chunk
            continue
        s = json.loads('"' + chunk + '"')
        rows, _ = json.JSONDecoder().raw_decode(s[s.index('"rows":[') + 7:])
        break
    if not rows:
        raise ValueError(f"metaFM page layout changed: leaderboard rows not found ({patch})")
    out = []
    for x in rows:
        n = x["matches"]
        w = round(x["winRate"] / 100 * n)
        pts = round(x["ppg"] * n)
        d = pts - 3 * w
        out.append(rec(
            "metafm", tactic=x["name"], url=f"{METAFM}/tactics/{x['id']}", author=x.get("sharedBy") or x.get("author") or None,
            patch=patch, team="metaFM standard test",
            formation_ip=formation_from_positions(x.get("inPos") or []) or formation_from_title(x.get("formation")),
            formation_oop=formation_from_positions(x.get("outPos") or []) or formation_from_title(x.get("oop")),
            positions_ip=x.get("inPos"), positions_oop=x.get("outPos"),
            matches=n, w=w, d=d if d >= 0 else None, l=n - w - d if d >= 0 else None,
            gf=x["gf38"] / 38 * n, ga=x["ga38"] / 38 * n, points=pts,
            note="W/D/L derived from Win % and PPG; the number of tests is not published",
        ))
    return out


# ---------- FMDelicious: official ranking (one protocol, 6,048 matches per tactic) + FM27 archive ----------

FMD = "https://www.fmdelicious.com"
FMD_LEVELS = {"TOP": "Top", "MID": "Sub-top", "BOTTOM": "Underdog"}  # archive club levels -> our team tiers


def fmdelicious():
    first = get_doc(f"{FMD}/tactics?fm_version=FM27")  # the patch picker
    patches = sorted({m.group(1) for h in first.xpath("//a/@href") if (m := re.search(r"patch_version=([\d.]+)", h))})
    ranked, out, seen = [], [], set()
    for patch in patches:
        d = get_doc(f"{FMD}/tactics?fm_version=FM27&patch_version={patch}")
        for tr in d.xpath('//table/tbody/tr[td[@data-label="Tactic"]]'):
            c = {td.get("data-label"): td for td in tr.xpath("./td")}
            a = c["Tactic"].xpath(".//a")[0]
            url = urljoin(FMD, a.get("href"))
            if url in seen:
                continue  # the archive shows the same tests under several 27.0 builds
            seen.add(url)
            sort = lambda label: num(c[label].get("data-sort")) if label in c else None
            form = c["Formation"].get("data-sort") if "Formation" in c else None
            author = text(c["Author"]) if "Author" in c else ""
            base = dict(tactic=text(a), url=url, author=None if author in ("", "Not recorded") else author, patch=patch,
                        formation_ip=form if re.fullmatch(r"\d(?:-\d)+", form or "") else None,
                        published=datetime.fromtimestamp(sort("Date") / 1000).isoformat() if sort("Date") else None)
            base["formation_guessed"] = bool(base["formation_ip"])  # the site's own label, not rebuilt from positions
            patreon = "Patreon-exclusive download · " if tr.xpath('.//span[contains(@class,"badge-patreon")]') else ""
            if "Pts/38" in c:  # official ranking table
                ranked.append((base, sort("Matches"), sort("Pts/38"), sort("Win %"), sort("GF/38"), sort("GA/38"),
                               f"{patreon}Pts/38 vs top {sort('vs Top')} · vs mid {sort('vs Mid')} · vs bottom {sort('vs Bottom')} opponents"))
                continue
            for label, td in c.items():  # archive (beta): one 38-match league season at a TOP, MID and BOTTOM club
                m = re.fullmatch(r"(TOP|MID|BOTTOM) · (.+)", label or "")
                pts = num(td.get("data-sort"))
                if m and pts is not None and "SACK" not in text(td).upper():
                    out.append(rec("fmdelicious", **base, team=f"{m.group(2)} (FMD {m.group(1).lower()})",
                                   tier=FMD_LEVELS[m.group(1)], matches=38, points=pts,
                                   note=f"{patreon}FMDelicious archive (beta): one league season per club, points only"))

    def record(job):
        # exact W-D-L is only on the tactic page; reuse the last update while the number of matches is unchanged
        base, n, pts38, win, gf38, ga38, note = job
        old = previous("fmdelicious", base["url"])
        if old.get("w") is not None and old.get("matches") == n and "derived" not in (old.get("note") or ""):
            wdl = old["w"], old["d"], old["l"]
        else:
            try:
                m = re.search(r"Record\s*([\d,]+)\s*[–-]\s*([\d,]+)\s*[–-]\s*([\d,]+)", text(get_doc(base["url"]).xpath("//body")[0]))
            except requests.RequestException:
                m = None
            wdl = tuple(num(g) for g in m.groups()) if m else None
        if not wdl or sum(wdl) != n:  # page not readable: derive from Win % and points like metaFM
            w = round(win / 100 * n)
            d = round(pts38 / 38 * n) - 3 * w
            wdl, note = (w, d, n - w - d) if d >= 0 else (None, None, None), note + " · W/D/L derived from Win % and points"
        return rec("fmdelicious", **base, team="FMDelicious standard test", matches=n,
                   w=wdl[0], d=wdl[1], l=wdl[2], points=None if wdl[0] is not None else pts38 / 38 * n,
                   gf=gf38 / 38 * n, ga=ga38 / 38 * n, note=note)

    with ThreadPoolExecutor(3) as ex:
        out += list(ex.map(record, ranked))
    return out


# ---------- FMSite.net (Spanish community): FM27 tactic files, downloads only ----------

FMSITE = "https://www.fmsite.net/files/category/123-t%C3%A1cticas-fm27/"


def fmsite():
    out, seen = [], set()
    for page in range(1, 21):
        d = get_doc(FMSITE + (f"page/{page}/" if page > 1 else ""))
        items = [it for it in d.xpath('//li[contains(@class,"ipsData__item")][@data-rowid]')  # other lists are site-wide widgets
                 if it.get("data-rowid") not in seen]
        if not items:
            break
        for it in items:
            seen.add(it.get("data-rowid"))
            a = it.xpath(".//h4/a")[0]
            title = text(a)
            f = formation_from_title(title)
            out.append(rec(
                "fmsite", tactic=title, url=a.get("href"),
                author=text((it.xpath('.//a[contains(@class,"ipsUsername")]') or [None])[0]).replace("﻿", "") or None,
                downloads=num((it.xpath('.//li[@data-stattype="downloads"]//@data-stat-value') or [None])[0]),
                published=(it.xpath(".//time/@datetime") or [None])[0],
                formation_ip=f, formation_guessed=bool(f),
            ))
    return out


# ---------- 4. FM Scout: tactic catalogue (no structured test data) ----------

FMS = "https://www.fmscout.com/"


def fmscout():
    out, seen = [], set()
    for page in range(1, 11):
        d = get_doc(FMS + "c-fm27-tactics.html" + (f"?d={page}" if page > 1 else ""))
        new = 0
        for card in d.xpath('//div[contains(@class,"column")][h5/a]'):
            a = card.xpath("./h5/a")[0]
            url = urljoin(FMS, a.get("href"))
            if url in seen:
                continue
            seen.add(url)
            new += 1

            def after(icon):
                t = card.xpath(f'./i[@class="{icon}"]/following-sibling::text()[1]')
                return num(t[0]) if t else None

            title = text(a)
            sub = text((card.xpath('./div[contains(@class,"subheader")]') or [None])[0])
            f = formation_from_title(title) or formation_from_title(sub)
            out.append(rec(
                "fmscout", tactic=title, url=url, views=after("fi-eye"), downloads=after("fi-download"),
                published=(card.xpath("./time/@datetime") or [None])[0],
                formation_ip=f, formation_guessed=bool(f), note=sub or None,
            ))
        if not new:
            break
    return out


# ---------- 5. FM Base: threads in the FM27 Tactics section ----------

FMB = "https://fm-base.co.uk"


def fm_base():
    out, url = [], f"{FMB}/forums/fm27-tactics.301/"
    for _ in range(20):
        d = get_doc(url)
        for it in d.xpath('//div[contains(@class,"structItem--thread")]'):
            a = it.xpath('.//div[contains(@class,"structItem-title")]/a[last()]')[0]
            title = text(a)

            def pair(label):
                v = it.xpath(f'.//dl[dt[normalize-space()="{label}"]]/dd')
                return num(text(v[0])) if v else None

            f = formation_from_title(title)
            out.append(rec(
                "fmbase", tactic=title, url=urljoin(FMB, a.get("href")), author=it.get("data-author"),
                views=pair("Views"),
                published=(it.xpath('.//li[contains(@class,"structItem-startDate")]//time/@datetime') or [None])[0],
                formation_ip=f, formation_guessed=bool(f), note=f"{int(pair('Replies') or 0)} replies",
            ))
        nxt = d.xpath('//a[contains(@class,"pageNav-jump--next")]/@href')
        if not nxt:
            break
        url = urljoin(FMB, nxt[0])
    return out


# ---------- 6. YouTube (YouTube Data API v3 needs a key; RSS fallback needs channel IDs) ----------

YOUTUBE_QUERY = "FM27 tactic"


def youtube():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    key = os.environ.get("YOUTUBE_API_KEY") or cfg.get("youtube_api_key")
    if not key:
        channels = cfg.get("youtube_channel_ids") or []
        if not channels:
            raise Skipped("Not configured: add youtube_api_key or youtube_channel_ids to config.json")
        return youtube_rss(channels)
    api = "https://www.googleapis.com/youtube/v3"
    r = _session.get(f"{api}/search", timeout=TIMEOUT, params=dict(
        part="snippet", q=YOUTUBE_QUERY, type="video", maxResults=50, order="relevance", key=key))
    r.raise_for_status()
    items = r.json().get("items", [])
    ids = [i["id"]["videoId"] for i in items]
    stats = {}
    if ids:
        s = _session.get(f"{api}/videos", timeout=TIMEOUT,
                         params=dict(part="statistics", id=",".join(ids), key=key))
        s.raise_for_status()
        stats = {v["id"]: v.get("statistics", {}) for v in s.json().get("items", [])}
    out = []
    for i in items:
        sn, vid = i["snippet"], i["id"]["videoId"]
        title = unescape(sn["title"])
        f = formation_from_title(title)
        out.append(rec(
            "youtube", tactic=title, url=f"https://www.youtube.com/watch?v={vid}",
            author=unescape(sn.get("channelTitle", "")), published=sn.get("publishedAt"),
            views=num(stats.get(vid, {}).get("viewCount")),
            formation_ip=f, formation_guessed=bool(f),
        ))
    return out


def youtube_rss(channel_ids):
    """No API key: read each channel's public RSS feed and keep videos that mention FM27."""
    out = []
    for cid in channel_ids:
        r = _session.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": cid}, timeout=TIMEOUT)
        r.raise_for_status()
        feed = etree.fromstring(r.content)
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
              "m": "http://search.yahoo.com/mrss/"}
        channel = feed.findtext("a:title", namespaces=ns)
        for e in feed.findall("a:entry", ns):
            title = e.findtext("a:title", namespaces=ns) or ""
            if not re.search(r"fm\s?-?27|football manager 27", title, re.I):
                continue
            views = e.find("m:group/m:community/m:statistics", ns)
            vid = e.findtext("yt:videoId", namespaces=ns)
            f = formation_from_title(title)
            out.append(rec("youtube", tactic=title, url=f"https://www.youtube.com/watch?v={vid}", author=channel,
                           published=e.findtext("a:published", namespaces=ns),
                           views=num(views.get("views")) if views is not None else None,
                           formation_ip=f, formation_guessed=bool(f)))
    return out


# ---------- 7. fmkorea: blocked by Cloudflare Turnstile ----------

def fmkorea():
    r = _session.get("https://www.fmkorea.com/", timeout=TIMEOUT)
    if r.status_code == 430 or "cf-turnstile" in r.text:
        raise Blocked("fmkorea blocks automated access (Cloudflare captcha) — enter data by hand in data/manual.csv")
    raise Blocked("fmkorea has no structured test table — enter data by hand in data/manual.csv")


# ---------- Manual entry (fmkorea / other sources) ----------

MANUAL_NUMERIC = ("tests", "matches", "w", "d", "l", "gf", "ga", "points", "downloads", "views")


def manual(path):
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("tactic") or "").strip():
                continue
            vals = {k: (v or "").strip() or None for k, v in row.items() if k}
            for k in MANUAL_NUMERIC:
                vals[k] = num(vals.get(k))
            source = vals.pop("source", None) or "khac"
            out.append(rec(source, **vals, origin="manual"))
    return out


SOURCES = {
    "fmarena": ("FM-Arena", fm_arena),
    "sortitoutsi": ("Sortitoutsi", sortitoutsi),
    "fmax": ("FMAX Tactic Lab", fmax),
    "metafm": ("metaFM", metafm),
    "fmdelicious": ("FMDelicious", fmdelicious),
    "fmtactichub": ("FM-Tactic-Hub", fm_tactic_hub),
    "fmscout": ("FM Scout", fmscout),
    "fmsite": ("FMSite.net", fmsite),
    "fmbase": ("FM Base", fm_base),
    "youtube": ("YouTube", youtube),
    "fmkorea": ("fmkorea", fmkorea),
}
