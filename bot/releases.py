"""Слежение за исполнителями: раз в сутки проверяем, не вышло ли у них нового.

Список исполнителей (только имена, без людей) берём у воркера, новые релизы ищем в MusicBrainz,
новые альбомы в архиве — в albums.json. Находки отдаём воркеру, в личку пишет он.
В репозиторий ничего не коммитим: всё состояние лежит в базе воркера.
"""
import argparse
import calendar
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import enrich as E
from genres import is_various

ROOT = Path(__file__).resolve().parent.parent
TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
WORKER_URL = (os.environ.get("WORKER_URL") or (ROOT / "worker_url.txt").read_text(encoding="utf-8").strip()).rstrip("/")
HOOK_SECRET = hmac.new(TOKEN.encode(), b"webhook", hashlib.sha256).hexdigest()[:32]
MB = "https://musicbrainz.org/ws/2/"
WINDOW_DAYS = 45          # релиз считается новым, если вышел не раньше чем за столько дней до подписки
RETRY_UNKNOWN = 7 * 86400 # исполнителя, которого не нашли в MusicBrainz, ищем заново раз в неделю
# не альбомы: сборники выходят у давно не записывающихся исполнителей без конца, новостью это не считаем
SKIP_SECONDARY = {"Compilation", "Remix", "DJ-mix", "Demo", "Interview", "Audiobook", "Spokenword", "Audio drama", "Field recording", "Mixtape/Street"}


def worker(path, data=None):
    req = urllib.request.Request(WORKER_URL + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {HOOK_SECRET}", "Content-Type": "application/json",
                                          "User-Agent": "volodya-archive-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def mb(path, **params):
    time.sleep(1.1)   # MusicBrainz: не чаще запроса в секунду
    return E.get_json(MB + path + "?" + urllib.parse.urlencode({"fmt": "json", **params}))


def find_mbid(name, albums):
    """Номер исполнителя в MusicBrainz. Надёжнее всего — через его альбом из архива:
    тёзок много, а альбом с таким названием у тёзки вряд ли есть."""
    first = E.first_artist(name)
    for a in albums[:3]:
        try:
            data = mb("release-group/", limit=5, query=f'releasegroup:"{a["title"]}" AND artist:"{first}"')
        except Exception as e:
            print("  MusicBrainz:", e)
            continue
        for rg in data.get("release-groups", []):
            credit = rg.get("artist-credit") or []
            if not credit or E.similar(rg.get("title"), a["title"]) < 0.85:
                continue
            ar = credit[0].get("artist") or {}
            if ar.get("id") and max(E.similar(ar.get("name"), first), E.similar(credit[0].get("name"), first)) >= 0.6:
                return ar["id"]
    try:   # альбом не нашёлся — по имени, но только при точном совпадении
        data = mb("artist/", limit=3, query=f'artist:"{first}" OR alias:"{first}"')
        for ar in data.get("artists", []):
            if ar.get("score", 0) >= 95 and E.norm(ar.get("name")) == E.norm(first):
                return ar["id"]
    except Exception as e:
        print("  MusicBrainz:", e)
    return ""


def has_cover(rid):
    try:
        req = urllib.request.Request(f"https://coverartarchive.org/release-group/{rid}/front-500", method="HEAD", headers={"User-Agent": E.UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def new_releases(mbid, since):
    """Альбомы и EP исполнителя с датой первого выхода не раньше since (и анонсированные на будущее)."""
    q = f'arid:{mbid} AND firstreleasedate:[{since} TO 2100-12-31] AND (primarytype:album OR primarytype:ep) AND status:official'
    out = []
    for rg in mb("release-group/", limit=25, query=q).get("release-groups", []):
        date, secondary = rg.get("first-release-date") or "", set(rg.get("secondary-types") or [])
        credit = rg.get("artist-credit") or []
        # только свои релизы: участие в чужом альбоме или в сборнике разных исполнителей — не новость
        if not date or date < since or secondary & SKIP_SECONDARY or not credit or (credit[0].get("artist") or {}).get("id") != mbid:
            continue
        kind = "live" if "Live" in secondary else "soundtrack" if "Soundtrack" in secondary else "ep" if rg.get("primary-type") == "EP" else "album"
        out.append({"rid": rg["id"], "title": rg.get("title") or "?", "date": date, "type": kind})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--probe", default="", help="проверка без воркера: «Исполнитель|ГГГГ-ММ-ДД» — что найдётся с этой даты")
    args = ap.parse_args()
    if args.probe:
        name, _, since = args.probe.partition("|")
        albums = json.loads((ROOT / "albums.json").read_text(encoding="utf-8"))
        mbid = find_mbid(name, [a for a in albums if E.norm(a.get("artist")) == E.norm(name)])
        print("MusicBrainz:", mbid or "не найден")
        for r in new_releases(mbid, since or "2020-01-01") if mbid else []:
            print(" ", r["date"], r["type"], r["title"], "обложка" if has_cover(r["rid"]) else "без обложки")
        return
    if not TOKEN or not WORKER_URL:
        print("Нет BOT_TOKEN или адреса воркера — выходим")
        return
    deadline = time.time() + args.minutes * 60
    artists = worker("/follow/list").get("artists", [])
    print(f"Исполнителей, за которыми следят: {len(artists)}")
    if not artists:
        return
    albums = json.loads((ROOT / "albums.json").read_text(encoding="utf-8"))
    by_artist = {}
    for a in albums:
        by_artist.setdefault(E.norm(a.get("artist")), []).append(a)
    now = time.time()
    fresh = time.strftime("%Y-%m-%d", time.gmtime(now - WINDOW_DAYS * 86400))
    seen, found = [], []
    # сначала те, кого проверяли давно или не проверяли совсем
    for ar in sorted(artists, key=lambda x: x.get("checked") or 0):
        if time.time() > deadline:
            print("Время вышло, остальные — в следующий раз")
            break
        key, name = ar["key"], ar["name"]
        if is_various(name):
            continue
        ours = by_artist.get(E.norm(name), [])
        # новые альбомы этого исполнителя в архиве (за последние трое суток; повторно воркер не запишет)
        for a in ours:
            try:
                at = calendar.timegm(time.strptime(a.get("added", ""), "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            if now - at < 3 * 86400:
                found.append({"key": key, "rid": f"a{a['id']}", "title": a["title"], "date": str(a.get("year") or time.gmtime(at).tm_year),
                              "type": "album", "album": a["id"], "at": int(at * 1000), "cover": 1})
        mbid = ar.get("mbid") or ""
        if not mbid:
            if ar.get("mbid") == "" and now - (ar.get("checked") or 0) / 1000 < RETRY_UNKNOWN:
                continue   # недавно искали и не нашли
            mbid = find_mbid(name, ours)
            print(f"  {name}: {'нашёл в MusicBrainz' if mbid else 'в MusicBrainz не найден'}")
        seen.append({"key": key, "mbid": mbid})
        if not mbid:
            continue
        try:
            rel = new_releases(mbid, fresh)
        except Exception as e:
            print(f"  {name}: {e}")
            continue
        have = {E.norm(a["title"]) for a in ours}
        for r in rel:
            if E.norm(r["title"]) in have:
                continue   # этот альбом уже в архиве — о нём скажет сообщение «появился в архиве»
            r.update(key=key, cover=1 if has_cover(r["rid"]) else 0)
            found.append(r)
            print(f"  {name}: {r['title']} ({r['date']}, {r['type']})")
    res = worker("/follow/found", {"artists": seen, "releases": found})
    print(f"Проверено: {len(seen)}, найдено релизов: {len(found)}, воркер записал строк: {res.get('saved')}")


if __name__ == "__main__":
    main()
