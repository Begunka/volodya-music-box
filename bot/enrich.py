"""Поиск года первого выхода (Discogs + MusicBrainz) и жанров для одного альбома.
Общий код для бота и разового скрипта fix_metadata.py."""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

UA = "VolodyaAlbumArchive/0.1 ( https://t.me/volodyamusicbox )"


def get_json(url, tries=5):
    """Запрос с повторами: MusicBrainz отвечает 503, когда его просят слишком часто
    или он перегружен. Тогда ждём (2, 4, 8, 16 с) и пробуем снова."""
    delay = 2
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        print(f"  сервер занят, жду {delay} с…")
        time.sleep(delay)
        delay *= 2


def norm(s):
    # без диакритики: «Pétrouchka» и «Petrouchka» — одно и то же
    s = "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower()) if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", s).strip()


def similar(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def first_artist(artist):
    return re.split(r",|&| and | feat\.? ", artist or "", maxsplit=1)[0].strip()


def lookup_year(artist, title):
    """Год первого выхода альбома по MusicBrainz или None."""
    q = f'releasegroup:"{title}" AND artist:"{first_artist(artist)}"'
    url = "https://musicbrainz.org/ws/2/release-group/?fmt=json&limit=5&query=" + urllib.parse.quote(q)
    data = get_json(url)
    best = None
    for rg in data.get("release-groups", []):
        credit = " ".join(c.get("name", "") for c in rg.get("artist-credit", []))
        t_sim = similar(rg.get("title"), title)
        a_sim = max(similar(credit, artist), similar(credit, first_artist(artist)))
        date = rg.get("first-release-date") or ""
        if t_sim >= 0.85 and a_sim >= 0.6 and re.match(r"\d{4}", date):
            score = t_sim + a_sim
            if not best or score > best[0]:
                best = (score, date[:4], rg.get("title"), rg.get("id"))
    return best


DISCOGS_TOKEN = None  # выставляет enrich() из настроек


def _dg_artist(r):
    """У Discogs title = «Исполнитель - Альбом»; у тёзок в имени хвост «(2)», у вариантов имени — «*»."""
    r_artist, _, r_title = (r.get("title") or "").partition(" - ")
    return re.sub(r"\s*\(\d+\)|\*", "", r_artist).strip(), r_title


def lookup_discogs(artist, title):
    """Год первого выхода по Discogs (master release) + жанры/стили.
    Возвращает (score, year, title, genres, styles, cover) или None."""
    best = tagged = None
    for kind in ("master", "release"):
        params = urllib.parse.urlencode({
            "type": kind, "artist": first_artist(artist), "release_title": title,
            "per_page": 10, "token": DISCOGS_TOKEN})
        data = get_json("https://api.discogs.com/database/search?" + params)
        for r in data.get("results", []):
            r_artist, r_title = _dg_artist(r)
            t_sim = similar(r_title, title)
            a_sim = max(similar(r_artist, artist), similar(r_artist, first_artist(artist)))
            if t_sim < 0.85 or a_sim < 0.6:
                continue
            score = t_sim + a_sim
            # жанры берём у самого похожего результата, у которого они размечены полностью:
            # у отдельных изданий стили часто пустые, хотя у соседнего издания того же альбома они есть
            if r.get("style") and (not tagged or score > tagged[0] + 0.01):
                tagged = (score, r.get("genre", []), r.get("style", []))
            year = str(r.get("year") or "")
            if re.fullmatch(r"\d{4}", year) and year != "0":
                # среди одинаково похожих берём самый ранний год
                if not best or score > best[0] + 0.01 or (abs(score - best[0]) <= 0.01 and year < best[1]):
                    best = (score, year, r_title, r.get("genre", []), r.get("style", []), r.get("cover_image"))
        time.sleep(1.1)  # лимит Discogs: 60 запросов в минуту
        if best and (best[4] or tagged):
            break
    if best and not best[4] and tagged:
        best = best[:3] + (tagged[1] or best[3], tagged[2]) + best[5:]
    return best


try:
    from unidecode import unidecode
except ImportError:          # без библиотеки просто не будет транслитерации
    def unidecode(x):
        return x


def latin(s):
    """Латиница вместо любой письменности: «คนกับควาย» → «khnkabkhway», «Кино» → «kino»."""
    return norm(unidecode(s or ""))


def skeleton(s):
    """Согласные без пробелов: разные латинские записи одного названия
    («Kon Gub Kwai» и «Khon Kap Khwai») сходятся так лучше, чем целиком."""
    x = re.sub(r"[aeiouyh\s]", "", latin(s)).translate(str.maketrans("gbdvzqc", "kptwskk"))
    return re.sub(r"(.)\1+", r"\1", x)


def title_match(ours, theirs):
    """Насколько название из базы похоже на наше. У Discogs часто два написания:
    «คนกับควาย = Kon Kab Kwai», поэтому сравниваем и с каждой частью."""
    parts = [theirs] + [x for x in re.split(r"\s*=\s*|\s*[()\[\]]\s*", theirs or "") if x.strip()]
    best = 0
    for x in parts:
        best = max(best, similar(x, ours), SequenceMatcher(None, latin(x), latin(ours)).ratio())
        # совпали все согласные (с точностью до звонких/глухих) — то же название в другой записи
        a, b = skeleton(x), skeleton(ours)
        if len(a) >= 5 and a == b:
            best = max(best, 0.9)
    return best


_disco = {}


def discography(artist):
    """Все альбомы исполнителя с годами: Discogs (основная роль) + MusicBrainz.
    Список (название, год). Нужен, когда поиск по названию ничего не дал:
    название в посте записано не так, как в базе (другая письменность, транслит)."""
    key = norm(artist)
    if key in _disco:
        return _disco[key]
    out = []
    try:   # у сборников («Разные исполнители» на любом языке) дискографии нет
        from genres import is_various
        if is_various(artist):
            _disco[key] = out
            return out
    except ImportError:
        pass
    names = [first_artist(artist)] + artist_variants(artist)[:2]
    if DISCOGS_TOKEN:
        for name in names:
            try:
                q = urllib.parse.urlencode({"type": "artist", "q": name, "per_page": 5, "token": DISCOGS_TOKEN})
                data = get_json("https://api.discogs.com/database/search?" + q)
                time.sleep(1.1)
                ids = [r["id"] for r in data.get("results", [])
                       if similar(re.sub(r"\s*\(\d+\)", "", r.get("title", "")), name) >= 0.9][:2]
                for aid in ids:
                    q = urllib.parse.urlencode({"sort": "year", "sort_order": "asc", "per_page": 100, "token": DISCOGS_TOKEN})
                    rel = get_json(f"https://api.discogs.com/artists/{aid}/releases?" + q)
                    time.sleep(1.1)
                    out += [(r.get("title", ""), str(r["year"])) for r in rel.get("releases", [])
                            if r.get("role") == "Main" and re.fullmatch(r"\d{4}", str(r.get("year") or "")) and str(r["year"]) != "0"]
                if ids:
                    break
            except Exception as e:
                print("  Discogs (дискография):", e)
    for name in names[:2]:
        try:
            time.sleep(1.1)
            data = get_json("https://musicbrainz.org/ws/2/artist/?fmt=json&limit=2&query=" +
                            urllib.parse.quote(f'artist:"{name}" OR alias:"{name}"'))
            ar = [a for a in data.get("artists", []) if a.get("score", 0) >= 95][:1]
            if not ar:
                continue
            time.sleep(1.1)
            rg = get_json(f"https://musicbrainz.org/ws/2/release-group?fmt=json&limit=100&artist={ar[0]['id']}")
            out += [(g.get("title", ""), g["first-release-date"][:4]) for g in rg.get("release-groups", [])
                    if re.match(r"\d{4}", g.get("first-release-date") or "")]
            break
        except Exception as e:
            print("  MusicBrainz (дискография):", e)
    # в базах встречаются мусорные годы («0029») — оставляем только правдоподобные
    out = [(t, y) for t, y in out if 1900 <= int(y) <= time.gmtime().tm_year + 1]
    _disco[key] = out
    return out


def lookup_discography(artist, title):
    """Альбом в дискографии исполнителя: (score, год, название) с самым ранним годом
    среди уверенно похожих названий, или None."""
    best = None
    for t, y in discography(artist):
        sc = title_match(title, t)
        if sc >= 0.85 and (not best or y < best[1]):
            best = (sc, y, t)
    return best


def spotify_title(url):
    data = get_json("https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe=""))
    return data.get("title")




_variants = {}


def artist_variants(artist):
    """Другие написания исполнителя по MusicBrainz: Aquarium → Аквариум, Kino → Кино и т.п.
    Базы часто знают группу только под родным именем, поэтому, если поиск
    по имени из поста ничего не дал, пробуем эти варианты."""
    key = norm(artist)
    if key in _variants:
        return _variants[key]
    out = []
    try:
        name = first_artist(artist).replace('"', "")
        time.sleep(1.1)
        data = get_json("https://musicbrainz.org/ws/2/artist/?fmt=json&limit=3&query=" +
                        urllib.parse.quote(f'artist:"{name}" OR alias:"{name}"'))
        for ar in data.get("artists", []):
            if ar.get("score", 0) < 90:
                continue
            names = [ar.get("name", ""), ar.get("sort-name", "")] + [x.get("name", "") for x in ar.get("aliases", [])]
            if not any(similar(n, name) >= 0.9 for n in names):
                continue
            for n in names:
                if n and norm(n) != key and norm(n) not in map(norm, out) and "," not in n:
                    out.append(n)
    except Exception as e:
        print("  MusicBrainz (имена):", e)
    # кириллица вперёд: если в посте латиница, то не нашлось именно из-за неё
    out.sort(key=lambda n: not re.search("[а-яё]", n, re.I))
    _variants[key] = out[:3]
    return _variants[key]


def first_found(fn, artist, title):
    """fn(artist, title) по имени из поста, потом по другим написаниям."""
    r = fn(artist, title)
    if r:
        return r
    for v in artist_variants(artist):
        r = fn(v, title)
        if r:
            print(f"  нашлось как «{v}»")
            return r
    return None


def enrich(album, discogs_token=None, albums=None):
    """Дополняет album: год первого выхода, reissue_year, genres, styles.
    albums — весь архив: по нему жанры подбираются, когда базы про сам альбом молчат.
    Возвращает True, если год подтверждён хотя бы одной базой."""
    global DISCOGS_TOKEN
    DISCOGS_TOKEN = discogs_token
    title, artist = album.get("title"), album.get("artist")
    if not title or not artist or title.startswith("http"):
        return False
    dg = mb = None
    if discogs_token:
        try:
            dg = first_found(lookup_discogs, artist, title)
        except Exception as e:
            print("  Discogs:", e)
    try:
        mb = first_found(lookup_year, artist, title)
    except Exception as e:
        print("  MusicBrainz:", e)
    if dg and dg[3] and dg[4]:
        album["genres"], album["styles"] = dg[3], dg[4]
        album["genre_src"] = "discogs"
    else:
        if dg and dg[3]:
            album["genres"] = dg[3]
        from genres import fill_genres
        fill_genres(album, albums or [], discogs_token, mb_id=mb[3] if mb else None, skip_exact=bool(discogs_token))
    # откуда можно взять обложку, если в посте её нет
    covers = []
    if dg and len(dg) > 5 and dg[5] and "spacer.gif" not in dg[5]:
        covers.append(dg[5])
    if mb and len(mb) > 3 and mb[3]:
        covers.append(f"https://coverartarchive.org/release-group/{mb[3]}/front-500")
    album["_cover_candidates"] = covers
    found = [f[1] for f in (dg, mb) if f]
    album.pop("year_suspect", None)
    if not found:
        # сам альбом не нашёлся — ищем его в дискографии исполнителя
        try:
            ds = lookup_discography(artist, title)
        except Exception as e:
            ds = None
            print("  дискография:", e)
        if ds:
            print(f"  нашлось в дискографии: «{ds[2]}», {ds[1]}")
            album["year_src"] = "discography"
            found = [ds[1]]
        else:
            # не нашёлся и там: если исполнитель выпускался задолго до года из поста,
            # год из поста скорее всего от переиздания — отмечаем для владельца
            years = [y for _, y in discography(artist)]
            post = album.get("year")
            if years and post and str(post).isdigit() and int(post) - int(min(years)) >= 15:
                album["year_suspect"] = min(years)
            return False
    post_year = album.get("year")
    year = min(found + ([post_year] if post_year else []))
    if post_year and int(post_year) > int(year):
        album["reissue_year"] = post_year
    album["year"] = year
    album["year_checked"] = True
    return True
