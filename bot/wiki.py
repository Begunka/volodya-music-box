"""Справка к альбомам: вступление статьи из Википедии и состав из Discogs.

Запуск: python bot/wiki.py [--minutes 45] [--only 12,34]
Идёт по альбомам, у которых справки ещё нет, пока не кончится время.
Результат — wiki/pack-N.json (по 50 альбомов в файле, как обложки),
мини-приложение подгружает нужный файл, только когда открываешь альбом.
wiki/state.json помнит, что уже проверено, чтобы не спрашивать базы заново.
"""
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from enrich import get_json, norm, similar, first_artist, artist_variants, first_found  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
STATE = WIKI / "state.json"
PACK = 50
RETRY_DAYS = 30          # не нашлось — спросим ещё раз через месяц: статьи появляются
TEXT_MAX = 1100
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "").strip()

_last = {}


def pace(host, gap):
    """Не чаще одного запроса в gap секунд к одному сайту (правила MusicBrainz и Discogs)."""
    wait = _last.get(host, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def mb(url):
    pace("mb", 1.1)
    return get_json("https://musicbrainz.org/ws/2/" + url)


def dg(url):
    pace("dg", 1.1)
    sep = "&" if "?" in url else "?"
    return get_json("https://api.discogs.com/" + url + sep + "token=" + DISCOGS_TOKEN)


def wp(lang, **params):
    pace("wp", 0.2)
    params.update(format="json", formatversion=2)
    return get_json(f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params))


# ---------- Википедия ----------

def strip_album(t):
    return re.sub(r"\s*\((?:[^()]*\s)?(?:album|альбом)[^()]*\)\s*$", "", t, flags=re.I)


def via_musicbrainz(artist, title):
    """Самый точный путь: MusicBrainz знает, какая страница Википедии про этот альбом."""
    q = f'releasegroup:"{title}" AND artist:"{first_artist(artist)}"'
    data = mb("release-group/?fmt=json&limit=5&query=" + urllib.parse.quote(q))
    for rg in data.get("release-groups", []):
        credit = " ".join(c.get("name", "") for c in rg.get("artist-credit", []))
        if similar(rg.get("title"), title) < 0.85 or max(similar(credit, artist), similar(credit, first_artist(artist))) < 0.6:
            continue
        rels = mb(f"release-group/{rg['id']}?fmt=json&inc=url-rels").get("relations", [])
        for r in rels:
            res = (r.get("url") or {}).get("resource", "")
            if r.get("type") == "wikidata":
                qid = res.rstrip("/").rsplit("/", 1)[-1]
                pace("wp", 0.2)
                ent = get_json("https://www.wikidata.org/w/api.php?format=json&action=wbgetentities&props=sitelinks&ids=" + qid)
                links = ent.get("entities", {}).get(qid, {}).get("sitelinks", {})
                out = {lang: links[lang + "wiki"]["title"] for lang in ("ru", "en") if lang + "wiki" in links}
                if out:
                    return out
            m = re.match(r"https?://(ru|en)\.wikipedia\.org/wiki/(.+)", res)
            if r.get("type") == "wikipedia" and m:
                return {m.group(1): urllib.parse.unquote(m.group(2)).replace("_", " ")}
        return None
    return None


def via_search(lang, artist, title):
    """Запасной путь: поиск по Википедии, с проверкой, что статья про тот самый альбом."""
    word = "альбом" if lang == "ru" else "album"
    res = wp(lang, action="query", list="search", srsearch=f'"{title}" {first_artist(artist)} {word}', srlimit=5)
    a = norm(first_artist(artist))
    for hit in res.get("query", {}).get("search", []):
        if similar(strip_album(hit["title"]), title) < 0.85:
            continue
        snip = norm(re.sub(r"<[^>]+>", "", hit.get("snippet", "")))
        if word in snip and (a in snip or a in norm(hit["title"])):
            return hit["title"]
    return None


def langlink(lang, page, to):
    r = wp(lang, action="query", prop="langlinks", titles=page, lllang=to, redirects=1)
    pages = r.get("query", {}).get("pages", [])
    ll = pages and pages[0].get("langlinks")
    return ll[0]["title"] if ll else None


def intro(lang, page):
    r = wp(lang, action="query", prop="extracts", exintro=1, explaintext=1, redirects=1, titles=page)
    pages = r.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None, page
    return (pages[0].get("extract") or "").strip(), pages[0]["title"]


def shorten(text):
    text = re.sub(r"\s*\([^()]*(?:англ|произносится|listen|pronounced)[^()]*\)", "", text)
    text = re.sub(r"\s*\[[^\]]*[ˈˌː][^\]]*\]", "", text)   # транскрипции МФА
    text = re.sub(r"\(([^():]*):\s*[;,]\s*", "(", text)                # «(Язык:; перевод)» → «(перевод)»
    text = re.sub(r"\(\s*[;,]\s*", "(", text).replace(" ()", "").replace("()", "")
    out = ""
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        if len(out) + len(para) <= TEXT_MAX:
            out += ("\n" if out else "") + para
            continue
        if not out:   # первый абзац сам длинный — режем по предложению
            cut = para[:TEXT_MAX]
            dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            out = cut[:dot + 1] if dot > 200 else cut.rsplit(" ", 1)[0] + "…"
        break
    return out


# wiki/fix.json — ручные поправки: {"номер": "en:Название статьи"} или {"номер": "none"} (статьи нет)
FIX = load_fix = None


def wiki_for(artist, title, aid=None):
    global FIX
    if FIX is None:
        try:
            FIX = json.loads((WIKI / "fix.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            FIX = {}
    fix = FIX.get(str(aid))
    if fix == "none":
        return None
    if fix:
        lang, page = fix.split(":", 1)
        pages = {lang: page}
        if lang == "en":
            ru = langlink("en", page, "ru")
            if ru:
                pages["ru"] = ru
        for lg in ("ru", "en"):
            if lg in pages:
                text, real = intro(lg, pages[lg])
                if text and len(text) > 80:
                    return {"lang": lg, "text": shorten(text),
                            "url": f"https://{lg}.wikipedia.org/wiki/" + urllib.parse.quote(real.replace(" ", "_"))}
        return None
    pages = first_found(via_musicbrainz, artist, title) or {}
    if not pages:
        t = first_found(lambda a, t: via_search("ru", a, t), artist, title)
        if t:
            pages["ru"] = t
        else:
            t = first_found(lambda a, t: via_search("en", a, t), artist, title)
            if t:
                pages["en"] = t
    if "en" in pages and "ru" not in pages:
        ru = langlink("en", pages["en"], "ru")
        if ru:
            pages["ru"] = ru
    for lang in ("ru", "en"):
        if lang in pages:
            text, real = intro(lang, pages[lang])
            if text and len(text) > 80:
                return {"lang": lang, "text": shorten(text),
                        "url": f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(real.replace(" ", "_"))}
    return None


# ---------- состав из Discogs ----------

SKIP_ROLES = re.compile(r"lacquer|master|design|photo|artwork|art direction|liner|layout|lettering|copyright|phonograph|"
                        r"management|coordinat|executive|a&r|legal|booklet|cover|illustrat|typograph|graphic|transfer|"
                        r"remaster|restor|licens|manufactur|distribut|publish|translat|notes|compiled|pressed|edited by|"
                        r"other|sleeve|painting|logo|concept|tape op|supervis|contractor|written|composed|lyrics|music by|songwriter|"
                        r"words by|adapted|libretto|text by", re.I)
PROD_ROLES = re.compile(r"produc|engineer|recorded|mixed|mixing|arranged|conduct|orchestrat", re.I)
RU_ROLE = [("Slide Guitar", "слайд-гитара"), ("Steel Guitar", "стил-гитара"), ("12-String", "12-струнная гитара"),
           ("Drum Machine", "драм-машина"), ("Drum Programming", "программирование ударных"), ("Brass", "духовые"),
           ("Horns", "духовые"), ("Woodwind", "деревянные духовые"), ("Reeds", "язычковые"), ("Gong", "гонг"),
           ("Celesta", "челеста"), ("Timpani", "литавры"), ("Bells", "колокола"), ("Handclaps", "хлопки"),
           ("Ukulele", "укулеле"), ("Theremin", "терменвокс"), ("Moog", "муг"), ("Clavinet", "клавинет"),
           ("Turntables", "вертушки"), ("Scratches", "скретчи"), ("Rap", "рэп"), ("Concertmaster", "концертмейстер"),
           ("Leader", "руководитель"), ("Directed By", "руководитель"), ("Sound Effects", "эффекты"), ("Mellophone", "меллофон"),
           ("Lead Vocals", "вокал"), ("Backing Vocals", "бэк-вокал"), ("Vocals", "вокал"), ("Voice", "голос"),
           ("Electric Guitar", "электрогитара"), ("Acoustic Guitar", "акустическая гитара"), ("Bass Guitar", "бас-гитара"),
           ("Double Bass", "контрабас"), ("Bass", "бас"), ("Lead Guitar", "соло-гитара"), ("Rhythm Guitar", "ритм-гитара"),
           ("Guitar", "гитара"), ("Drums", "ударные"), ("Percussion", "перкуссия"), ("Keyboards", "клавишные"),
           ("Grand Piano", "рояль"), ("Electric Piano", "электропиано"), ("Piano", "фортепиано"), ("Organ", "орган"),
           ("Synthesizer", "синтезатор"), ("Mellotron", "меллотрон"), ("Electronics", "электроника"),
           ("Programmed By", "программирование"), ("Sampler", "сэмплер"), ("Tenor Saxophone", "тенор-саксофон"),
           ("Alto Saxophone", "альт-саксофон"), ("Baritone Saxophone", "баритон-саксофон"), ("Soprano Saxophone", "сопрано-саксофон"),
           ("Saxophone", "саксофон"), ("Trumpet", "труба"), ("Flugelhorn", "флюгельгорн"), ("Trombone", "тромбон"),
           ("French Horn", "валторна"), ("Tuba", "туба"), ("Clarinet", "кларнет"), ("Bass Clarinet", "бас-кларнет"),
           ("Flute", "флейта"), ("Oboe", "гобой"), ("Bassoon", "фагот"), ("Harmonica", "губная гармошка"),
           ("Violin", "скрипка"), ("Viola", "альт"), ("Cello", "виолончель"), ("Strings", "струнные"), ("Harp", "арфа"),
           ("Banjo", "банджо"), ("Mandolin", "мандолина"), ("Sitar", "ситар"), ("Vibraphone", "вибрафон"),
           ("Marimba", "маримба"), ("Xylophone", "ксилофон"), ("Glockenspiel", "колокольчики"), ("Congas", "конги"),
           ("Bongos", "бонго"), ("Tambourine", "бубен"), ("Accordion", "аккордеон"), ("Harpsichord", "клавесин"),
           ("Choir", "хор"), ("Chorus", "хор"), ("Effects", "эффекты"), ("Tape", "плёнка"), ("Written-By", "автор"),
           ("Written By", "автор"), ("Composed By", "композитор"), ("Lyrics By", "слова"), ("Music By", "музыка"),
           ("Producer", "продюсер"), ("Co-producer", "сопродюсер"), ("Recorded By", "запись"), ("Engineer", "звукорежиссёр"),
           ("Mixed By", "сведение"), ("Arranged By", "аранжировка"), ("Conductor", "дирижёр"), ("Orchestra", "оркестр"),
           ("Featuring", "при участии"), ("Performer", "исполнитель"), ("Instruments", "инструменты"), ("Whistle", "свист")]


def ru_role(role):
    role = re.sub(r"\s*\[[^\]]*\]", "", role).strip()
    for en, ru in RU_ROLE:
        if role.lower() == en.lower():
            return ru
    for en, ru in RU_ROLE:
        if en.lower() in role.lower():
            return ru
    return role.lower()


def clean_name(c):
    return re.sub(r"\s*\(\d+\)$", "", (c.get("anv") or c.get("name") or "").rstrip("*")).strip()


def discogs_release(artist, title):
    for kind in ("master", "release"):
        data = dg("database/search?" + urllib.parse.urlencode(
            {"type": kind, "artist": first_artist(artist), "release_title": title, "per_page": 8}))
        best = None
        for r in data.get("results", []):
            r_artist, _, r_title = (r.get("title") or "").partition(" - ")
            score = similar(r_title, title) + max(similar(r_artist, artist), similar(r_artist, first_artist(artist)))
            if similar(r_title, title) >= 0.85 and score >= 1.5 and (not best or score > best[0]):
                best = (score, r["id"])
        if best:
            if kind == "master":
                return dg(f"masters/{best[1]}").get("main_release")
            return best[1]
    return None


def credits_for(artist, title):
    if not DISCOGS_TOKEN:
        return None
    rid = first_found(discogs_release, artist, title)
    if not rid:
        return None
    rel = dg(f"releases/{rid}")
    people = {}
    entries = list(rel.get("extraartists", []))
    for t in rel.get("tracklist", []):
        entries += t.get("extraartists", [])
    for c in entries:
        name, role = clean_name(c), c.get("role") or ""
        parts = [x for x in re.split(r",(?![^\[]*\])", role) if x.strip() and not SKIP_ROLES.search(x)]
        if not name or not parts:
            continue
        roles = people.setdefault(name, [])
        for part in parts:
            r = "бэк-вокал" if re.search(r"vocals?\s*\[backing", part, re.I) else ru_role(part)
            if r and r not in roles:
                roles.append(r)
    if not people:
        return None
    # сначала музыканты (у кого больше инструментов — выше), потом продюсеры и звукорежиссёры
    prod_ru = ("продюсер", "сопродюсер", "запись", "звукорежиссёр", "сведение", "аранжировка", "дирижёр")

    def key(item):
        play = [r for r in item[1] if r not in prod_ru and not PROD_ROLES.search(r)]
        return (not play, -len(play))
    items = sorted(people.items(), key=key)
    return {"crew": [[n, ", ".join(r[:4])] for n, r in items[:30]], "src": f"https://www.discogs.com/release/{rid}"}


# ---------- перевод английских текстов ----------
# wiki/tr.json: {id: {"src": хэш английского текста, "ru": перевод, "auto": 1 если машинный}}
# Ручные переводы лежат там же; если статья в Википедии изменится, хэш не совпадёт и
# текст переведётся заново.
TR = WIKI / "tr.json"
MM_DAILY = 4500     # MyMemory без ключа даёт ~5000 знаков в сутки


def src_hash(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def mymemory(text):
    """Машинный перевод через бесплатный MyMemory: кусками до 450 знаков по предложениям."""
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+|\n", text):
        if len(cur) + len(sent) + 1 > 450 and cur:
            parts.append(cur)
            cur = ""
        cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    out = []
    for part in parts:
        pace("mm", 1.0)
        r = get_json("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode({"q": part, "langpair": "en|ru"}))
        t = (r.get("responseData") or {}).get("translatedText") or ""
        if r.get("responseStatus") not in (200, "200") or "MYMEMORY WARNING" in t.upper() or not t:
            raise RuntimeError("MyMemory: лимит или ошибка")
        out.append(t)
    return " ".join(out)


def translate_all(packs_all):
    tr = load(TR, {})
    spent = 0
    for k, p in packs_all.items():
        for aid, info in p.items():
            if info.get("lang") != "en" or not info.get("text"):
                info.pop("ru", None)
                continue
            h = src_hash(info["text"])
            t = tr.get(aid)
            if not t or t.get("src") != h:
                if spent + len(info["text"]) > MM_DAILY:
                    continue
                try:
                    t = tr[aid] = {"src": h, "ru": mymemory(info["text"]), "auto": 1}
                    spent += len(info["text"])
                    print(f"  автоперевод: {aid}")
                except Exception as e:
                    print(" ", e)
                    spent = MM_DAILY
                    continue
            info["ru"] = t["ru"]
            if t.get("auto"):
                info["auto"] = 1
            else:
                info.pop("auto", None)
    TR.write_text(json.dumps(tr, ensure_ascii=False, indent=0), encoding="utf-8")


# ---------- проход по архиву ----------

def load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def main():
    args = sys.argv[1:]
    minutes = float(args[args.index("--minutes") + 1]) if "--minutes" in args else 45
    only = {int(x) for x in args[args.index("--only") + 1].split(",")} if "--only" in args else None
    deadline = time.time() + minutes * 60
    WIKI.mkdir(exist_ok=True)
    albums = load(ROOT / "albums.json", [])
    state = load(STATE, {})
    packs = {}

    def pack(k):
        if k not in packs:
            packs[k] = load(WIKI / f"pack-{k}.json", {})
        return packs[k]

    now = time.time()

    def due(a):
        if only:
            return a["id"] in only
        st = state.get(str(a["id"]))
        # «не нашлось» (там время проверки) перепроверяем раз в месяц
        return st is None or (isinstance(st, (int, float)) and now - st > RETRY_DAYS * 86400)

    todo = [a for a in albums if a.get("artist") and not a["title"].startswith("http") and due(a)]
    print(f"К проверке: {len(todo)}")
    done = found = 0
    for a in todo:
        if time.time() > deadline:
            print("Время вышло, продолжу в следующий раз")
            break
        artist, title = a["artist"], a["title"]
        try:
            w = wiki_for(artist, title, a["id"])
            c = credits_for(artist, title)
        except Exception as e:     # сеть: не запоминаем, попробуем в следующий раз
            print(f"  ошибка ({e}): {artist} — {title}")
            continue
        info = {}
        if w:
            info.update(w)
        if c:
            info.update(c)
        p = pack(a["id"] // PACK)
        if info:
            p[str(a["id"])] = info
            state[str(a["id"])] = "ok"
            found += 1
        else:
            p.pop(str(a["id"]), None)
            state[str(a["id"])] = int(time.time())
        done += 1
        print(f"{'+' if info else '-'} {'W' + w['lang'] if w else '  '} {'C' + str(len(c['crew'])) if c else '  '}  {artist} — {title}")
        if done % 20 == 0:
            save(packs, state)
    save(packs, state)
    # переводы: применяем ко всем пакам (и докидываем машинный перевод новым английским текстам)
    for f in WIKI.glob("pack-*.json"):
        pack(int(f.stem.split("-")[1]))
    translate_all(packs)
    save(packs, state)
    print(f"Готово: проверено {done}, со справкой {found}. Всего со справкой: {sum(1 for v in state.values() if v == 'ok')}")


def save(packs, state):
    for k, p in packs.items():
        (WIKI / f"pack-{k}.json").write_text(json.dumps(p, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    STATE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
