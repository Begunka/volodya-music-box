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
