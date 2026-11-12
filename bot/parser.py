"""Разбор альбомных постов из канала.

Понимает оба формата:
  💽 Album: RAM                     🎧 Album title: D7-D5 (ссылка на Deezer)
  👤 Artist: ...                    👤 Artist: ...
  📅 Date: 1971                     📅 Release date: 05-11-2026
  🎧 Total tracks: 12               🎵 Total tracks: 6
                                    (+ необязательные Duration / Genre / Label)

Смайлики игнорируются, ищутся только ключевые слова.
"""
import re

FIELDS = {
    "title":    r"^\W*(?:Album(?:\s+title)?|Альбом)\s*:\s*(.+)$",
    "artist":   r"^\W*(?:Artists?|Исполнител[ьи])\s*:\s*(.+)$",
    "date":     r"^\W*(?:(?:Release\s+)?Date|Published\s+on|Дата(?:\s+выхода)?)\s*:\s*(.+)$",
    "tracks":   r"^\W*(?:Total\s+tracks|Количество\s+треков)\s*:\s*(\d+)",
    "duration": r"^\W*Duration\s*:\s*(.+)$",
    "genre":    r"^\W*Genre\s*:\s*(.+)$",
    "label":    r"^\W*Label\s*:\s*(.+)$",
}


def extract_year(date_str):
    """'1971', '05-11-2026', '2026-11-05', '5.11.2026' -> '1971' / '2026'."""
    m = re.search(r"\b(19\d{2}|20\d{2})\b", date_str)
    return m.group(1) if m else None


def flatten_export_text(text):
    """В экспорте Telegram Desktop текст бывает строкой или списком кусков
    вида {"type": "text_link", "text": "D7-D5", "href": "https://..."}.
    Возвращает (обычный текст, {текст ссылки: адрес})."""
    if isinstance(text, str):
        return text, {}
    plain, links = [], {}
    for part in text:
        if isinstance(part, str):
            plain.append(part)
        else:
            plain.append(part.get("text", ""))
            if part.get("href"):
                links[part["text"].strip()] = part["href"]
    return "".join(plain), links


TRACK_LINE = re.compile(r"^\W*(?:Track|Трек)\s*:", re.IGNORECASE | re.MULTILINE)
# запасной вариант: «Iron Butterfly - Heavy (1968)» в первой строке
FREE_FORM = re.compile(r"^\s*(.+?)\s+[-–—]\s+(.+?)\s*[\(\s]((?:19|20)\d{2})\)?(?:\s|$)")


def is_track_post(text):
    """Пост про отдельный трек (DeezLoad и т.п.), а не про альбом."""
    return bool(TRACK_LINE.search(text))


def parse_free_form(text):
    m = FREE_FORM.match(text.split("\n", 1)[0])
    if not m:
        return None
    return {"artist": m.group(1).strip(), "title": m.group(2).strip(),
            "year": m.group(3), "free_form": True}


def parse_post(text, links=None):
    """Возвращает словарь с данными альбома или None, если пост не альбомный.
    Если нет исполнителя или название — голая ссылка, ставит needs_fix=True."""
    links = links or {}
    if is_track_post(text):
        return None
    album = {}
    for key, pattern in FIELDS.items():
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            album[key] = m.group(1).strip()

    if "title" not in album:
        return parse_free_form(text)
    if "artist" not in album:
        album["artist"] = None
        album["needs_fix"] = True
    if re.match(r"https?://", album["title"]):
        album["listen_url"] = album["title"]
        album["needs_fix"] = True

    if "date" in album:
        album["year"] = extract_year(album["date"])
    if "tracks" in album:
        album["tracks"] = int(album["tracks"])

    # ссылка на альбом (Deezer и т.п.), если название было гиперссылкой
    if album["title"] in links and "listen_url" not in album:
        album["listen_url"] = links[album["title"]]

    return album


def looks_almost_like_album(text):
    """Пост не разобрался, но в нём есть хоть одно ключевое слово —
    кандидат на ручную проверку (вдруг формат от другого бота)."""
    return bool(re.search(r"\b(album|artist|tracks|release)\b", text, re.IGNORECASE))


# ---- чистка названий и ключ для сопоставления альбомов ----
_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster|deluxe|anniversary|expanded|edition|mono|stereo|bonus|reissue"
    r"|version|master|\bLP\b|\bEP\b|mix\b)[^\)\]]*[\)\]]", re.I)
# теги релиз-групп: [FLAC], [Vinyl 0-44], [24bit], [WEB] и т.п.
_TAGS = re.compile(r"\s*\[[^\]]*(flac|vinyl|bit|web|mp3|320|lossless|rip)[^\]]*\]", re.I)
_JUNK_TAIL = re.compile(r"\s*-\s*(\d{4}\s+)?remaster(ed)?.*$|\s+\d+(st|nd|rd|th) Anniversary$", re.I)


def clean_title(title):
    """'Abbey Road (Remastered)' -> 'Abbey Road'; '(2011 Remaster)', 🅴 и т.п. тоже убираются."""
    c = _JUNK_TAIL.sub("", _JUNK.sub("", _TAGS.sub("", title or ""))).replace("🅴", "").strip()
    return c or (title or "").strip()


def norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


def album_key(artist, title):
    return norm(artist) + "|" + norm(clean_title(title))
