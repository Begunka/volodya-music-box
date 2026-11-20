"""Дни рождения альбомов: число и месяц первого выхода — для «в этот день» и ленты «следом».

Почему не дата из поста: её ставит бот-загрузчик по данным Deezer, а там у переизданий
часто дата ремастера или случайное число в нужном году (All Things Must Pass стоял
20 ноября вместо 27-го). Поэтому дата засчитывается только с подтверждением.

Как решается (только даты в год первого выхода из архива — другие относятся к переизданиям):
  1. Английская Википедия (поле «Released» в карточке) — главный источник: так дату видят все,
     кто её гуглит. Берётся самая ранняя её дата, если её подтвердил кто-то ещё
     (MusicBrainz, Discogs, пост, Wikidata или русская Википедия — эти две хотя бы
     страхуют от ошибки разбора). Самая ранняя — потому что у альбома, вышедшего в США
     и Англии в разные дни, день рождения — первый выход.
     Если подтверждения нет, но и возразить некому (у баз нет другой точной даты в этот год,
     а неполная дата MusicBrainz вроде «1987-10» с ней сходится) — тоже берётся. Без английской
     статьи так же берётся дата из русской — но не тогда, когда английская знает только месяц
     («March 1967»: точный день неизвестен — так у The Velvet Underground & Nico).
  2. Иначе нужна дата, которую
     назвали минимум два разных семейства источников; из таких берётся самая ранняя.
  3. Иначе дня рождения нет: лучше без даты, чем с чужой.

Семейства (внутри одного подтверждения не складываются — они списывают друг у друга):
  mb   — MusicBrainz: first-release-date группы релизов; если там только год или месяц —
         даты самых ранних изданий этого года;
  wiki — Википедия (en, ru) и Wikidata (дата публикации P577 с точностью до дня);
  dg   — Discogs: основное издание мастер-релиза (или самые ранние издания);
  post — дата из поста (кроме 1 января — так пишут, когда известен только год).
Почему не «два любых источника» и всё: у The Money Store русская Википедия и Deezer
сходились на 20 апреля (предрелиз), а вышел альбом 24-го — так написано в английской.

dates.json (корень) — доказательства: {"номер": {"y": год, "seen": {источник: [даты]},
  "ids": {"mb": …, "wd": …, "dg": …}, "at": время проверки}}. Решение (`day_for`) считается
  при экспорте в data.json из этих доказательств, так что правка правил не требует новых запросов.
dates_fix.json (корень) — ручные поправки, сильнее всего: {"номер": "11-27"} или {"номер": "none"}.

Запуск (так делает workflow dates.yml):
  python bot/dates.py --minutes 40            — новые альбомы и те, кого пора перепроверить
  python bot/dates.py --only 235,511          — только эти номера
  python bot/dates.py --all --minutes 170     — все заново
  python bot/dates.py --report                — без сети: сводка по dates.json
"""
import calendar
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
DATES = ROOT / "dates.json"
FIX = ROOT / "dates_fix.json"
WIKI = ROOT / "wiki"
RULES = 4             # поменял правила в decide() — увеличь: бот пересоберёт data.json
RETRY_DAYS = 30       # дата не подтвердилась — спросим базы снова через месяц
RECHECK_DAYS = 180    # подтвердилась — перепроверим через полгода (базы исправляют ошибки)

FAMILY = {"mb": "mb", "wd": "wiki", "en": "wiki", "ru": "wiki", "dg": "dg", "post": "post"}


# ---------- даты ----------

def full(y, m, d):
    """Полная дата 'ГГГГ-ММ-ДД' или None, если такой даты нет."""
    try:
        y, m, d = int(y), int(m), int(d)
    except (TypeError, ValueError):
        return None
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    if d > calendar.monthrange(y, m)[1]:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def iso(s):
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", s or "")
    return full(*m.groups()) if m else None


def post_date(a):
    """Дата из поста: '1970-11-20' или '31-08-1970' (так пишет бот-загрузчик). 1 января не в счёт."""
    s = (a.get("date") or "").strip()
    d = iso(s)
    if not d:
        m = re.fullmatch(r"(\d{1,2})[-.](\d{1,2})[-.](\d{4})", s)
        d = full(m.group(3), m.group(2), m.group(1)) if m else None
    return None if not d or d[5:] == "01-01" else d


# ---------- решение ----------

def decide(seen, a):
    """(«ММ-ДД», [кто подтвердил]) или (None, []). seen — доказательства из dates.json."""
    year = str(a.get("year") or "")
    srcs = {k: [d for d in v if d[:4] == year] for k, v in (seen or {}).items() if k in FAMILY}
    p = post_date(a)
    if p and p[:4] == year:
        srcs["post"] = [p]
    srcs = {k: v for k, v in srcs.items() if v and k in FAMILY}
    # 1. английская Википедия, подтверждённая кем угодно ещё
    en = srcs.get("en", [])
    for d in sorted(en):
        by = sorted(k for k, v in srcs.items() if k != "en" and d in v)
        if by:
            return d[5:], ["en"] + by
    # 1б. Википедия одна, но никто из баз ей не противоречит: у MusicBrainz, Discogs и Wikidata
    #     нет другой точной даты в этот год, а неполная дата MusicBrainz («1987-10») с ней сходится.
    #     Пост не в счёт — он и так чаще всего ошибается. Русская — только если английской нет.
    enp = (seen or {}).get("enp", "")
    enp = enp if enp[:4] == year and len(enp) == 7 else ""
    for lang in ("en", "ru"):
        ds = sorted(srcs.get(lang, []))
        if not ds:
            continue
        if lang == "ru" and enp:
            break      # английская статья знает только месяц — русской одной не верим
        d = ds[0]
        rivals = [x for k in ("mb", "dg", "wd", "ru", "en") if k != lang for x in srcs.get(k, []) if x != d]
        mbp = (seen or {}).get("mbp", "")
        if not rivals and (not mbp or d.startswith(mbp) or not mbp.startswith(year)):
            return d[5:], [lang] + sorted(k for k, v in srcs.items() if k != lang and d in v)
        break
    # 2. согласие двух семейств
    votes = {}
    for src, ds in srcs.items():
        for d in ds:
            votes.setdefault(d, set()).add(FAMILY[src])
    good = []
    for d, fams in votes.items():
        need = fams - {"post"} if d[5:] == "01-01" else fams   # 1 января любят ставить «для года»
        # английская статья знает только месяц — день должен быть в этом месяце
        if len(need) >= 2 and (not enp or d.startswith(enp)):
            good.append(d)
    if not good:
        return None, []
    d = min(good)
    return d[5:], sorted(k for k, v in srcs.items() if d in v)


_cache = {}


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def day_for(a):
    """«ММ-ДД» для data.json или None. Ручная поправка сильнее всего."""
    if "dates" not in _cache:
        _cache["dates"], _cache["fix"] = _load(DATES), _load(FIX)
    fix = _cache["fix"].get(str(a["id"]))
    if fix:
        return None if fix == "none" else fix
    rec = _cache["dates"].get(str(a["id"]))
    return decide(rec.get("seen"), a)[0] if rec else None


def signature():
    """Меняется, когда изменились доказательства, поправки или правила — тогда бот пересобирает data.json."""
    h = hashlib.sha1(str(RULES).encode())
    for p in (DATES, FIX):
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


# ---------- сеть ----------

_last = {}


def pace(host, gap):
    """Не чаще одного запроса в gap секунд к одному сайту (MusicBrainz и Discogs требуют ~1 в секунду)."""
    wait = _last.get(host, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def mb(path):
    from enrich import get_json
    pace("mb", 1.1)
    return get_json("https://musicbrainz.org/ws/2/" + path)


def dg(path, **params):
    from enrich import get_json
    pace("dg", 1.1)
    params["token"] = DISCOGS_TOKEN
    return get_json("https://api.discogs.com/" + path + "?" + urllib.parse.urlencode(params))


def wapi(host, **params):
    from enrich import get_json
    pace(host, 0.2)
    params.update(format="json", formatversion=2)
    return get_json(f"https://{host}/w/api.php?" + urllib.parse.urlencode(params))


DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "").strip()
VERBOSE = False        # подробности (что нашлось в каждой базе) — при --only
PARTIAL = {}           # неполная дата MusicBrainz текущего альбома


def say(msg):
    if VERBOSE:
        print(msg)


def phrase(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def names(artist):
    """«John Lennon / Plastic Ono Band» → [целиком, «John Lennon», «Plastic Ono Band»]."""
    parts = re.split(r"\s*(?:/|,|&|\+|\bfeat\.?|\bft\.|\bwith\b|\band\b|\bи\b)\s*", artist or "", flags=re.I)
    return [artist or ""] + [x for x in parts if x.strip()]


def matches(their_title, their_artist, a, artist=None):
    """Насколько запись из базы похожа на наш альбом: 0 — не он, иначе сумма сходств (до ~2.2)."""
    from enrich import similar, title_match, norm
    t = max(title_match(a["title"], their_title or ""),
            title_match(a["title_full"], their_title or "") if a.get("title_full") else 0)
    # в базах бывает «John Lennon/Plastic Ono Band», а в посте просто «Plastic Ono Band»
    parts = [x for x in re.split(r"\s*/\s*", their_title or "") if x.strip()]
    if len(parts) > 1:
        t = max([t] + [0.95 * title_match(a["title"], x) for x in parts])
    ours = names(artist or a["artist"])
    ar = max(similar(x, y) for x in names(their_artist) for y in ours)
    if t < 0.85 or ar < 0.6:
        return 0
    return t + ar + (0.2 if norm(their_title) == norm(a["title"]) else 0)


def plain_title(title):
    return re.sub(r"[^\w]+", " ", title or "").strip()


# ---------- MusicBrainz ----------

def mb_find(artist, a):
    from enrich import first_artist
    year = str(a["year"])
    for q in (f'releasegroup:"{phrase(a["title"])}" AND artist:"{phrase(first_artist(artist))}"',
              f'releasegroup:({phrase(plain_title(a["title"]))}) AND artist:"{phrase(first_artist(artist))}"'):
        data = mb("release-group/?fmt=json&limit=50&query=" + urllib.parse.quote(q))
        best = None
        for rg in data.get("release-groups", []):
            credit = " / ".join(c.get("name", "") for c in rg.get("artist-credit", []))
            sc = matches(rg.get("title"), credit, a, artist)
            if not sc:
                continue
            if (rg.get("first-release-date") or "")[:4] == year:
                sc += 0.5          # среди тёзок (концертник, переиздание) — та, что вышла в наш год
            # одноимённые синглы и сборники («Black Sabbath» — и альбом, и песня, и сборник)
            sc += {"Album": 0.3, "EP": 0.1}.get(rg.get("primary-type"), 0)
            if set(rg.get("secondary-types") or []) & {"Compilation", "Live", "Remix", "DJ-mix"} and \
                    not re.search(r"live|концерт|best|greatest|collection|сборник|remix", a["title"], re.I):
                sc -= 0.3
            if not best or sc > best[0]:
                best = (sc, rg["id"])
        say(f"    MB поиск {q[:60]}…: {len(data.get('release-groups', []))} групп, выбрана {best}")
        if best:
            return best[1]
        if plain_title(a["title"]) == a["title"]:
            break
    return None


def mb_part(a, ids):
    """Даты MusicBrainz + ссылки на Wikidata/Википедию из той же записи."""
    from enrich import artist_variants
    year = str(a["year"])
    rid = ids.get("mb")
    if not rid:
        rid = mb_find(a["artist"], a)
        if not rid:
            for v in artist_variants(a["artist"]):
                rid = mb_find(v, a)
                if rid:
                    break
    if not rid:
        return [], None, None, {}
    rg = mb(f"release-group/{rid}?fmt=json&inc=url-rels")
    qid, pages = None, {}
    for r in rg.get("relations", []):
        res = (r.get("url") or {}).get("resource", "")
        if r.get("type") == "wikidata":
            qid = res.rstrip("/").rsplit("/", 1)[-1]
        m = re.match(r"https?://(en|ru)\.wikipedia\.org/wiki/(.+)", res)
        if r.get("type") == "wikipedia" and m:
            pages[m.group(1)] = urllib.parse.unquote(m.group(2)).replace("_", " ")
    frd = rg.get("first-release-date") or ""
    say(f"    MB: {rg.get('title')} · {frd or '—'} · {rid}")
    d = iso(frd)
    if d:
        return [d], rid, qid, pages
    PARTIAL["mbp"] = frd
    dates = set()
    if frd[:4] == year:
        # у группы известен только год (или месяц) — смотрим даты её изданий в этот год
        for off in range(0, 800, 100):     # у классики бывают сотни изданий, бутлеги не нужны
            r = mb(f"release?release-group={rid}&status=official&fmt=json&limit=100&offset={off}")
            for rel in r.get("releases", []):
                for ev in rel.get("release-events") or [{"date": rel.get("date")}]:
                    x = iso(ev.get("date"))
                    if x and x.startswith(frd):
                        dates.add(x)
            if off + 100 >= r.get("release-count", 0):
                break
    return sorted(dates)[:3], rid, qid, pages


# ---------- Википедия и Wikidata ----------

MONTHS = {}
for i, words in enumerate([
        "january jan января январь", "february feb февраля февраль", "march mar марта март",
        "april apr апреля апрель", "may мая май", "june jun июня июнь", "july jul июля июль",
        "august aug августа август", "september sep sept сентября сентябрь", "october oct октября октябрь",
        "november nov ноября ноябрь", "december dec декабря декабрь"], 1):
    for n in words.split():
        MONTHS[n] = i


def month(word):
    """Номер месяца по названию (англ. полное или сокращение, рус. «ноября»/«ноябрь») или None."""
    return MONTHS.get(word.lower().strip("."))


INFOBOX = re.compile(r"\{\{\s*(?:infobox[ _]+album|музыкальный[ _]+альбом|альбом)\s*(?=\||\n|<!--)", re.I)
RELEASED = {"released", "release date", "выпущен", "дата выпуска", "дата релиза", "релиз"}


def template_params(text, start):
    """Параметры верхнего уровня шаблона, который начинается с '{{' в позиции start."""
    depth, i, cur, params = 0, start, [], []
    while i < len(text):
        two = text[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            if depth == 0:
                params.append("".join(cur))
                return params[1:]
            cur.append(two)
            i += 2
            continue
        if text[i] == "|" and depth == 1:
            params.append("".join(cur))
            cur = []
        else:
            cur.append(text[i])
        i += 1
    return params[1:]


def dates_in(val):
    """Все полные даты в значении поля карточки: шаблоны {{Start date|1970|11|27}}, {{дата|27|11|1970}},
    текст «27 November 1970», «November 27, 1970», «27 ноября 1970», «1970-11-27», «27.11.1970»."""
    val = re.sub(r"<!--.*?-->", " ", val, flags=re.S)
    val = re.sub(r"<ref[^>]*/>", " ", val, flags=re.I)
    val = re.sub(r"<ref[^>]*>.*?</ref>", " ", val, flags=re.S | re.I)
    out = []
    for m in re.finditer(r"\{\{\s*[^|{}]+\|([^{}]*)\}\}", val):
        nums = [x.strip() for x in m.group(1).split("|") if "=" not in x and re.fullmatch(r"\s*\d{1,4}\s*", x)]
        if len(nums) >= 3:
            d = full(nums[0], nums[1], nums[2]) if len(nums[0]) == 4 else \
                full(nums[2], nums[1], nums[0]) if len(nums[2]) == 4 else None
            if d:
                out.append(d)
    # шаблоны → их текст, ссылки → подпись
    for _ in range(5):
        new = re.sub(r"\{\{\s*[^|{}]+\|([^{}]*)\}\}",
                     lambda m: " " + " ".join(x for x in m.group(1).split("|") if "=" not in x) + " ", val)
        new = re.sub(r"\{\{[^{}|]*\}\}", " ", new)    # {{nbsp}}, {{-}} и т.п.
        if new == val:
            break
        val = new
    val = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", val)
    for m in re.finditer(r"\b(\d{1,2})\s+([A-Za-zА-Яа-яЁё]+)\.?,?\s+(\d{4})", val):
        mo = month(m.group(2))
        d = mo and full(m.group(3), mo, m.group(1))
        if d:
            out.append(d)
    for m in re.finditer(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", val):
        mo = month(m.group(1))
        d = mo and full(m.group(3), mo, m.group(2))
        if d:
            out.append(d)
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", val):
        d = full(*m.groups())
        if d:
            out.append(d)
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", val):
        d = full(m.group(3), m.group(2), m.group(1))
        if d:
            out.append(d)
    return sorted(set(out))


def months_in(val):
    """Неполные даты «ГГГГ-ММ»: {{Start date|1967|03}}, «May 1978», «март 1967»."""
    val = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>|<!--.*?-->", " ", val, flags=re.S | re.I)
    out = []
    for m in re.finditer(r"\{\{\s*start[ _]date[^|{}]*\|\s*(\d{4})\s*\|\s*(\d{1,2})\s*(?:\||\}\})", val, re.I):
        out.append(f"{m.group(1)}-{int(m.group(2)):02d}")
    for m in re.finditer(r"([A-Za-zА-Яа-яЁё]+)\.?,?\s+(\d{4})", val):
        mo = month(m.group(1))
        if mo:
            out.append(f"{m.group(2)}-{mo:02d}")
    return sorted(set(out))


def infobox(text):
    """(полные даты, неполные «ГГГГ-ММ») из поля «Released»/«Выпущен» карточки альбома."""
    m = INFOBOX.search(text or "")
    if not m:
        say("    карточки альбома нет")
        return [], []
    out, part = [], []
    for p in template_params(text, m.start()):
        name, eq, val = p.partition("=")
        if eq and re.sub(r"[\s_]+", " ", name).strip().lower() in RELEASED:
            say(f"    поле «{name.strip()}»: {' '.join(val.split())[:220]}")
            out += dates_in(val)
            part += months_in(val)
    return sorted(set(out)), sorted(set(part))


def infobox_dates(text):
    return infobox(text)[0]


def wikitext(lang, title):
    r = wapi(f"{lang}.wikipedia.org", action="query", prop="revisions", rvprop="content", rvslots="main",
             rvsection=0, redirects=1, titles=title)
    pages = r.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return ""
    revs = pages[0].get("revisions") or [{}]
    return revs[0].get("slots", {}).get("main", {}).get("content", "")


def pack_page(aid):
    """Статья из справки (wiki/pack-N.json), если её уже нашли: {'ru': 'Название'}."""
    p = _load(WIKI / f"pack-{aid // 50}.json").get(str(aid)) or {}
    m = re.match(r"https?://(en|ru)\.wikipedia\.org/wiki/(.+)", p.get("url") or "")
    return {m.group(1): urllib.parse.unquote(m.group(2)).replace("_", " ")} if m else {}


def wiki_part(a, qid, pages):
    seen = {}
    pages = dict(pages or {})
    if not qid and not pages:
        pages = pack_page(a["id"])
    if not qid and pages:
        lang, t = next(iter(pages.items()))
        r = wapi(f"{lang}.wikipedia.org", action="query", prop="pageprops", ppprop="wikibase_item", redirects=1, titles=t)
        pg = (r.get("query", {}).get("pages") or [{}])[0]
        qid = (pg.get("pageprops") or {}).get("wikibase_item")
    if qid:
        ent = wapi("www.wikidata.org", action="wbgetentities", ids=qid, props="claims|sitelinks")
        e = next(iter((ent.get("entities") or {}).values()), {})
        wd = []
        for c in (e.get("claims") or {}).get("P577", []):
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
            if v.get("precision") == 11:
                d = iso((v.get("time") or "")[1:11])
                if d:
                    wd.append(d)
        if wd:
            seen["wd"] = sorted(set(wd))
        links = e.get("sitelinks") or {}
        for lang in ("en", "ru"):
            if lang + "wiki" in links:
                pages[lang] = links[lang + "wiki"]["title"]
    for lang in ("en", "ru"):
        if lang in pages:
            say(f"    {lang}: {pages[lang]}")
            ds, part = infobox(wikitext(lang, pages[lang]))
            if ds:
                seen[lang] = ds
            elif lang == "en" and part:
                # английская статья знает только месяц: «точная дата неизвестна» — тоже знание
                seen["enp"] = part[0]
    return seen, qid


# ---------- Discogs ----------

def dg_part(a, ids):
    if not DISCOGS_TOKEN:
        return [], None
    from enrich import first_artist, _dg_artist
    year = str(a["year"])
    did = ids.get("dg")
    if not did:
        for kind in ("master", "release"):
            for title in dict.fromkeys([a["title"], plain_title(a["title"])]):
                data = dg("database/search", type=kind, artist=first_artist(a["artist"]), release_title=title, per_page=20)
                best = None
                for r in data.get("results", []):
                    ra, rt = _dg_artist(r)
                    sc = matches(rt, ra, a)
                    if not sc:
                        continue
                    if str(r.get("year") or "") == year:
                        sc += 0.5
                    if not best or sc > best[0]:
                        best = (sc, kind[0] + str(r["id"]))
                if best:
                    did = best[1]
                    break
            if did:
                break
    say(f"    Discogs: {did}")
    if not did:
        return [], None
    if did[0] == "r":
        d = iso(dg(f"releases/{did[1:]}").get("released"))
        return ([d] if d else []), did
    # мастер: основное издание; если у него нет точной даты в наш год — самые ранние издания
    rel_id = dg(f"masters/{did[1:]}").get("main_release")
    d = iso(dg(f"releases/{rel_id}").get("released")) if rel_id else None
    if d and d[:4] == year:
        return [d], did
    vs = dg(f"masters/{did[1:]}/versions", sort="released", sort_order="asc", per_page=25)
    out = sorted({x for x in (iso(v.get("released")) for v in vs.get("versions", [])) if x and x[:4] == year})
    return (out[:2] or ([d] if d else [])), did


# ---------- проход по архиву ----------

USEFUL = {"mb": ("mb",), "wd": ("wd", "en", "ru"), "dg": ("dg",)}


def check(a, rec, fresh=False):
    """Собирает доказательства для одного альбома. Discogs идёт параллельно (у него свой лимит).
    Найденные раньше записи в базах берутся снова, только если они дали дату в наш год
    (иначе ищем заново: вдруг в прошлый раз нашлась не та запись)."""
    ids = {}
    if rec and not fresh and rec.get("y") == str(a["year"]):
        seen = rec.get("seen") or {}
        ids = {k: v for k, v in (rec.get("ids") or {}).items()
               if any(d[:4] == rec["y"] for s in USEFUL.get(k, ()) for d in (seen.get(s) or []) if isinstance(d, str))}
    PARTIAL.clear()
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(dg_part, a, ids)
        mbd, rid, qid, pages = mb_part(a, ids)
        seen, qid = wiki_part(a, qid or ids.get("wd"), pages)
        dgd, did = fut.result()
    if PARTIAL.get("mbp"):
        seen["mbp"] = PARTIAL["mbp"]
    if mbd:
        seen["mb"] = mbd
    if dgd:
        seen["dg"] = dgd
    new_ids = {k: v for k, v in (("mb", rid), ("wd", qid), ("dg", did)) if v}
    return {"y": str(a["year"]), "seen": seen, "ids": new_ids, "at": int(time.time())}


def save(dates):
    """Одна строка на альбом — читаемые изменения в истории git."""
    keys = sorted(dates, key=int)
    body = ",\n".join(json.dumps(k) + ":" + json.dumps(dates[k], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                      for k in keys)
    tmp = DATES.with_suffix(".tmp")
    tmp.write_text("{\n" + body + "\n}\n", encoding="utf-8")
    tmp.replace(DATES)


def due(a, rec, now):
    if not rec or rec.get("y") != str(a["year"]):
        return True
    ok = decide(rec.get("seen"), a)[0]
    return now - rec.get("at", 0) > (RECHECK_DAYS if ok else RETRY_DAYS) * 86400


def report(albums, dates):
    fix = _load(FIX)
    by_fam, n_ok, n_none, rows = {}, 0, 0, []
    for a in albums:
        rec = dates.get(str(a["id"]))
        if not rec:
            continue
        day, fams = decide(rec.get("seen"), a)
        if day:
            n_ok += 1
            k = "+".join(fams)
            by_fam[k] = by_fam.get(k, 0) + 1
        else:
            n_none += 1
        p = post_date(a)
        if p and day and p[5:] != day:
            rows.append(f"  №{a['id']} {a['artist']} — {a['title']}: в посте {p[5:]}, на самом деле {day} ({'+'.join(fams)})")
    print(f"Проверено: {len(dates)} из {len(albums)}. С подтверждённой датой: {n_ok}, без даты: {n_none}. "
          f"Ручных поправок: {len(fix)}")
    for k, v in sorted(by_fam.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    if rows:
        print(f"Дата в посте была неверной ({len(rows)}):")
        print("\n".join(rows))


def main():
    args = sys.argv[1:]
    minutes = float(args[args.index("--minutes") + 1]) if "--minutes" in args else 40
    only = {x.strip() for x in args[args.index("--only") + 1].split(",") if x.strip()} if "--only" in args else None
    albums = [a for a in _load(ROOT / "albums.json") or [] if a.get("artist") and a.get("year")
              and not str(a.get("title", "")).startswith("http")]
    dates = _load(DATES)
    if "--report" in args:
        report(albums, dates)
        return
    global VERBOSE
    VERBOSE = bool(only)
    now, deadline = time.time(), time.time() + minutes * 60
    if only:
        todo = [a for a in albums if str(a["id"]) in only]
    elif "--all" in args:
        todo = list(albums)
    else:
        todo = [a for a in albums if due(a, dates.get(str(a["id"])), now)]
        # сначала новые альбомы, потом давно проверенные
        todo.sort(key=lambda a: (str(a["id"]) in dates, dates.get(str(a["id"]), {}).get("at", 0)))
    print(f"К проверке: {len(todo)}")
    done = 0
    for a in todo:
        if time.time() > deadline:
            print("Время вышло, остальное — в следующий запуск.")
            break
        key = str(a["id"])
        try:
            rec = check(a, dates.get(key), fresh="--all" in args)
        except Exception as e:      # сеть: не запоминаем, спросим в следующий раз
            print(f"  ошибка ({str(e)[:120]}): №{key} {a['artist']} — {a['title']}")
            continue
        dates[key] = rec
        day, fams = decide(rec["seen"], a)
        seen = " ".join(f"{k}:{','.join(d[5:] if d[:4] == rec['y'] else d for d in v)}" if isinstance(v, list) else f"{k}:{v}"
                        for k, v in sorted(rec["seen"].items()))
        p = post_date(a)
        print(f"{day or '  -  '} {'+'.join(fams):<16} №{key} {a['artist']} — {a['title']} ({a['year']})  "
              f"[{seen}{' post:' + (p[5:] if p[:4] == rec['y'] else p) if p else ''}]")
        done += 1
        if done % 20 == 0:
            save(dates)
    save(dates)
    report(albums, dates)


if __name__ == "__main__":
    main()
