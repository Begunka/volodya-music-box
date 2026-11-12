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
