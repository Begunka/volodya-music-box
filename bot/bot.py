"""Бот архива Volodya Music Box.

Что делает:
  • новый пост с альбомом в канале → добавляет альбом в архив (год первого выхода
    и жанры через Discogs/MusicBrainz, обложку берёт из поста) и пишет комментарий
    под постом со ссылкой на архив;
  • пост, пересланный ему из канала владельцем → привязывает ссылку на этот пост
    к альбому в архиве (так подтягиваются старые посты), или добавляет альбом,
    если его ещё нет;
  • присылает владельцу сводку и посты, которые не смог разобрать.

Запуск:
  python bot/bot.py           — забрать всё накопившееся и выйти (так запускает GitHub)
  python bot/bot.py --watch   — работать постоянно, пока не закроешь окно
  python bot/bot.py --scan    — один раз прочитать всю историю канала (бот должен
                                 быть админом канала)

Нужны файлы в корне архива (или переменные окружения BOT_TOKEN / DISCOGS_TOKEN):
  bot_token.txt, discogs_token.txt
Только стандартный Python. Если установлен Pillow (pip install pillow),
обложки сжимаются в .webp, иначе сохраняются как .jpg.
"""
import hashlib
import hmac
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parser import parse_post, is_track_post, looks_almost_like_album, album_key, clean_title  # noqa: E402
from enrich import enrich  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "albums.json"        # полный архив
SITE_DATA = ROOT / "data.json"        # облегчённая версия для витрины
COVERS = ROOT / "covers"
RATINGS = ROOT / "ratings.json"       # лайки/дизлайки подписчиков (ID зашифрованы)
STATE = ROOT / "bot_state.json"       # номер последнего обработанного события, владелец

CHANNEL = "volodyamusicbox"
# мини-приложение раздаёт Cloudflare (безлимитный трафик); GitHub Pages — запасная копия
PAGES_URL = "https://volodya-archive.zaharmakaka.workers.dev/"
# Ссылка на мини-приложение. Появится после /newapp в BotFather; пока пусто — в комментарии
# будет ссылка на бота.
MINIAPP = os.environ.get("MINIAPP_LINK", "https://t.me/volodyamusicbox_bot/archive")


def secret(name, filename):
    if os.environ.get(name):
        return os.environ[name].strip()
    p = ROOT / filename
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


TOKEN = secret("BOT_TOKEN", "bot_token.txt")
OWNER_ID = secret("OWNER_ID", "owner_id.txt")            # твой Telegram ID — жёстко, чтобы никто не перехватил
DIARY_CHAT_ID = secret("DIARY_CHAT_ID", "diary_chat_id.txt")
CRONJOB_KEY = secret("CRONJOB_API_KEY", "cronjob_api_key.txt")
# адрес Cloudflare-воркера записывает сам процесс его развёртывания
WORKER_URL = (os.environ.get("WORKER_URL") or
              ((ROOT / "worker_url.txt").read_text(encoding="utf-8").strip() if (ROOT / "worker_url.txt").exists() else ""))
# секрет между Telegram/GitHub и воркером — выводится из токена бота, отдельно хранить не нужно
HOOK_SECRET = hmac.new((TOKEN or "").encode(), b"webhook", hashlib.sha256).hexdigest()[:32]
UPDATE_TYPES = ["message", "channel_post", "inline_query", "chosen_inline_result"]
BOT_USERNAME = f"{CHANNEL}_bot"
DISCOGS = secret("DISCOGS_TOKEN", "discogs_token.txt")
API = f"https://api.telegram.org/bot{TOKEN}/"


# ---------------- Telegram ----------------

class TgError(RuntimeError):
    def __init__(self, method, res):
        super().__init__(f"{method}: {res.get('description')}")
        self.code = res.get("error_code")
        self.description = res.get("description", "")
        self.retry_after = (res.get("parameters") or {}).get("retry_after")


def tg(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(API + method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:   # 400/403/429 — Telegram присылает объяснение в теле ответа
        try:
            res = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise e
    if not res.get("ok"):
        raise TgError(method, res)
    return res["result"]


def say(chat_id, text, **kw):
    try:
        return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True}, **kw)
    except Exception as e:
        print("  не смог отправить сообщение:", e)


def react(chat_id, msg_id, emoji):
    """Ставит реакцию на сообщение — чтобы сразу было видно, что бот пост увидел."""
    try:
        tg("setMessageReaction", chat_id=chat_id, message_id=msg_id,
           reaction=[{"type": "emoji", "emoji": emoji}])
    except Exception as e:
        print("  реакция не поставилась:", e)


def save_cover(raw, album_id):
    """Сжимает картинку в .webp 400px (если есть Pillow), иначе сохраняет как есть."""
    COVERS.mkdir(exist_ok=True)
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(raw)).convert("RGB")
        im.thumbnail((400, 400))
        im.save(COVERS / f"{album_id}.webp", "WEBP", quality=78)
        # маленький .jpg — для инлайн-режима (Telegram показывает превью только из jpg)
        (COVERS / "jpg").mkdir(exist_ok=True)
        im.thumbnail((320, 320))
        im.save(COVERS / "jpg" / f"{album_id}.jpg", "JPEG", quality=80, optimize=True)
        return "webp"
    except ImportError:
        (COVERS / f"{album_id}.jpg").write_bytes(raw)
        return "jpg"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "VolodyaAlbumArchive/0.1 ( https://t.me/volodyamusicbox )"})
    return urllib.request.urlopen(req, timeout=60).read()


def get_cover(photo_sizes, album, album_id):
    """Обложка: из картинки поста, а если её нет — из Discogs / Cover Art Archive / iTunes.
    Возвращает расширение файла или None."""
    if photo_sizes:
        try:
            f = tg("getFile", file_id=photo_sizes[-1]["file_id"])
            return save_cover(fetch(f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}"), album_id)
        except Exception as e:
            print("  обложка из поста не скачалась:", e)
    candidates = list(album.pop("_cover_candidates", []) or [])
    if album.get("artist") and album.get("title"):
        q = urllib.parse.urlencode({"term": f"{album['artist']} {album['title']}", "entity": "album", "limit": 1})
        candidates.append(("itunes", "https://itunes.apple.com/search?" + q))
    for c in candidates:
        try:
            if isinstance(c, tuple):      # iTunes отдаёт ссылку на картинку в ответе поиска
                res = json.loads(fetch(c[1]).decode("utf-8")).get("results") or []
                if not res:
                    continue
                c = res[0]["artworkUrl100"].replace("100x100", "600x600")
            ext = save_cover(fetch(c), album_id)
            if ext:
                return ext
        except Exception as e:
            print("  обложка не нашлась тут:", str(e)[:80])
    return None


# ---------------- архив ----------------

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data, compact=False):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=None if compact else 1,
                              separators=(",", ":") if compact else None), encoding="utf-8")
    tmp.replace(path)


SITE_FIELDS = ["id", "title", "artist", "year", "reissue_year", "tracks", "added", "listen_url",
               "needs_fix", "note", "genres", "styles", "post_url", "cover_ext"]


def rating_counts():
    """{album_id: [лайки, дизлайки]} и оценки автора канала {album_id: 1/-1}."""
    r = load_json(RATINGS, {"users": {}, "author": {}})
    counts = {}
    for votes in r["users"].values():
        for aid, v in votes.items():
            c = counts.setdefault(aid, [0, 0])
            c[0 if v > 0 else 1] += 1
    return counts, r.get("author", {})


def export_site_data(albums):
    counts, author = rating_counts()
    out = []
    for a in albums:
        d = {k: a[k] for k in SITE_FIELDS if a.get(k) not in (None, "", [])}
        c = counts.get(str(a["id"]))
        if c:
            d["likes"], d["dislikes"] = c
        if str(a["id"]) in author:
            d["author"] = author[str(a["id"])]
        if a.get("cover") or a.get("cover_ext"):
            d["has_cover"] = True
        if not a.get("year_checked"):
            d["unverified"] = True
        out.append(d)
    save_json(SITE_DATA, out, compact=True)


class Archive:
    def __init__(self):
        self.albums = load_json(ARCHIVE, [])
        self.by_key = {}
        self.by_link = {a["listen_url"]: a for a in self.albums if a.get("listen_url")}
        for a in self.albums:
            self.by_key[album_key(a.get("artist"), a.get("title"))] = a
            if a.get("title_full"):
                self.by_key.setdefault(album_key(a.get("artist"), a["title_full"]), a)
        self.changed = False

    def find(self, parsed):
        # 1) по исполнителю и названию
        a = self.by_key.get(album_key(parsed.get("artist"), parsed.get("title")))
        if a:
            return a
        # 2) по ссылке (посты, где вместо названия стоит ссылка на Spotify и т.п.)
        if parsed.get("listen_url") and parsed["listen_url"] in self.by_link:
            return self.by_link[parsed["listen_url"]]
        # 3) исполнитель в посте не указан — по одному названию, если оно уникально
        if not parsed.get("artist"):
            t = album_key("", parsed.get("title")).split("|", 1)[1]
            same = [x for k, x in self.by_key.items() if k.split("|", 1)[1] == t]
            if len({id(x) for x in same}) == 1:
                return same[0]
        return None

    def add(self, parsed, date, photo):
        a = dict(parsed)
        full = a["title"]
        a["title"] = clean_title(full)
        if a["title"] != full:
            a["title_full"] = full
        a["id"] = max((x["id"] for x in self.albums), default=0) + 1
        a["added"] = date
        enrich(a, DISCOGS)
        ext = get_cover(photo, a, a["id"])
        a.pop("_cover_candidates", None)
        if ext:
            a["cover_ext"] = ext
        else:
            a["no_cover"] = True
        self.albums.append(a)
        self.by_key[album_key(a.get("artist"), a["title"])] = a
        if a.get("listen_url"):
            self.by_link[a["listen_url"]] = a
        self.changed = True
        return a

    def save(self):
        if self.changed:
            save_json(ARCHIVE, self.albums)
            export_site_data(self.albums)
            self.changed = False


def post_url(msg_id):
    return f"https://t.me/{CHANNEL}/{msg_id}"


def archive_link(album_id=None):
    """Ссылка, которая сразу открывает мини-приложение (на карточке альбома, если указан)."""
    if MINIAPP:
        return MINIAPP + (f"?startapp=album_{album_id}" if album_id else "")
    return f"https://t.me/{BOT_USERNAME}" + (f"?start=album_{album_id}" if album_id else "")


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


# Текст комментария под постом в канале. {total} — сколько альбомов в архиве.
COMMENT_TEXT = "🎧 <b>В архиве больше музыки</b>\nУже {total} альбомов канала — с годами, стилями и обложками."
COMMENT_BUTTON = "🗄 Открыть архив"


def archive_keyboard(album_id=None):
    """Кнопка под полем ввода в личке с ботом. Только из неё мини-приложение может
    отправить оценки боту (так устроен Telegram)."""
    url = PAGES_URL + "?from=kb" + (f"#album-{album_id}" if album_id else "")
    return {"keyboard": [[{"text": "🗄 Архив", "web_app": {"url": url}}]],
            "resize_keyboard": True, "is_persistent": True}


def comment_text(total):
    return COMMENT_TEXT.format(total=total)


def comment_button(album_id=None):
    """Кнопка сразу открывает мини-приложение — на карточке альбома, если он уже в архиве."""
    return {"inline_keyboard": [[{"text": COMMENT_BUTTON, "url": archive_link(album_id)}]]}


PHOTO_WAIT = 15 * 60   # сколько ждать подпись к картинке, присланной без текста


class Bot:
    def __init__(self):
        self.state = load_json(STATE, {})
        self.state.setdefault("offset", 0)
        self.state.setdefault("pending_comments", {})
        self.state.setdefault("pending_photo", None)
        self.state.setdefault("orphans", {})
        self.state.setdefault("group_copies", {})
        self.archive = Archive()
        self.summary = self.empty_summary()

    @staticmethod
    def empty_summary():
        return {"linked": 0, "known": 0, "added": [], "skipped": 0, "failed": [], "foreign": 0,
                "missing": []}

    def owner(self):
        return int(OWNER_ID) if OWNER_ID else self.state.get("owner_id")

    def diary(self):
        return int(DIARY_CHAT_ID) if DIARY_CHAT_ID else self.state.get("diary_chat_id")

    # ---------- канал: только привязка и комментарий ----------
    def on_channel_post(self, m):
        if (m["chat"].get("username") or "").lower() != CHANNEL:
            return
        text = m.get("text") or m.get("caption") or ""
        if not text or is_track_post(text):
            return
        parsed = parse_post(text, links_of(m))
        if not parsed:
            return
        post_id = m["message_id"]
        a = self.archive.find(parsed)
        if a:
            if a.get("post_url") != post_url(post_id):
                a["post_url"] = post_url(post_id)
                self.archive.changed = True
        else:
            # пост вышел раньше, чем альбом попал в дневник — запомним и привяжем потом
            self.state["orphans"][album_key(parsed.get("artist"), parsed.get("title"))] = {"post_id": post_id}
            self.summary["missing"].append(
                f"{parsed.get('artist') or '?'} — {parsed.get('title')} ({post_url(post_id)})")
        # комментарий пишется ответом на копию поста в группе обсуждения.
        # Telegram может прислать эту копию и раньше, и позже самого поста — учитываем оба случая.
        self.state["pending_comments"][str(post_id)] = a["id"] if a else 0
        copy = self.state["group_copies"].pop(str(post_id), None)
        if copy:
            self.comment(copy)

    def comment(self, m):
        origin = m.get("forward_origin") or {}
        post_id = str(origin.get("message_id") or m.get("forward_from_message_id") or "")
        if not post_id:
            return
        if post_id not in self.state["pending_comments"]:
            # пост ещё не обработан (или это трек/мем) — запомним копию на всякий случай
            copies = self.state["group_copies"]
            copies[post_id] = {"chat": {"id": m["chat"]["id"]}, "message_id": m["message_id"],
                               "forward_origin": {"message_id": int(post_id)}}
            for k in sorted(copies, key=int)[:-100]:   # храним только последние 100
                copies.pop(k)
            return
        album_id = self.state["pending_comments"].pop(post_id)
        sent = say(m["chat"]["id"], comment_text(len(self.archive.albums)),
                   reply_parameters={"message_id": m["message_id"]},
                   reply_markup=comment_button(album_id or None))
        if not album_id and sent:
            # запомним, где комментарий: когда альбом появится в архиве, кнопка станет вести прямо на него
            for o in self.state["orphans"].values():
                if str(o.get("post_id")) == post_id:
                    o.update(chat_id=sent["chat"]["id"], comment_id=sent["message_id"])

    def adopt_orphan(self, a):
        """Альбом только что попал в архив — а пост про него в канале уже был?"""
        o = self.state["orphans"].pop(album_key(a.get("artist"), a.get("title")), None)
        if not o:
            return
        a["post_url"] = post_url(o["post_id"])
        self.archive.changed = True
        self.summary["linked"] += 1
        if o.get("comment_id"):
            try:
                tg("editMessageReplyMarkup", chat_id=o["chat_id"], message_id=o["comment_id"],
                   reply_markup=comment_button(a["id"]))
            except Exception as e:
                print("  кнопку в комментарии не обновил:", e)

    # ---------- группы ----------
    def on_group_message(self, m):
        if m.get("is_automatic_forward"):
            self.comment(m)
            return
        uid = (m.get("from") or {}).get("id")
        if uid != self.owner():
            return
        text = m.get("text") or ""
        if text.startswith("/comment"):
            # ручной запасной вариант: ответь «/comment» на пост в группе комментариев
            copy = m.get("reply_to_message") or {}
            pid = str((copy.get("forward_origin") or {}).get("message_id") or "")
            if copy.get("is_automatic_forward") and pid:
                if pid not in self.state["pending_comments"]:
                    parsed = parse_post(copy.get("text") or copy.get("caption") or "")
                    a = self.archive.find(parsed) if parsed else None
                    self.state["pending_comments"][pid] = a["id"] if a else 0
                self.comment(copy)
            try:
                tg("deleteMessage", chat_id=m["chat"]["id"], message_id=m["message_id"])
            except Exception:
                pass
            return
        if text.startswith("/diary"):
            self.state["diary_chat_id"] = m["chat"]["id"]
            say(m["chat"]["id"], "Запомнил: этот чат — дневник. Всё, что сюда кидаешь, попадает в архив.")
            return
        if m["chat"]["id"] == self.diary():
            self.on_diary(m)

    # ---------- чат-дневник: единственный источник архива ----------
    def on_diary(self, m):
        me = (m["chat"]["id"], m["message_id"])
        if "audio" in m or "voice" in m or "document" in m and not m.get("caption"):
            return   # треки и файлы не трогаем
        text = m.get("text") or m.get("caption") or ""
        photo = m.get("photo")

        if photo and not text:
            # картинка без подписи — ждём текст следующим сообщением или ответом на неё
            self.state["pending_photo"] = {"message_id": m["message_id"], "photo": photo, "date": m["date"]}
            return
        if not text or is_track_post(text):
            return

        if not photo:
            reply = m.get("reply_to_message") or {}
            pend = self.state.get("pending_photo")
            if reply.get("photo"):
                photo = reply["photo"]
            elif pend and m["date"] - pend["date"] <= PHOTO_WAIT:
                photo = pend["photo"]
                self.state["pending_photo"] = None

        parsed = parse_post(text, links_of(m))
        if not parsed:
            if looks_almost_like_album(text):
                react(*me, "🤨")
                self.summary["failed"].append(text.split("\n", 1)[0][:60])
            return
        a = self.archive.find(parsed)
        if a:
            self.summary["known"] += 1
            react(*me, "👌")
            return
        a = self.archive.add(parsed, iso(m["date"]), photo)
        self.summary["added"].append(a)
        self.adopt_orphan(a)
        react(*me, "🔥")

    # ---------- оценки из мини-приложения ----------
    def on_ratings(self, m):
        """Мини-приложение присылает пачку оценок: {"r": {"148": 1, "22": -1, "301": 0}}.
        1 — зашло, -1 — не зашло, 0 — снять оценку. ID человека хранится зашифрованным."""
        uid = m["from"]["id"]
        try:
            votes = json.loads(m["web_app_data"]["data"]).get("r", {})
            assert isinstance(votes, dict)
        except Exception:
            return
        known = {str(a["id"]) for a in self.archive.albums}
        r = load_json(RATINGS, {"users": {}, "author": {}})
        r.setdefault("author", {})
        key = hmac.new(TOKEN.encode(), str(uid).encode(), hashlib.sha256).hexdigest()[:16]
        mine = r["users"].setdefault(key, {})
        n = 0
        for aid, v in votes.items():
            aid = str(aid)
            if aid not in known or v not in (1, -1, 0):
                continue
            n += 1
            for store in ([mine, r["author"]] if uid == self.owner() else [mine]):
                if v == 0:
                    store.pop(aid, None)
                else:
                    store[aid] = v
        if not mine:
            r["users"].pop(key, None)
        save_json(RATINGS, r)
        self.archive.changed = True   # пересчитать счётчики на витрине
        say(uid, f"Учёл оценок: {n} 👍👎\nОбщий счёт на витрине обновится через пару минут.",
            reply_markup=archive_keyboard())

    # ---------- личка ----------
    def on_private(self, m):
        uid = m["from"]["id"]
        if m.get("web_app_data"):
            self.on_ratings(m)
            return
        text = m.get("text") or m.get("caption") or ""
        if text.startswith("/start"):
            arg = text.split(maxsplit=1)[1] if " " in text else ""
            if not self.owner() and not arg:
                self.state["owner_id"] = uid
                say(uid, "Готово, ты записан как владелец архива.")
                return
            album_id = arg.split("_", 1)[1] if arg.startswith("album_") else None
            say(uid, "📀 Архив альбомов канала @volodyamusicbox.\n"
                     "Открывай кнопкой «🗄 Архив» внизу — так твои 👍 и 👎 попадут в общий счёт.",
                reply_markup=archive_keyboard(album_id))
            return
        if uid != self.owner():
            return
        origin = m.get("forward_origin") or {}
        chat = origin.get("chat") or m.get("forward_from_chat") or {}
        me = (m["chat"]["id"], m["message_id"])
        if (chat.get("username") or "").lower() != CHANNEL:
            self.summary["foreign"] += 1
            react(*me, "🤔")
            return
        msg_id = origin.get("message_id") or m.get("forward_from_message_id")
        self.process_post(msg_id, text, links_of(m), origin.get("date") or m["date"], m.get("photo"), me)

    def process_post(self, msg_id, text, links, date, photo, me=None):
        """Пост канала с известным номером: привязать к альбому или добавить альбом."""
        if not text or is_track_post(text):
            self.summary["skipped"] += 1
            me and react(*me, "🤷")
            return
        parsed = parse_post(text, links)
        if not parsed:
            self.summary["skipped"] += 1
            if looks_almost_like_album(text):
                self.summary["failed"].append(post_url(msg_id))
                me and react(*me, "🤨")
            else:
                me and react(*me, "🤷")
            return
        a = self.archive.find(parsed)
        if a is None:
            a = self.archive.add(parsed, iso(date), photo)
            self.summary["added"].append(a)
            emoji = "🔥"   # новый альбом в архиве
        elif a.get("post_url") == post_url(msg_id):
            self.summary["known"] += 1
            emoji = "👌"   # уже был привязан
        else:
            emoji = "👍"   # привязал сейчас
        if a.get("post_url") != post_url(msg_id):
            a["post_url"] = post_url(msg_id)
            self.archive.changed = True
            if emoji == "👍":
                self.summary["linked"] += 1
        me and react(*me, emoji)

    def handle(self, u):
        if "channel_post" in u:
            self.on_channel_post(u["channel_post"])
        elif "message" in u:
            m = u["message"]
            if m["chat"]["type"] == "private":
                self.on_private(m)
            elif m["chat"]["type"] in ("group", "supergroup"):
                self.on_group_message(m)

    def report(self):
        """Сводка владельцу в личку — только если случилось что-то важное."""
        s = self.summary
        if not self.owner() or not any(s[k] for k in ("linked", "added", "failed", "foreign", "missing")):
            self.summary = self.empty_summary()
            return
        lines = [f"<b>Архив: {len(self.archive.albums)} альбомов</b>, "
                 f"из них с постом в канале: {sum(1 for a in self.archive.albums if a.get('post_url'))}"]
        if s["added"]:
            lines.append(f"🔥 Добавил: {len(s['added'])}")
            for a in s["added"][:10]:
                mark = "" if a.get("year_checked") else " — год не нашёл, оставил твой"
                if a.get("no_cover"):
                    mark += ", обложку не нашёл"
                lines.append(f"  • {html.escape(a.get('artist') or '?')} — {html.escape(a['title'])} "
                             f"({a.get('year') or '?'}){mark}")
        if s["linked"]:
            lines.append(f"👍 Привязал к постам: {s['linked']}")
        if s["known"]:
            lines.append(f"👌 Уже были: {s['known']}")
        if s["skipped"]:
            lines.append(f"🤷 Не альбомы: {s['skipped']}")
        if s["missing"]:
            lines.append("📭 В канале вышло, а в архиве пока нет — закинь в дневник, и я сам привяжу пост:")
            lines += [f"  • {html.escape(x)}" for x in s["missing"][:10]]
        if s["foreign"]:
            lines.append(f"🤔 Переслано не из канала: {s['foreign']}")
        if s["failed"]:
            lines.append("🤨 Похоже на альбом, но не разобрал:")
            lines += [f"  • {html.escape(u)}" for u in s["failed"][:15]]
        say(self.owner(), "\n".join(lines))
        self.summary = self.empty_summary()

    def poll(self, timeout):
        updates = tg("getUpdates", offset=self.state["offset"], timeout=timeout,
                     allowed_updates=["message", "channel_post"])
        return self.process(updates)

    def process(self, updates):
        # посты канала — первыми, потом всё остальное (копии постов в группе ссылаются на них)
        for u in sorted(updates, key=lambda u: "channel_post" not in u):
            if "inline_query" in u or "chosen_inline_result" in u:
                continue   # на инлайн отвечает воркер; в запасном режиме инлайн просто молчит
            try:
                self.handle(u)
            except Exception as e:
                print("ошибка при обработке события:", e)
            self.state["offset"] = max(self.state["offset"], u["update_id"] + 1)
        if updates:
            self.archive.save()
            save_json(STATE, self.state)
        return len(updates)

    # ---------- Cloudflare: основной режим, GitHub: запасной ----------
    def worker(self, path, data=None):
        req = urllib.request.Request(WORKER_URL.rstrip("/") + path,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Authorization": f"Bearer {HOOK_SECRET}",
                                              "Content-Type": "application/json",
                                              "User-Agent": "volodya-archive-bot"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def worker_alive(self):
        if not WORKER_URL:
            return False
        try:
            return self.worker("/health").get("ok") is True
        except urllib.error.HTTPError as e:
            print("Cloudflare не отвечает:", e, e.read()[:200])
            return False
        except Exception as e:
            print("Cloudflare не отвечает:", e)
            return False

    def fast_alarm(self, on):
        """Включить/выключить быстрый будильник cron-job.org («… быстрый», раз в 3 минуты)."""
        if not CRONJOB_KEY:
            return
        def api(method, path, body=None):
            req = urllib.request.Request("https://api.cron-job.org" + path, method=method,
                                         data=json.dumps(body).encode() if body else None,
                                         headers={"Authorization": f"Bearer {CRONJOB_KEY}",
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        try:
            job = self.state.get("fast_job_id")
            if not job:
                jobs = api("GET", "/jobs").get("jobs", [])
                job = next((j["jobId"] for j in jobs if "быстр" in (j.get("title") or "").lower()), None)
                self.state["fast_job_id"] = job
            if job:
                api("PATCH", f"/jobs/{job}", {"job": {"enabled": on}})
        except Exception as e:
            print("cron-job.org:", e)

    def ensure_mode(self):
        """Решает, кто сейчас принимает сообщения от Telegram, и переключает при необходимости.
        Возвращает "worker" или "polling"."""
        alive = self.worker_alive()
        info = tg("getWebhookInfo")
        hook = WORKER_URL.rstrip("/") + "/tg" if WORKER_URL else ""
        mode = self.state.get("mode")
        if alive:
            if info.get("url") != hook or sorted(info.get("allowed_updates") or []) != sorted(UPDATE_TYPES):
                tg("setWebhook", url=hook, secret_token=HOOK_SECRET, allowed_updates=UPDATE_TYPES,
                   max_connections=10)
            if mode != "worker":
                self.fast_alarm(False)
                if mode == "polling" and self.owner():
                    say(self.owner(), "☁️ Cloudflare снова в строю — вернул основной режим, инлайн работает.")
            self.state["mode"] = "worker"
        else:
            if info.get("url"):
                tg("deleteWebhook", drop_pending_updates=False)
            if mode != "polling":
                self.fast_alarm(True)
                if mode == "worker" and self.owner():
                    say(self.owner(), "⚠️ Cloudflare не отвечает (наверное, кончился лимит на сутки). "
                                      "Перешёл на запасной режим через GitHub: всё работает, "
                                      "только инлайн-список молчит. Вернусь сам, когда он оживёт.")
            self.state["mode"] = "polling"
        return self.state["mode"]

    def run_once(self):
        total = 0
        mode = self.ensure_mode() if WORKER_URL else "polling"
        if mode == "worker":
            # забираем очередь, которую накопил воркер; повторяем, пока не опустеет
            for _ in range(20):
                batch = self.worker("/queue").get("updates", [])
                if not batch:
                    break
                total += self.process(batch)
                self.worker("/ack", {"max_id": max(u["update_id"] for u in batch)})
        else:
            while True:
                n = self.poll(timeout=0)
                total += n
                if n == 0:
                    break
        self.report()
        save_json(STATE, self.state)
        print(f"Режим: {mode}. Обработано событий: {total}")

    def scan_channel(self):
        """Читает ВСЮ историю канала по номерам постов: бот пересылает себе в личку
        владельца пост №1, №2, … (без звука), читает текст и сразу удаляет копию.
        Работает и для постов, которые в канал были пересланы от других ботов.

        Треки после альбомного поста не читаются по одному: если в посте
        «Total tracks: 12», бот перепрыгивает сразу через 12 номеров и проверяет
        только, не перепрыгнул ли лишнего (вдруг треков выложено меньше).

        Нужно: бот — админ канала, владелец нажал /start."""
        owner = self.owner()
        if not owner:
            sys.exit("Сначала нажми /start у бота, чтобы он знал, куда пересылать.")
        known = [int(a["post_url"].rsplit("/", 1)[1]) for a in self.archive.albums if a.get("post_url")]
        last_known = max(known, default=0)
        pos = self.state.get("scan_pos", 1)
        misses, seen = 0, 0

        def peek(n):
            """Переслать пост №n себе, прочитать, удалить. None — такого номера нет."""
            while True:
                try:
                    fwd = tg("forwardMessage", chat_id=owner, from_chat_id="@" + CHANNEL,
                             message_id=n, disable_notification=True)
                    break
                except TgError as e:
                    if e.code == 429:   # слишком часто — Telegram просит подождать
                        print(f"  Telegram просит паузу {e.retry_after or 5} с")
                        time.sleep((e.retry_after or 5) + 1)
                        continue
                    d = e.description.lower()
                    if "not found" in d or "can't be forwarded" in d:
                        return None
                    raise
                except (OSError, urllib.error.URLError) as e:
                    # обрыв связи / провайдер сбросил соединение — ждём и пробуем тот же пост снова
                    print(f"  сеть: {e} — пробую снова через 10 с")
                    time.sleep(10)
            try:
                tg("deleteMessage", chat_id=owner, message_id=fwd["message_id"])
            except Exception:
                pass
            time.sleep(1.05)   # не чаще раза в секунду в один чат
            return fwd

        def is_track(m):
            return ("audio" in m or "voice" in m
                    or (m.get("document") or {}).get("mime_type", "").startswith("audio"))

        def handle(n, m):
            if is_track(m):
                return None
            text = m.get("text") or m.get("caption") or ""
            date = (m.get("forward_origin") or {}).get("date") or m["date"]
            self.process_post(n, text, links_of(m), date, m.get("photo"))
            parsed = parse_post(text) if text else None
            return parsed

        print(f"Сканирую канал с поста №{pos}. Остановить можно Ctrl+C — потом продолжит с того же места.")
        try:
            while True:
                m = peek(pos)
                if m is None:
                    misses += 1
                    if pos > last_known and misses > 300:
                        break   # дальше постов уже нет
                    pos += 1
                    continue
                misses = 0
                seen += 1
                parsed = handle(pos, m)
                tracks = (parsed or {}).get("tracks") or 0
                if tracks > 1:
                    target = pos + tracks + 1          # первый номер после последнего трека
                    # не перепрыгнули ли лишнего? идём назад, пока не упрёмся в трек
                    back = target - 1
                    while back > pos:
                        mb = peek(back)
                        if mb is None:                 # удалённое сообщение — смотрим дальше назад
                            back -= 1
                            continue
                        if is_track(mb):
                            break
                        handle(back, mb)               # это был пост, а не трек — учли его
                        back -= 1
                    pos = target
                else:
                    pos += 1
                if seen % 25 == 0:
                    print(f"  дошёл до поста №{pos}…")
                    self.archive.save()
                    self.state["scan_pos"] = pos
                    save_json(STATE, self.state)
        except KeyboardInterrupt:
            print("Остановлено.")
        except Exception as e:
            print("Неожиданная ошибка, прогресс сохранён:", e)
        self.state["scan_pos"] = pos
        self.archive.save()
        save_json(STATE, self.state)
        self.report()
        print(f"Готово, дошёл до поста №{pos}.")

    def watch(self):
        print("Бот работает. Пересылай посты; закрыть — Ctrl+C.")
        try:
            me = tg("getMe")
            print(f"Подключился как @{me['username']}")
        except Exception as e:
            print("Не могу достучаться до Telegram:", e)
        idle = 0
        while True:
            try:
                n = self.poll(timeout=25)
                if n:
                    print(f"получил {n} сообщ.")
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("сеть:", e, "— пробую снова через 5 с")
                time.sleep(5)
                continue
            # сводку шлём, когда поток пересылок затих
            idle = 0 if n else idle + 1
            if idle == 1:
                self.report()
        self.archive.save()
        save_json(STATE, self.state)


def links_of(m):
    """{текст: ссылка} из форматирования сообщения (название альбома со ссылкой на Deezer и т.п.)."""
    text = m.get("text") or m.get("caption") or ""
    ents = m.get("entities") or m.get("caption_entities") or []
    # смещения в Telegram считаются в UTF-16
    u16 = text.encode("utf-16-le")
    links = {}
    for e in ents:
        if e["type"] == "text_link":
            part = u16[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le").strip()
            links[part] = e["url"]
    return links


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Нет токена бота: положи его в bot_token.txt рядом с albums.json")
    bot = Bot()
    if "--scan" in sys.argv:
        bot.scan_channel()
    elif "--watch" in sys.argv:
        bot.watch()
    else:
        bot.run_once()
