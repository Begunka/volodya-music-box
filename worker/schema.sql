-- очередь событий Telegram, которые воркер принял, а бот на GitHub ещё не разобрал
CREATE TABLE IF NOT EXISTS queue (update_id INTEGER PRIMARY KEY, body TEXT NOT NULL, received INTEGER NOT NULL);
-- служебные значения (когда последний раз будили GitHub)
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- дуэли «сравни вкусы»: одна дуэль = одно сообщение = один соперник
CREATE TABLE IF NOT EXISTS duels (
  id TEXT PRIMARY KEY, created INTEGER NOT NULL,
  a_id INTEGER NOT NULL, a_name TEXT, ids TEXT NOT NULL, a_ans TEXT NOT NULL,
  b_id INTEGER, b_name TEXT, b_ans TEXT, pct INTEGER, msg TEXT
);
-- оценки: кто (зашифрованный ID), какой альбом, 1 — зашло, -1 — не зашло
CREATE TABLE IF NOT EXISTS votes (uid TEXT NOT NULL, album INTEGER NOT NULL, v INTEGER NOT NULL, PRIMARY KEY (uid, album));
-- готовые счётчики, чтобы не пересчитывать все оценки при каждом открытии архива
CREATE TABLE IF NOT EXISTS counts (album INTEGER PRIMARY KEY, likes INTEGER NOT NULL DEFAULT 0, dislikes INTEGER NOT NULL DEFAULT 0);
-- плейлисты: автор хранится зашифрованным ID и @ником для подписи
CREATE TABLE IF NOT EXISTS playlists (
  id TEXT PRIMARY KEY, uid TEXT NOT NULL, author TEXT, official INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL, descr TEXT, tags TEXT, albums TEXT NOT NULL,
  created INTEGER NOT NULL, updated INTEGER NOT NULL, likes INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_likes (pid TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY (pid, uid));
-- забаненные: не могут публиковать плейлисты, лайкать и играть в дуэли
CREATE TABLE IF NOT EXISTS bans (uid TEXT PRIMARY KEY, author TEXT, at INTEGER NOT NULL);
-- «послушаю позже»: очередь человека и когда напомнить. Здесь настоящий Telegram ID — иначе боту некуда писать
CREATE TABLE IF NOT EXISTS later (
  tg_id INTEGER NOT NULL, album INTEGER NOT NULL, added INTEGER NOT NULL,
  remind_at INTEGER, tries INTEGER NOT NULL DEFAULT 0, done INTEGER, PRIMARY KEY (tg_id, album)
);
CREATE INDEX IF NOT EXISTS later_due ON later (remind_at);
-- кому бот может писать в личку: нажал /start или разрешил в мини-приложении. remind — хочет ли напоминания
CREATE TABLE IF NOT EXISTS people (
  tg_id INTEGER PRIMARY KEY, name TEXT, first INTEGER NOT NULL, dm INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0, remind INTEGER NOT NULL DEFAULT 1, last_dm INTEGER
);
-- статистика: счётчики по дням и «кто был в этот день» (зашифрованный ID) для подсчёта уникальных людей
CREATE TABLE IF NOT EXISTS stats (day TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, key));
CREATE TABLE IF NOT EXISTS seen (day TEXT NOT NULL, kind TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY (day, kind, uid));
-- группы и каналы, куда добавили бота
CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, type TEXT, title TEXT, username TEXT, status TEXT, at INTEGER);
-- пожертвования звёздами (charge нужен, чтобы вернуть)
CREATE TABLE IF NOT EXISTS donations (charge TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, amount INTEGER NOT NULL, at INTEGER NOT NULL, refunded INTEGER NOT NULL DEFAULT 0);
