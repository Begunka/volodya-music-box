-- очередь событий Telegram, которые воркер принял, а бот на GitHub ещё не разобрал
CREATE TABLE IF NOT EXISTS queue (update_id INTEGER PRIMARY KEY, body TEXT NOT NULL, received INTEGER NOT NULL);
-- служебные значения (когда последний раз будили GitHub)
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
