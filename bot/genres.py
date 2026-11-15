"""Жанры и стили: каскад источников, чтобы альбом не оставался без жанра.

Порядок (от точного к грубому), останавливаемся на первом, кто дал и жанр, и стиль:
  1. Discogs — точное название (делает enrich), потом другие написания названия и общий поиск;
  2. MusicBrainz — жанры релиз-группы;
  3. Deezer, iTunes — крупный жанр («Rock», «Jazz»), стилей там нет;
  4. исполнитель — другие его альбомы в архиве, его релизы в Discogs, его жанры в MusicBrainz;
  5. Gemini (только если в секретах есть GEMINI_API_KEY) — последний шанс для совсем неизвестного;
  6. genres_fix.json в корне — ручные поправки, сильнее всего остального.

Откуда взято, пишется в album["genre_src"]. Всё, что не «discogs» и не «manual», бот
раз в месяц перепроверяет по Discogs: базы пополняются.

Запуск вручную (так делает workflow genres.yml):
  python bot/genres.py --minutes 12            — альбомы без жанра/стиля
  python bot/genres.py --only 5,43 --minutes 5  — только эти номера
"""
import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import enrich as E  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "genres_fix.json"
WEAK = ("mb", "deezer", "itunes", "title", "artist", "ai")   # такие источники стоит перепроверять

DISCOGS_GENRES = ["Rock", "Electronic", "Pop", "Jazz", "Folk, World, & Country", "Funk / Soul", "Hip Hop",
                  "Classical", "Latin", "Blues", "Reggae", "Stage & Screen", "Non-Music", "Brass & Military",
                  "Children's"]

# как стиль называется в Discogs, если в другой базе он назван иначе
ALIASES = {
    "progressive rock": "Prog Rock", "prog rock": "Prog Rock", "avant garde": "Avantgarde", "avant-garde": "Avantgarde",
    "avant garde jazz": "Free Jazz", "synthpop": "Synth-pop", "synth pop": "Synth-pop", "electropop": "Synth-pop",
    "rock and roll": "Rock & Roll", "rock n roll": "Rock & Roll", "rhythm and blues": "Rhythm & Blues",
    "r b": "Rhythm & Blues", "rnb": "Rhythm & Blues", "post rock": "Post Rock", "post punk": "Post-Punk",
    "jazz fusion": "Fusion", "jazz rock": "Jazz-Rock", "trip hop": "Trip Hop", "lo fi": "Lo-Fi", "lofi": "Lo-Fi",
    "drum and bass": "Drum n Bass", "drum n bass": "Drum n Bass", "idm": "IDM", "ebm": "EBM",
    "musique concrete": "Musique Concrète", "bossa nova": "Bossa Nova", "j pop": "J-pop", "k pop": "K-pop",
    "city pop": "City Pop", "art pop": "Art Pop", "baroque pop": "Baroque Pop", "sunshine pop": "Sunshine Pop",
    "psychedelic pop": "Psychedelic Pop", "psychedelic folk": "Psychedelic Folk", "psych folk": "Psychedelic Folk",
    "space rock": "Space Rock", "krautrock": "Krautrock", "kraut rock": "Krautrock", "canterbury scene": "Prog Rock",
    "zeuhl": "Prog Rock", "symphonic prog": "Symphonic Rock", "symphonic rock": "Symphonic Rock",
    "singer songwriter": "Singer/Songwriter", "new wave": "New Wave", "no wave": "No Wave", "noise rock": "Noise Rock",
    "garage rock": "Garage Rock", "surf rock": "Surf", "surf": "Surf", "exotica": "Exotica", "lounge": "Lounge",
    "easy listening": "Easy Listening", "soundtrack": "Soundtrack", "film score": "Score", "score": "Score",
    "video game music": "Video Game Music", "video game": "Video Game Music", "chiptune": "Chiptune",
    "modern classical": "Modern", "contemporary classical": "Contemporary", "minimalism": "Minimal",
    "minimal": "Minimal", "medieval": "Medieval", "renaissance": "Renaissance", "baroque": "Baroque",
    "romantic": "Romantic", "opera": "Opera", "choral": "Choral", "big band": "Big Band", "swing": "Swing",
    "bebop": "Bop", "bop": "Bop", "hard bop": "Hard Bop", "cool jazz": "Cool Jazz", "free jazz": "Free Jazz",
    "spiritual jazz": "Free Jazz", "free improvisation": "Free Improvisation", "soul jazz": "Soul-Jazz",
    "jazz funk": "Jazz-Funk", "acid jazz": "Acid Jazz", "dream pop": "Dream Pop", "shoegaze": "Shoegaze",
    "indie rock": "Indie Rock", "indie pop": "Indie Pop", "alternative rock": "Alternative Rock",
    "art rock": "Art Rock", "pop rock": "Pop Rock", "folk rock": "Folk Rock", "hard rock": "Hard Rock",
    "blues rock": "Blues Rock", "psychedelic rock": "Psychedelic Rock", "psychedelic": "Psychedelic Rock",
    "psychedelia": "Psychedelic Rock", "acid rock": "Acid Rock", "experimental rock": "Experimental",
    "experimental": "Experimental", "ambient": "Ambient", "dark ambient": "Dark Ambient", "drone": "Drone",
    "noise": "Noise", "industrial": "Industrial", "techno": "Techno", "acid techno": "Acid", "acid": "Acid",
    "acid house": "Acid House", "house": "House", "trance": "Trance", "psytrance": "Psy-Trance",
    "psychedelic trance": "Psy-Trance", "goa trance": "Goa Trance", "breakbeat": "Breakbeat", "breakcore": "Breakcore",
    "downtempo": "Downtempo", "electro": "Electro", "disco": "Disco", "funk": "Funk", "soul": "Soul",
    "gospel": "Gospel", "spoken word": "Spoken Word", "poetry": "Poetry", "field recording": "Field Recording",
    "sound collage": "Sound Collage", "plunderphonics": "Sound Collage", "vaporwave": "Vaporwave",
    "new age": "New Age", "folk": "Folk", "contemporary folk": "Folk", "country": "Country", "bluegrass": "Bluegrass",
    "celtic": "Celtic", "chanson": "Chanson", "schlager": "Schlager", "vocal": "Vocal", "ballad": "Ballad",
    "europop": "Europop", "britpop": "Brit Pop", "brit pop": "Brit Pop", "power pop": "Power Pop",
    "punk": "Punk", "punk rock": "Punk", "hardcore punk": "Hardcore", "emo": "Emo", "grunge": "Grunge",
    "heavy metal": "Heavy Metal", "black metal": "Black Metal", "doom metal": "Doom Metal", "math rock": "Math Rock",
    "post hardcore": "Post-Hardcore", "ska": "Ska", "dub": "Dub", "reggae": "Reggae", "dancehall": "Dancehall",
    "samba": "Samba", "mpb": "MPB", "tropicalia": "MPB", "salsa": "Salsa", "tango": "Tango", "flamenco": "Flamenco",
    "afrobeat": "Afrobeat", "highlife": "Highlife", "raga": "Indian Classical", "hindustani classical": "Indian Classical",
    "enka": "Enka", "kayokyoku": "Kayōkyoku", "group sounds": "Beat", "beat": "Beat", "merseybeat": "Beat",
    "boom bap": "Boom Bap", "trap": "Trap", "cloud rap": "Cloud Rap", "abstract hip hop": "Abstract",
    "instrumental hip hop": "Instrumental", "instrumental": "Instrumental", "comedy": "Comedy", "parody": "Parody",
    "novelty": "Novelty", "musical": "Musical", "theme": "Theme", "library music": "Library Music",
    "bard": "Bard", "bard music": "Bard", "russian rock": "Pop Rock", "via": "Pop Rock", "space age pop": "Space-Age",
    "space age": "Space-Age", "glam rock": "Glam", "glam": "Glam", "southern rock": "Southern Rock",
    "soft rock": "Soft Rock", "yacht rock": "Soft Rock", "aor": "AOR", "electronica": "Leftfield",
    "folktronica": "Leftfield", "glitch": "Glitch", "hyperpop": "Hyperpop", "phonk": "Phonk", "funk carioca": "Baile Funk",
    "baile funk": "Baile Funk", "brazilian phonk": "Phonk", "world": "", "pop": "", "rock": "", "jazz": "",
    "electronic": "", "classical": "", "hip hop": "", "blues": "", "latin": "",
}
# слово в названии стиля → жанр Discogs (если архив сам не подсказал)
PARENT_HINTS = [
    (r"hip hop|rap\b|trap|boom bap|phonk", "Hip Hop"),
    (r"jazz|bop\b|swing|big band|fusion|dixieland|free improvisation", "Jazz"),
    (r"soundtrack|score|musical|theme|video game", "Stage & Screen"),
    (r"baroque$|romantic|classical|opera|choral|medieval|renaissance|modern$|contemporary$", "Classical"),
    (r"techno|house|ambient|electro|idm|trance|synth|drone|noise$|industrial|downtempo|breakbeat|breakcore|glitch"
     r"|vaporwave|leftfield|acid$|chiptune|new age|drum n bass|musique|experimental|hyperpop|ebm|trip hop", "Electronic"),
    (r"soul|funk|disco|rhythm & blues|gospel", "Funk / Soul"),
    (r"reggae|dub$|ska|dancehall", "Reggae"),
    (r"bossa|samba|mpb|salsa|tango|latin|baile", "Latin"),
    (r"blues", "Blues"),
    (r"folk|country|bluegrass|celtic|flamenco|afrobeat|highlife|indian|enka|bard|chanson|field recording", "Folk, World, & Country"),
    (r"spoken|poetry|comedy|parody|novelty", "Non-Music"),
    (r"pop\b|schlager|ballad|vocal|easy listening|lounge|exotica|kay", "Pop"),
    (r"rock|punk|metal|grunge|shoegaze|beat$|surf|psychedel|new wave|emo|glam|aor|hardcore|garage", "Rock"),
]
# крупные жанры Deezer и iTunes → (жанры Discogs, стили)
BROAD = {
    "rock": (["Rock"], []), "alternative": (["Rock"], ["Alternative Rock"]), "indie rock": (["Rock"], ["Indie Rock"]),
    "indie pop": (["Rock", "Pop"], ["Indie Pop"]), "indie pop/folk": (["Rock", "Pop"], ["Indie Pop"]),
    "indie rock/rock pop": (["Rock"], ["Indie Rock"]), "hard rock": (["Rock"], ["Hard Rock"]),
    "metal": (["Rock"], ["Heavy Metal"]), "pop": (["Pop"], []), "international pop": (["Pop"], []),
    "rap/hip hop": (["Hip Hop"], []), "hip-hop/rap": (["Hip Hop"], []), "hip hop": (["Hip Hop"], []),
    "electro": (["Electronic"], []), "electronic": (["Electronic"], []), "dance": (["Electronic"], []),
    "techno/house": (["Electronic"], ["Techno"]), "chill out/trip-hop/lounge": (["Electronic"], ["Downtempo"]),
    "dubstep": (["Electronic"], ["Dubstep"]), "jazz": (["Jazz"], []), "instrumental jazz": (["Jazz"], []),
    "vocal jazz": (["Jazz"], ["Vocal"]), "classical": (["Classical"], []), "opera": (["Classical"], ["Opera"]),
    "modern": (["Classical"], ["Modern"]), "baroque": (["Classical"], ["Baroque"]),
    "films/games": (["Stage & Screen"], ["Soundtrack"]), "film scores": (["Stage & Screen"], ["Score"]),
    "game scores": (["Stage & Screen"], ["Video Game Music"]), "soundtrack": (["Stage & Screen"], ["Soundtrack"]),
    "tv soundtracks": (["Stage & Screen"], ["Soundtrack"]), "musicals": (["Stage & Screen"], ["Musical"]),
    "anime": (["Stage & Screen"], ["Soundtrack"]), "r&b": (["Funk / Soul"], ["Rhythm & Blues"]),
    "r&b/soul": (["Funk / Soul"], ["Soul"]), "soul & funk": (["Funk / Soul"], []), "disco": (["Funk / Soul"], ["Disco"]),
    "folk": (["Folk, World, & Country"], ["Folk"]), "singer/songwriter": (["Folk, World, & Country", "Pop"], []),
    "singer & songwriter": (["Folk, World, & Country", "Pop"], []), "country": (["Folk, World, & Country"], ["Country"]),
    "world": (["Folk, World, & Country"], []), "african music": (["Folk, World, & Country"], ["African"]),
    "asian music": (["Folk, World, & Country"], []), "indian music": (["Folk, World, & Country"], ["Indian Classical"]),
    "reggae": (["Reggae"], []), "blues": (["Blues"], []), "latin music": (["Latin"], []), "latin": (["Latin"], []),
    "brazilian music": (["Latin"], []), "kids": (["Children's"], []), "children's music": (["Children's"], []),
    "j-pop": (["Pop"], ["J-pop"]), "k-pop": (["Pop"], ["K-pop"]), "new age": (["Electronic"], ["New Age"]),
    "easy listening": (["Pop"], ["Easy Listening"]), "vocal": (["Pop"], ["Vocal"]), "comedy": (["Non-Music"], ["Comedy"]),
    "spoken word": (["Non-Music"], ["Spoken Word"]), "русская поп-музыка": (["Pop"], []), "russian": (["Pop"], []),
    "французский поп": (["Pop"], ["Chanson"]), "french pop": (["Pop"], ["Chanson"]),
}


def nkey(s):
    return re.sub(r"[^a-z0-9а-яё]+", " ", E.norm(s)).strip()


def title_variants(title):
    """Написания названия, под которыми альбом может лежать в базах:
    без хвоста в скобках, без «Композитор: », без «3 - », половинки «A / B»."""
    out = [title]
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title).strip()
    out.append(t)
    if ":" in t:
        out.append(t.split(":", 1)[1].strip())
    out.append(re.sub(r"^\d+\s*[-–.]\s*", "", t))
    out.append(re.sub(r",?\s*(Pts?|Vol|Part)\.?\s*[\d\-–IVX]+.*$", "", t, flags=re.I).strip())
    if " / " in t:
        out += [p.strip() for p in t.split(" / ")]
    seen, res = set(), []
    for v in out:
        k = nkey(v)
        if len(k) >= 2 and k not in seen:
            seen.add(k)
            res.append(v)
    return res


def _base(t):
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", t or "").strip() or (t or "")


def close(a, b, floor=0.85):
    """Похожи ли два названия: почти одинаковые (хвост в скобках не в счёт)
    или одно целиком внутри другого."""
    ka, kb = nkey(a), nkey(b)
    if not ka or not kb:
        return False
    if E.similar(a, b) >= floor or E.similar(_base(a), _base(b)) >= max(floor, 0.9):
        return True
    short, long_ = sorted((ka, kb), key=len)
    return (len(short) >= 8 or len(short) * 5 >= len(long_) * 3) and len(short) >= 5 and f" {short} " in f" {long_} "


def artist_close(found, artist, floor=0.6):
    names = [artist, E.first_artist(artist)] + E._variants.get(E.norm(artist), [])
    return any(E.similar(found, n) >= floor or close(found, n, 0.8) for n in names if n)


class Vocab:
    """Словарь стилей Discogs, собранный из архива: как стиль пишется и под каким жанром живёт."""

    def __init__(self, albums):
        self.styles, co = {}, {}
        for a in albums:
            if a.get("genre_src") in WEAK:
                continue
            for s in a.get("styles") or []:
                self.styles[nkey(s)] = s
                for g in a.get("genres") or []:
                    co.setdefault(s, Counter())[g] += 1
        self.parent = {s: c.most_common(1)[0][0] for s, c in co.items()}

    def style(self, name, curated=False):
        """Имя из чужой базы → стиль Discogs. curated — источник с выверенным списком жанров
        (ему верим и на незнакомые названия), иначе берём только известное."""
        k = nkey(name)
        if not k:
            return None
        if k in ALIASES:
            return ALIASES[k] or None
        if k in self.styles:
            return self.styles[k]
        if curated and 3 <= len(k) <= 30:
            return " ".join(w.capitalize() for w in name.replace("-", " ").split())
        return None

    def genre_of(self, style):
        if style in self.parent:
            return self.parent[style]
        low = style.lower()
        for rx, g in PARENT_HINTS:
            if re.search(rx, low):
                return g
        return None

    def from_names(self, names, curated=False, limit=4):
        """Список названий из чужой базы → (жанры, стили) в словаре Discogs."""
        genres, styles = [], []
        for n in names:
            k = nkey(n)
            for g in DISCOGS_GENRES:   # «rock», «jazz», «hip hop» — это жанр, а не стиль
                if nkey(g) == k or (k == "folk" and g.startswith("Folk")) or (k in ("funk", "soul") and g.startswith("Funk")):
                    if g not in genres:
                        genres.append(g)
            if k in BROAD:
                for g in BROAD[k][0]:
                    if g not in genres:
                        genres.append(g)
            s = self.style(n, curated)
            if s and s not in styles and len(styles) < limit:
                styles.append(s)
        for s in styles:
            g = self.genre_of(s)
            if g and g not in genres:
                genres.append(g)
        return genres[:3], styles


# ---------------- источники ----------------

def discogs_loose(artist, title):
    """Discogs по другим написаниям названия и общим поиском. Год не нужен — только жанры."""
    if not E.DISCOGS_TOKEN:
        return None
    base = "https://api.discogs.com/database/search?"
    tries = []
    for v in title_variants(title)[1:4]:
        tries.append({"type": "master", "artist": E.first_artist(artist), "release_title": v})
    tries.append({"type": "release", "artist": E.first_artist(artist), "release_title": title_variants(title)[-1]})
    tries.append({"q": f"{E.first_artist(artist)} {title_variants(title)[min(1, len(title_variants(title)) - 1)]}", "type": "release"})
    best = None
    for params in tries:
        params.update(per_page=15, token=E.DISCOGS_TOKEN)
        data = E.get_json(base + urllib.parse.urlencode(params))
        time.sleep(1.1)
        for r in data.get("results", []):
            r_artist, r_title = E._dg_artist(r)
            if not r.get("genre") or not artist_close(r_artist, artist):
                continue
            if not any(close(r_title, v) for v in title_variants(title)):
                continue
            score = E.similar(r_title, title) + (0.5 if r.get("style") else 0)
            if not best or score > best[0]:
                best = (score, r.get("genre", []), r.get("style", []))
        if best and best[2]:
            break
    return (best[1], best[2]) if best else None


def mb_get(path):
    time.sleep(1.1)   # MusicBrainz: один запрос в секунду
    return E.get_json("https://musicbrainz.org/ws/2/" + path)


def mb_names(obj):
    """Жанры (выверенный список MusicBrainz) и теги (что угодно от пользователей) по убыванию голосов."""
    genres = [g["name"] for g in sorted(obj.get("genres") or [], key=lambda g: -g.get("count", 0))]
    tags = [t["name"] for t in sorted(obj.get("tags") or [], key=lambda t: -t.get("count", 0)) if t.get("count", 0) > 0]
    return genres, tags


def mb_release_group(artist, title, mb_id, vocab):
    if not mb_id:
        for v in title_variants(title)[:3]:
            q = 'releasegroup:"%s" AND artist:"%s"' % (v.replace('"', ""), E.first_artist(artist).replace('"', ""))
            data = mb_get("release-group/?fmt=json&limit=5&query=" + urllib.parse.quote(q))
            for rg in data.get("release-groups", []):
                credit = " ".join(c.get("name", "") for c in rg.get("artist-credit", []))
                if close(rg.get("title"), v) and artist_close(credit, artist):
                    mb_id = rg["id"]
                    break
            if mb_id:
                break
    if not mb_id:
        return None
    genres, tags = mb_names(mb_get(f"release-group/{mb_id}?fmt=json&inc=genres+tags"))
    g, s = vocab.from_names(genres, curated=True)
    if not s:
        g2, s = vocab.from_names(tags)
        g = g or g2
    return (g, s) if g or s else None


def mb_artist(artist, vocab):
    name = E.first_artist(artist).replace('"', "")
    data = mb_get("artist/?fmt=json&limit=3&query=" + urllib.parse.quote(f'artist:"{name}" OR alias:"{name}"'))
    for ar in data.get("artists", []):
        names = [ar.get("name", "")] + [x.get("name", "") for x in ar.get("aliases", [])]
        if ar.get("score", 0) < 90 or not any(E.similar(n, name) >= 0.9 for n in names):
            continue
        genres, tags = mb_names(mb_get(f"artist/{ar['id']}?fmt=json&inc=genres+tags"))
        g, s = vocab.from_names(genres, curated=True, limit=2)
        if not s:
            g2, s = vocab.from_names(tags, limit=2)
            g = g or g2
        if g or s:
            return g, s
    return None


def deezer(album, vocab):
    artist, title = album["artist"], album["title"]
    ids = []
    m = re.search(r"deezer\.com/(?:\w+/)?album/(\d+)", album.get("listen_url") or "")
    if m:
        ids.append(m.group(1))
    else:
        for v in title_variants(title)[:2]:
            q = 'artist:"%s" album:"%s"' % (E.first_artist(artist).replace('"', ""), v.replace('"', ""))
            data = E.get_json("https://api.deezer.com/search/album?limit=5&q=" + urllib.parse.quote(q), tries=2)
            for r in data.get("data", []):
                if close(r.get("title"), v) and artist_close((r.get("artist") or {}).get("name", ""), artist):
                    ids.append(str(r["id"]))
                    break
            if ids:
                break
    for i in ids:
        d = E.get_json(f"https://api.deezer.com/album/{i}", tries=2)
        names = [g.get("name", "") for g in ((d.get("genres") or {}).get("data") or [])]
        g, s = broad(names, vocab)
        if g:
            return g, s
    return None


def itunes(album, vocab):
    artist, title = album["artist"], album["title"]
    q = urllib.parse.urlencode({"term": f"{E.first_artist(artist)} {title_variants(title)[-1]}", "entity": "album", "limit": 5})
    data = E.get_json("https://itunes.apple.com/search?" + q, tries=2)
    time.sleep(3)   # iTunes: около 20 запросов в минуту
    for r in data.get("results", []):
        if any(close(r.get("collectionName"), v) for v in title_variants(title)) and artist_close(r.get("artistName", ""), artist):
            g, s = broad([r.get("primaryGenreName", "")], vocab)
            if g:
                return g, s
    return None


def broad(names, vocab):
    genres, styles = [], []
    for n in names:
        gs, ss = BROAD.get(n.strip().lower(), (None, None))
        if gs is None:
            gs, ss = vocab.from_names([n])
        for g in gs:
            if g not in genres:
                genres.append(g)
        for s in ss:
            if s not in styles:
                styles.append(s)
    return genres[:3], styles[:3]


VARIOUS = re.compile(r"^(various|various artists|va|v/a|разные исполнители|сборник|verschiedene interpreten|"
                     r"różni wykonawcy|varios artistas|artistes divers|artisti vari)$", re.I)


def is_various(artist):
    """«Разные исполнители» на любом языке — по такому «исполнителю» жанр не угадать."""
    return bool(VARIOUS.match(E.first_artist(artist or "").strip()))


def from_title(title):
    """Саундтрек видно по названию: «… (Original Game Soundtrack)», «… OST»."""
    t = title.lower()
    if not re.search(r"soundtrack|\bost\b|саундтрек|サウンドトラック|original score|motion picture", t):
        return None
    game = re.search(r"game|игр", t)
    return ["Stage & Screen"], (["Soundtrack", "Video Game Music"] if game else ["Soundtrack"])


def from_archive(album, albums):
    """Другие альбомы того же исполнителя в архиве: что у них общего."""
    me = nkey(E.first_artist(album["artist"]))
    same = [a for a in albums if a is not album and a.get("artist") and nkey(E.first_artist(a["artist"])) == me
            and a.get("styles") and a.get("genre_src") not in WEAK]
    if not same:
        return None
    gc, sc = Counter(), Counter()
    for a in same:
        gc.update(a.get("genres") or [])
        sc.update(a.get("styles") or [])
    n = len(same)
    genres = [g for g, c in gc.most_common(3) if c * 2 >= n] or [gc.most_common(1)[0][0]]
    styles = [s for s, c in sc.most_common(3) if c * 3 >= n] or [sc.most_common(1)[0][0]]
    return genres, styles


def discogs_artist(artist):
    """Релизы исполнителя в Discogs: самые частые жанры и стили."""
    if not E.DISCOGS_TOKEN:
        return None
    names = [E.first_artist(artist)] + E.artist_variants(artist)[:1]
    for name in names:
        params = urllib.parse.urlencode({"type": "release", "artist": name, "per_page": 50, "token": E.DISCOGS_TOKEN})
        data = E.get_json("https://api.discogs.com/database/search?" + params)
        time.sleep(1.1)
        gc, sc, n = Counter(), Counter(), 0
        for r in data.get("results", []):
            r_artist, _ = E._dg_artist(r)
            if E.similar(r_artist, name) < 0.85 or not r.get("genre"):
                continue
            n += 1
            gc.update(r.get("genre") or [])
            sc.update(r.get("style") or [])
        if n:
            genres = [g for g, c in gc.most_common(3) if c * 5 >= n * 2] or [gc.most_common(1)[0][0]]
            styles = [s for s, c in sc.most_common(3) if c * 4 >= n]
            if not styles and sc:
                styles = [sc.most_common(1)[0][0]]
            return genres, styles
    return None


_gemini_model = None


def gemini(album, vocab):
    """Последний шанс: спросить модель. Работает, только если задан GEMINI_API_KEY."""
    global _gemini_model
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    import urllib.request
    api = "https://generativelanguage.googleapis.com/v1beta/"

    def call(path, body=None):
        req = urllib.request.Request(api + path, data=json.dumps(body).encode() if body else None,
                                     headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8"))

    if not _gemini_model:
        _gemini_model = os.environ.get("GEMINI_MODEL", "").strip()
    if not _gemini_model:   # названия моделей меняются — берём самую свежую бесплатную «flash» из списка
        models = [m["name"].split("/", 1)[1] for m in call("models?pageSize=200").get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [m for m in models if re.fullmatch(r"gemini-[\d.]+-flash(-lite)?", m)]
        flash.sort(key=lambda m: (float(re.search(r"[\d.]+", m).group().rstrip(".")), "lite" not in m), reverse=True)
        _gemini_model = (flash or models or ["gemini-flash-latest"])[0]
    prompt = (
        "Определи жанр музыкального альбома по классификации Discogs.\n"
        f"Исполнитель: {album['artist']}\nАльбом: {album['title']}\nГод: {album.get('year') or '?'}\n\n"
        f"genres — 1–2 значения строго из списка: {', '.join(DISCOGS_GENRES)}.\n"
        "styles — 1–3 стиля Discogs (например Psychedelic Rock, Ambient, Hard Bop, Soundtrack).\n"
        "Если альбома не знаешь — суди по исполнителю. Если не знаешь и исполнителя и по названиям ничего "
        'нельзя понять, верни {"unknown": true}. Не выдумывай.\n'
        'Ответ — только JSON: {"genres": [...], "styles": [...]}')
    res = call(f"models/{_gemini_model}:generateContent", {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
    time.sleep(4)   # бесплатный тариф: считанные запросы в минуту
    text = res["candidates"][0]["content"]["parts"][0]["text"]
    d = json.loads(text)
    genres = [g for g in d.get("genres") or [] if g in DISCOGS_GENRES][:2]
    styles = [s for s in (vocab.style(x, curated=True) for x in d.get("styles") or []) if s][:3]
    return (genres, styles) if genres else None


# ---------------- каскад ----------------

def load_fix():
    try:
        return json.loads(FIX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fill_genres(album, albums, discogs_token=None, mb_id=None, skip_exact=False, log=print):
    """Заполняет album["genres"], album["styles"], album["genre_src"]. Возвращает источник или None.
    skip_exact — точный поиск по Discogs только что сделал enrich, повторять не надо."""
    E.DISCOGS_TOKEN = discogs_token
    title, artist = album.get("title"), album.get("artist")
    fix = load_fix().get(str(album.get("id")))
    if fix:
        album["genres"], album["styles"], album["genre_src"] = fix.get("genres", []), fix.get("styles", []), "manual"
        return "manual"
    if not title or not artist or title.startswith("http"):
        return None
    vocab = Vocab(albums)
    genres, styles, src = list(album.get("genres") or []), list(album.get("styles") or []), None

    def take(name, fn):
        """Пробует источник; дополняет то, чего ещё нет. True — есть и жанр, и стиль."""
        nonlocal genres, styles, src
        try:
            r = fn()
        except Exception as e:
            log(f"  {name}: {str(e)[:80]}")
            return False
        if not r:
            return False
        g, s = r
        if g and not genres:
            genres, src = list(g), src or name
        if s and not styles:
            styles = list(s)
            if name in ("discogs", "mb") or not src:
                src = name
            for st in styles:   # стиль пришёл без жанра — жанр выводим из стиля
                if not genres and vocab.genre_of(st):
                    genres = [vocab.genre_of(st)]
        return bool(genres and styles)

    def exact():
        r = E.first_found(E.lookup_discogs, artist, title)
        return (r[3], r[4]) if r else None

    steps = []
    if discogs_token and not skip_exact:
        steps.append(("discogs", exact))
    if discogs_token:
        steps.append(("discogs", lambda: discogs_loose(artist, title)))
    steps += [
        ("mb", lambda: mb_release_group(artist, title, mb_id, vocab)),
        ("deezer", lambda: deezer(album, vocab)),
        ("itunes", lambda: itunes(album, vocab)),
        ("title", lambda: from_title(title)),
    ]
    if not is_various(artist):
        steps += [
            ("artist", lambda: from_archive(album, albums)),
            ("artist", lambda: discogs_artist(artist)),
            ("artist", lambda: mb_artist(artist, vocab)),
        ]
    steps.append(("ai", lambda: gemini(album, vocab)))
    for name, fn in steps:
        if name in ("deezer", "itunes") and genres:
            continue   # крупный жанр уже есть, а стилей там не бывает
        if take(name, fn):
            break
    if genres:
        album["genres"] = genres
    if styles:
        album["styles"] = styles
    if genres or styles:
        album["genre_src"] = src or "artist"
        return album["genre_src"]
    return None


def needs_genres(a):
    return bool(a.get("artist")) and not (a.get("title") or "").startswith("http") and \
        (not a.get("genres") or not a.get("styles"))


def main():
    import bot as B   # тот же формат файлов и экспорт, что у бота
    minutes = float(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 12
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    token = os.environ.get("DISCOGS_TOKEN", "").strip() or None
    albums = B.load_json(B.ARCHIVE, [])
    fix = load_fix()
    now, deadline = int(time.time()), time.time() + minutes * 60
    todo = [a for a in albums if (str(a["id"]) in only if only else
            (needs_genres(a) and now - a.get("genre_tried", 0) > 6 * 3600) or
            (str(a["id"]) in fix and a.get("genre_src") != "manual"))]
    print(f"Без жанра или стиля: {sum(1 for a in albums if needs_genres(a))}, беру в работу: {len(todo)}")
    done = 0
    for a in todo:
        if time.time() > deadline:
            print("Время вышло, остальное — в следующий запуск.")
            break
        before = (a.get("genres"), a.get("styles"))
        src = fill_genres(a, albums, token)
        a["genre_tried"] = now
        if a.get("genres") and a.get("styles"):
            a.pop("genre_tried", None)
        done += 1
        mark = "=" if (a.get("genres"), a.get("styles")) == before else "+"
        print(f"{mark} №{a['id']} {a['artist']} — {a['title']}: {src or 'ничего'} · "
              f"{', '.join(a.get('genres') or []) or '—'} · {', '.join(a.get('styles') or []) or '—'}")
        if done % 10 == 0:
            B.save_json(B.ARCHIVE, albums)
    B.save_json(B.ARCHIVE, albums)
    B.export_site_data(albums)
    left = [a for a in albums if needs_genres(a)]
    print(f"Обработано: {done}. Осталось без жанра или стиля: {len(left)}; совсем без жанра: "
          f"{sum(1 for a in left if not a.get('genres'))}")
    for a in left:
        if not a.get("genres"):
            print(f"  нет жанра: №{a['id']} {a['artist']} — {a['title']}")


if __name__ == "__main__":
    main()
