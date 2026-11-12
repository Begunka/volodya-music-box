// Входная дверь бота @volodyamusicbox_bot на Cloudflare Workers.
//  • инлайн-запросы (@volodyamusicbox_bot …) — отвечает сразу сам;
//  • всё остальное складывает в очередь (D1) и будит бота на GitHub,
//    который делает тяжёлую работу: архив, Discogs, обложки, сайт.
// Если воркер недоступен (например, кончился дневной лимит), бот на GitHub сам
// переключает Telegram на запасной режим и потом обратно.

const DISPATCH_EVERY = 30;   // будить GitHub не чаще, чем раз в N секунд

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    env.BOT_TOKEN = (env.BOT_TOKEN || "").trim();   // на случай лишнего пробела/переноса в секрете
    const secret = await hookSecret(env.BOT_TOKEN);

    if (url.pathname === "/health") {
      if (!authed(req, secret)) return new Response("wrong key", { status: 403 });
      await env.DB.prepare("SELECT 1").first();
      return json({ ok: true });
    }

    // ----- Telegram присылает события сюда -----
    if (url.pathname === "/tg" && req.method === "POST") {
      if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) return new Response("no", { status: 403 });
      const u = await req.json();
      if (u.chosen_inline_result) {
        ctx.waitUntil(chosen(u.chosen_inline_result, env).catch(e => console.log("chosen", e)));
        return new Response("ok");
      }
      if (u.inline_query) {
        ctx.waitUntil(inline(u.inline_query, env).catch(e => console.log("inline", e)));
        return new Response("ok");
      }
      if (!worthQueuing(u, env)) return new Response("ok");   // треки, болтовня в комментариях — боту не нужны
      await env.DB.prepare("INSERT OR IGNORE INTO queue (update_id, body, received) VALUES (?, ?, ?)")
        .bind(u.update_id, JSON.stringify(u), Date.now()).run();
      ctx.waitUntil(wakeGithub(env));
      return new Response("ok");
    }

    // ----- мини-приложение: общий счёт лайков (публичный, кэш 30 секунд) -----
    if (url.pathname === "/api/counts") return publicCounts(req, env, ctx);
    if (url.pathname === "/api/pl" && req.method === "GET") return publicPlaylists(req, env, ctx);
    const one = url.pathname.match(/^\/api\/pl\/([a-f0-9]{8})$/);
    if (one && req.method === "GET") {
      const p = await env.DB.prepare("SELECT * FROM playlists WHERE id = ?").bind(one[1]).first();
      return json(p && !p.hidden ? plView(p) : { error: "not found" });
    }

    // ----- мини-приложение: дуэли, оценки -----
    if (url.pathname.startsWith("/api/")) return api(req, url, env, ctx);

    // ----- бот на GitHub забирает очередь -----
    if (url.pathname === "/queue") {
      if (!authed(req, secret)) return new Response("no", { status: 403 });
      const { results } = await env.DB.prepare("SELECT body FROM queue ORDER BY update_id LIMIT 200").all();
      return json({ updates: results.map(r => JSON.parse(r.body)) });
    }
    if (url.pathname === "/ack" && req.method === "POST") {
      if (!authed(req, secret)) return new Response("no", { status: 403 });
      const { max_id } = await req.json();
      await env.DB.prepare("DELETE FROM queue WHERE update_id <= ?").bind(max_id).run();
      return json({ ok: true });
    }
    return new Response("Volodya Music Box", { status: 200 });
  },
};

// В очередь (и, значит, в запуск GitHub) попадает только то, с чем бот реально работает
function worthQueuing(u, env) {
  const isAudio = m => m && (m.audio || m.voice || (m.document && !m.caption));
  if (u.channel_post) return !isAudio(u.channel_post);
  const m = u.message;
  if (!m) return false;
  if (m.chat.type === "private") {
    // от тебя — всё (пересылки, команды); от остальных — только /start и отправка оценок, прочий текст выкидываем
    if (String(m.from && m.from.id) === String(env.OWNER_ID || "")) return true;
    return !!m.web_app_data || /^\/start\b/.test(m.text || "");
  }
  if (m.is_automatic_forward) return !isAudio(m);                   // копия поста — для комментария
  return String(m.from && m.from.id) === String(env.OWNER_ID || ""); // в группах — только твои сообщения (дневник, /comment)
}

async function hmacHex(key, msg, len) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, len);
}

// {альбом: [лайки, дизлайки]} и оценки автора канала. Кэшируется на 30 секунд,
// так что сколько бы людей ни открыло архив, база читается пару раз в минуту.
async function publicCounts(req, env, ctx) {
  const cache = caches.default, key = new Request(new URL("/api/counts", req.url).toString());
  const hit = await cache.match(key);
  if (hit) return hit;
  const c = {}, a = {};
  const { results } = await env.DB.prepare("SELECT album, likes, dislikes FROM counts WHERE likes > 0 OR dislikes > 0").all();
  results.forEach(r => c[r.album] = [r.likes, r.dislikes]);
  if (env.OWNER_ID) {
    const owner = await hmacHex(env.BOT_TOKEN, String(env.OWNER_ID), 16);
    const mine = await env.DB.prepare("SELECT album, v FROM votes WHERE uid = ?").bind(owner).all();
    mine.results.forEach(r => a[r.album] = r.v);
  }
  const res = new Response(JSON.stringify({ c, a }), { headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=30" } });
  ctx.waitUntil(cache.put(key, res.clone()));
  return res;
}

// ---------------- плейлисты ----------------
const PL = { min: 5, max: 50, title: 60, descr: 300, tags: 5, perUser: 5, every: 30 * 60 * 1000, stale: 60 * 86400000 };

function plView(p, extra) {
  return { id: p.id, author: p.author, official: !!p.official, title: p.title, descr: p.descr || "",
    tags: JSON.parse(p.tags || "[]"), albums: JSON.parse(p.albums), likes: p.likes, updated: p.updated, hidden: !!p.hidden, ...(extra || {}) };
}

// общий список: от Володи — все, от людей — живые (обновлялись или лайкались за 2 месяца). Кэш на минуту.
async function publicPlaylists(req, env, ctx) {
  const cache = caches.default, key = new Request(new URL("/api/pl", req.url).toString());
  const hit = await cache.match(key);
  if (hit) return hit;
  const { results } = await env.DB.prepare("SELECT * FROM playlists WHERE hidden = 0 ORDER BY updated DESC").all();
  const now = Date.now();
  const official = results.filter(p => p.official).map(p => plView(p));
  const people = results.filter(p => !p.official && (now - p.updated < PL.stale || p.likes > 0))
    .map(p => plView(p))
    .sort((a, b) => (b.likes - a.likes) || (b.updated - a.updated));
  const res = new Response(JSON.stringify({ official, people }), { headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=60" } });
  ctx.waitUntil(cache.put(key, res.clone()));
  return res;
}

async function playlistApi(req, url, env, user, body) {
  const uid = await hmacHex(env.BOT_TOKEN, String(user.id), 16);
  const owner = String(user.id) === String(env.OWNER_ID || "");

  // мои плейлисты (включая скрытые и неактивные) и что я лайкнул
  if (url.pathname === "/api/pl/mine") {
    const mine = await env.DB.prepare("SELECT * FROM playlists WHERE uid = ? ORDER BY updated DESC").bind(uid).all();
    const liked = await env.DB.prepare("SELECT pid FROM playlist_likes WHERE uid = ?").bind(uid).all();
    return json({ owner, playlists: mine.results.map(p => plView(p)), liked: liked.results.map(r => r.pid) });
  }
  if (req.method !== "POST") return json({ error: "bad" });
  if (!(await isSub(env, user.id))) return json({ error: "sub" });

  // опубликовать или обновить
  if (url.pathname === "/api/pl/save") {
    const title = String(body.title || "").trim().slice(0, PL.title);
    const descr = String(body.descr || "").trim().slice(0, PL.descr);
    const tags = (Array.isArray(body.tags) ? body.tags : []).map(t => String(t).slice(0, 40)).slice(0, PL.tags);
    const albums = [...new Set((Array.isArray(body.albums) ? body.albums : []).map(Number).filter(n => n > 0))].slice(0, PL.max);
    if (title.length < 2) return json({ error: "Дай плейлисту название" });
    if (albums.length < PL.min) return json({ error: `Нужно хотя бы ${PL.min} альбомов` });
    const now = Date.now(), author = handle(user);
    if (body.id) {
      const p = await env.DB.prepare("SELECT uid, updated FROM playlists WHERE id = ?").bind(body.id).first();
      if (!p || p.uid !== uid) return json({ error: "Это не твой плейлист" });
      if (!owner && now - p.updated < PL.every) return json({ error: "wait", minutes: Math.ceil((PL.every - (now - p.updated)) / 60000) });
      await env.DB.prepare("UPDATE playlists SET author = ?, title = ?, descr = ?, tags = ?, albums = ?, updated = ? WHERE id = ?")
        .bind(author, title, descr, JSON.stringify(tags), JSON.stringify(albums), now, body.id).run();
      return json({ ok: true, id: body.id });
    }
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM playlists WHERE uid = ?").bind(uid).first();
    if (!owner && n.n >= PL.perUser) return json({ error: `Не больше ${PL.perUser} плейлистов. Удали или обнови старый.` });
    const id = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
    await env.DB.prepare("INSERT INTO playlists (id, uid, author, official, title, descr, tags, albums, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .bind(id, uid, author, owner ? 1 : 0, title, descr, JSON.stringify(tags), JSON.stringify(albums), now, now).run();
    return json({ ok: true, id });
  }

  if (url.pathname === "/api/pl/delete") {
    const p = await env.DB.prepare("SELECT uid FROM playlists WHERE id = ?").bind(body.id).first();
    if (!p || (p.uid !== uid && !owner)) return json({ error: "bad" });
    await env.DB.batch([env.DB.prepare("DELETE FROM playlists WHERE id = ?").bind(body.id),
                        env.DB.prepare("DELETE FROM playlist_likes WHERE pid = ?").bind(body.id)]);
    return json({ ok: true });
  }

  // скрыть чужой плейлист — только админ
  if (url.pathname === "/api/pl/hide") {
    if (!owner) return json({ error: "bad" });
    await env.DB.prepare("UPDATE playlists SET hidden = ? WHERE id = ?").bind(body.hidden ? 1 : 0, body.id).run();
    return json({ ok: true });
  }

  // ♥ плейлисту (повторно — снять)
  if (url.pathname === "/api/pl/like") {
    const had = await env.DB.prepare("SELECT 1 FROM playlist_likes WHERE pid = ? AND uid = ?").bind(body.id, uid).first();
    const want = !!body.on;
    if (want && !had) await env.DB.batch([env.DB.prepare("INSERT INTO playlist_likes (pid, uid) VALUES (?, ?)").bind(body.id, uid),
                                          env.DB.prepare("UPDATE playlists SET likes = likes + 1 WHERE id = ?").bind(body.id)]);
    if (!want && had) await env.DB.batch([env.DB.prepare("DELETE FROM playlist_likes WHERE pid = ? AND uid = ?").bind(body.id, uid),
                                          env.DB.prepare("UPDATE playlists SET likes = MAX(likes - 1, 0) WHERE id = ?").bind(body.id)]);
    const p = await env.DB.prepare("SELECT likes FROM playlists WHERE id = ?").bind(body.id).first();
    return json({ ok: true, likes: p ? p.likes : 0 });
  }
  return json({ error: "not found" });
}

function authed(req, secret) {
  return req.headers.get("Authorization") === `Bearer ${secret}`;
}

function json(o) {
  return new Response(JSON.stringify(o), { headers: { "Content-Type": "application/json" } });
}

// тот же секрет, что вычисляет бот на GitHub: HMAC-SHA256(токен, "webhook"), первые 32 символа
async function hookSecret(token) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(token),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode("webhook"));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

async function wakeGithub(env) {
  const now = Math.floor(Date.now() / 1000);
  const row = await env.DB.prepare("SELECT value FROM meta WHERE key = 'last_dispatch'").first();
  if (row && now - Number(row.value) < DISPATCH_EVERY) return;
  await env.DB.prepare("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_dispatch', ?)").bind(String(now)).run();
  await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/bot.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "volodya-archive-worker",
    },
    body: JSON.stringify({ ref: "main" }),
  });
}

// ---------------- дуэли ----------------
// Одна дуэль — одно сообщение и один соперник: кто первым ответил, тот и соперник.
// Когда он ответит, бот правит само сообщение в чате и пишет автору в личку.

// Проверка, что запрос пришёл из мини-приложения от настоящего пользователя Telegram
async function tgUser(initData, token) {
  const params = new URLSearchParams(initData || "");
  const hash = params.get("hash");
  if (!hash) return null;
  params.delete("hash");
  const check = [...params.entries()].map(([k, v]) => `${k}=${v}`).sort().join("\n");
  const enc = new TextEncoder();
  const k1 = await crypto.subtle.importKey("raw", enc.encode("WebAppData"), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const secret = await crypto.subtle.sign("HMAC", k1, enc.encode(token));
  const k2 = await crypto.subtle.importKey("raw", secret, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = [...new Uint8Array(await crypto.subtle.sign("HMAC", k2, enc.encode(check)))].map(b => b.toString(16).padStart(2, "0")).join("");
  if (sig !== hash) return null;
  try { return JSON.parse(params.get("user")); } catch (e) { return null; }
}
const handle = u => u ? (u.username ? "@" + u.username : (u.first_name || "кто-то")) : "кто-то";

function duelMatch(a, b) {
  let both = 0, same = 0;
  a.forEach((x, i) => { if (x && b[i]) { both++; if (x === b[i]) same++; } });
  return { both, same, pct: both >= 3 ? Math.round(same / both * 100) : null };
}

async function tgCall(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

const CLAIM_TTL = 30 * 60 * 1000;   // принявший вызов держит его 30 минут; не ответил — вызов снова свободен
const claimActive = d => d.b_id && !d.b_ans && d.claimed && Date.now() - d.claimed < CLAIM_TTL;

function duelView(d, me) {
  const busy = d.b_ans || claimActive(d);
  const role = me === d.a_id ? "author" : (busy ? (me === d.b_id ? "opponent" : "other") : "new");
  return {
    id: d.id, role, created: d.created, ids: JSON.parse(d.ids), a_name: d.a_name,
    b_name: busy ? d.b_name : null, a_ans: JSON.parse(d.a_ans),
    b_ans: d.b_ans ? JSON.parse(d.b_ans) : null, done: !!d.b_ans, claimed: !!busy && !d.b_ans,
  };
}

// Подписан ли человек на канал. Если Telegram не ответил — пускаем (лучше пропустить, чем зря не пустить).
async function isSub(env, uid) {
  if (String(uid) === String(env.OWNER_ID || "")) return true;
  try {
    const r = await tgCall(env, "getChatMember", { chat_id: "@volodyamusicbox", user_id: uid });
    if (!r.ok) return true;
    const s = r.result.status;
    return s === "creator" || s === "administrator" || s === "member" || (s === "restricted" && r.result.is_member);
  } catch (e) { return true; }
}

let migrated = false;
async function migrate(env) {
  if (migrated) return;
  await env.DB.prepare("ALTER TABLE duels ADD COLUMN claimed INTEGER").run().catch(() => {});
  migrated = true;
}

async function api(req, url, env, ctx) {
  const body = req.method === "POST" ? await req.json().catch(() => ({})) : {};
  const user = await tgUser(body.initData || url.searchParams.get("initData"), env.BOT_TOKEN);
  if (!user) return json({ error: "Открой через Telegram" });
  await migrate(env);

  if (url.pathname === "/api/sub") return json({ sub: await isSub(env, user.id) });

  if (url.pathname.startsWith("/api/pl/")) return playlistApi(req, url, env, user, body);

  // оценки: пачка {альбом: 1 | -1 | 0}, 0 — снять оценку. Приходят прямо из мини-приложения.
  if (url.pathname === "/api/vote" && req.method === "POST") {
    if (!(await isSub(env, user.id))) return json({ error: "sub" });
    const uid = await hmacHex(env.BOT_TOKEN, String(user.id), 16);
    const entries = Object.entries(body.r || {}).slice(0, 300)
      .map(([a, v]) => [Number(a), Number(v)]).filter(([a, v]) => a > 0 && [1, -1, 0].includes(v));
    const touched = [];
    for (const [album, v] of entries) {
      const old = await env.DB.prepare("SELECT v FROM votes WHERE uid = ? AND album = ?").bind(uid, album).first();
      const was = old ? old.v : 0;
      if (was === v) continue;
      const dl = (v === 1) - (was === 1), dd = (v === -1) - (was === -1);
      await env.DB.batch([
        v === 0
          ? env.DB.prepare("DELETE FROM votes WHERE uid = ? AND album = ?").bind(uid, album)
          : env.DB.prepare("INSERT INTO votes (uid, album, v) VALUES (?, ?, ?) ON CONFLICT(uid, album) DO UPDATE SET v = excluded.v").bind(uid, album, v),
        env.DB.prepare("INSERT INTO counts (album, likes, dislikes) VALUES (?, ?, ?) ON CONFLICT(album) DO UPDATE SET likes = likes + ?, dislikes = dislikes + ?")
          .bind(album, Math.max(dl, 0), Math.max(dd, 0), dl, dd),
      ]);
      touched.push(album);
    }
    const counts = {};
    for (const album of touched) {
      const c = await env.DB.prepare("SELECT likes, dislikes FROM counts WHERE album = ?").bind(album).first();
      counts[album] = [c ? c.likes : 0, c ? c.dislikes : 0];
    }
    return json({ ok: true, counts });
  }

  // создать дуэль: 15 альбомов и ответы автора
  if (url.pathname === "/api/duel/new" && req.method === "POST") {
    if (!(await isSub(env, user.id))) return json({ error: "sub" });
    const ids = (body.ids || []).slice(0, 20).map(Number), ans = (body.answers || []).slice(0, 20).map(Number);
    if (ids.length < 5 || ids.length !== ans.length) return json({ error: "bad" });
    // не больше 10 вызовов в сутки с одного человека — чтобы никто не завалил базу
    const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM duels WHERE a_id = ? AND created > ?")
      .bind(user.id, Date.now() - 86400000).first();
    if (recent && recent.n >= 10) return json({ error: "limit" });
    const id = crypto.randomUUID().replace(/-/g, "").slice(0, 10);
    await env.DB.prepare("INSERT INTO duels (id, created, a_id, a_name, ids, a_ans) VALUES (?, ?, ?, ?, ?, ?)")
      .bind(id, Date.now(), user.id, handle(user), JSON.stringify(ids), JSON.stringify(ans)).run();
    return json({ id });
  }

  // мои дуэли: последние, где я автор или соперник
  if (url.pathname === "/api/duel/mine") {
    const { results } = await env.DB.prepare(
      "SELECT * FROM duels WHERE a_id = ? OR b_id = ? ORDER BY created DESC LIMIT 10").bind(user.id, user.id).all();
    return json({ duels: results.map(d => duelView(d, user.id)) });
  }

  const m = url.pathname.match(/^\/api\/duel\/([a-f0-9]{10})(\/claim|\/answer)?$/);
  if (!m) return json({ error: "not found" });
  const d = await env.DB.prepare("SELECT * FROM duels WHERE id = ?").bind(m[1]).first();
  if (!d) return json({ error: "not found" });
  if (!m[2] || user.id === d.a_id || d.b_ans) return json(duelView(d, user.id));
  const free = !d.b_id || d.b_id === user.id || !claimActive(d);
  if (!(await isSub(env, user.id))) return json({ error: "sub" });   // принять и ответить — только подписчикам

  // принять вызов: закрепить за собой и показать это в чате
  if (m[2] === "/claim") {
    if (!free) return json(duelView(d, user.id));
    await env.DB.prepare("UPDATE duels SET b_id = ?, b_name = ?, claimed = ? WHERE id = ? AND b_ans IS NULL")
      .bind(user.id, handle(user), Date.now(), d.id).run();
    const fresh = await env.DB.prepare("SELECT * FROM duels WHERE id = ?").bind(d.id).first();
    if (d.b_id !== user.id) ctx.waitUntil(editDuelMessage(fresh, env).catch(e => console.log("edit", e)));
    return json(duelView(fresh, user.id));
  }

  // ответы соперника — один раз
  if (!free) return json(duelView(d, user.id));
  const ans = (body.answers || []).map(Number);
  const r = duelMatch(JSON.parse(d.a_ans), ans);
  const upd = await env.DB.prepare(
    "UPDATE duels SET b_id = ?, b_name = ?, b_ans = ?, pct = ? WHERE id = ? AND b_ans IS NULL")
    .bind(user.id, handle(user), JSON.stringify(ans), r.pct, d.id).run();
  const fresh = await env.DB.prepare("SELECT * FROM duels WHERE id = ?").bind(d.id).first();
  if (upd.meta && upd.meta.changes) ctx.waitUntil(editDuelMessage(fresh, env).catch(e => console.log("edit", e)));
  return json(duelView(fresh, user.id));
}

// Сообщение с вызовом в чате живёт вместе с дуэлью: принят → итог. В личку ничего не шлём.
function duelText(d) {
  if (d.b_ans) {
    const r = duelMatch(JSON.parse(d.a_ans), JSON.parse(d.b_ans));
    return r.pct === null
      ? `${esc(d.a_name)} и ${esc(d.b_name)} почти не пересеклись: оба слышали только ${r.both} из 15.`
      : `Сравнение вкусов: ${esc(d.a_name)} и ${esc(d.b_name)} — <b>${r.pct}%</b>\nОба слышали ${r.both} из 15, одинаково оценили ${r.same}.`;
  }
  if (claimActive(d)) return `${esc(d.a_name)} предлагает сравнить музыкальные вкусы.\nВызов принял ${esc(d.b_name)} — отвечает.`;
  return `${esc(d.a_name)} предлагает сравнить музыкальные вкусы.\n15 альбомов из архива @volodyamusicbox, около минуты. Ответить может один человек — кто первым нажмёт.`;
}
async function editDuelMessage(d, env) {
  if (!d.msg) return;
  await tgCall(env, "editMessageText", {
    inline_message_id: d.msg, text: duelText(d), parse_mode: "HTML",
    reply_markup: { inline_keyboard: [[{ text: d.b_ans ? "Посмотреть ответы" : "Сравнить вкусы", url: `${env.MINIAPP}?startapp=x${d.id}` }]] },
  });
}

// Telegram сообщает, что вызов отправлен в чат — запоминаем сообщение, чтобы потом его обновить
async function chosen(c, env) {
  // id дуэли зашит в id результата («duel-<id>»), так что неважно, писал человек «вызов» или «вызов <id>»
  const m = (c.result_id || "").match(/^duel-([a-f0-9]{10})$/);
  if (!m || !c.inline_message_id) return;
  await env.DB.prepare("UPDATE duels SET msg = ? WHERE id = ? AND a_id = ? AND msg IS NULL")
    .bind(c.inline_message_id, m[1], c.from.id).run();
}

// ---------------- инлайн-режим ----------------

// карточка плейлиста для чата
function plArticle(p, all, env) {
  const ids = JSON.parse(p.albums), first = all.find(a => a.id === ids[0]);
  const cover = first && first.has_cover ? `${env.PAGES}covers/jpg/${first.id}.jpg` : null;
  const tags = JSON.parse(p.tags || "[]").join(", ");
  const res = {
    type: "article", id: "pl-" + p.id, title: `📼 ${p.title}`,
    description: `Твой плейлист · ${ids.length} альбомов${tags ? " · " + tags : ""}`,
    input_message_content: {
      message_text: `📼 <b>${esc(p.title)}</b> — плейлист ${esc(p.author)}, ${ids.length} альбомов`
        + (tags ? `\n${esc(tags)}` : "") + (p.descr ? `\n\n${esc(p.descr.slice(0, 200))}${p.descr.length > 200 ? "…" : ""}` : ""),
      parse_mode: "HTML",
      link_preview_options: cover ? { url: cover, prefer_large_media: true, show_above_text: true } : { is_disabled: true },
    },
    reply_markup: { inline_keyboard: [[{ text: "Открыть плейлист", url: `${env.MINIAPP}?startapp=p${p.id}` }]] },
  };
  if (cover) res.thumbnail_url = cover;
  return res;
}

// карточка «Отправить вызов» — по умолчанию вызов принимает тот, кто первым нажмёт
function duelArticle(id, who, env) {
  return {
    type: "article", id: "duel-" + id, title: "⚔️ Отправить вызов",
    description: "Твои ответы уже сохранены. Ответит тот, кто первым примет",
    input_message_content: {
      message_text: `${esc(who)} предлагает сравнить музыкальные вкусы.\n15 альбомов из архива @volodyamusicbox, около минуты. Ответить может один человек — кто первым нажмёт.`,
      parse_mode: "HTML",
    },
    reply_markup: { inline_keyboard: [[{ text: "Сравнить вкусы", url: `${env.MINIAPP}?startapp=x${id}` }]] },
  };
}

async function archive(env) {
  // архив лежит рядом, в статике воркера
  const r = await env.ASSETS.fetch(new Request(env.PAGES + "data.json"));
  return r.json();
}

const norm = s => (s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/ё/g, "е");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function card(a, env, title) {
  const cover = a.has_cover ? `${env.PAGES}covers/jpg/${a.id}.jpg` : null;
  const styles = (a.styles || []).slice(0, 3).join(", ");
  const text = `💽 <b>${esc(a.title)}</b> — ${esc(a.artist || "?")}${a.year ? ` (${a.year})` : ""}`
    + (styles ? `\n${esc(styles)}` : "")
    + `\nиз архива @volodyamusicbox`;
  const res = {
    type: "article",
    id: `a${a.id}-${title ? "r" : "s"}`,
    title: title || `${a.title}`,
    description: title ? `${a.artist || "?"} — ${a.title}${a.year ? ` (${a.year})` : ""}`
                       : `${a.artist || "?"}${a.year ? ` · ${a.year}` : ""}${styles ? ` · ${styles}` : ""}`,
    input_message_content: {
      message_text: text, parse_mode: "HTML",
      link_preview_options: cover ? { url: cover, prefer_large_media: true, show_above_text: true } : { is_disabled: true },
    },
    reply_markup: { inline_keyboard: [[{ text: "🗄 Открыть в архиве", url: `${env.MINIAPP}?startapp=album_${a.id}` }]] },
  };
  if (cover) res.thumbnail_url = cover;
  return res;
}

async function inline(q, env) {
  const all = await archive(env);
  const raw = q.query.trim();
  const query = norm(raw);
  let results;
  let duel = raw.match(/^вызов ([a-f0-9]{10})$/);
  if (!duel && /^вызов$/.test(raw) && q.from) {   // просто «вызов» — берём твой последний неотвеченный
    const last = await env.DB.prepare("SELECT id FROM duels WHERE a_id = ? AND b_ans IS NULL ORDER BY created DESC LIMIT 1").bind(q.from.id).first();
    if (last) duel = [raw, last.id];
  }
  const who = q.from && q.from.username ? "@" + q.from.username : (q.from && q.from.first_name) || "Кто-то";
  if (duel) {
    const d = await env.DB.prepare("SELECT a_id, b_id FROM duels WHERE id = ?").bind(duel[1]).first();
    if (d && d.a_id === q.from.id && !d.b_id) {
      results = [duelArticle(duel[1], who, env)];
    } else results = [];
  } else if (/^вызов$/.test(raw)) {
    results = [{
      type: "article", id: "duel-none", title: "У тебя нет неотправленных вызовов",
      description: "Пройди 15 альбомов в архиве — потом вызов появится здесь",
      input_message_content: { message_text: "Сравнение музыкальных вкусов: 15 альбомов из архива @volodyamusicbox.", parse_mode: "HTML" },
      reply_markup: { inline_keyboard: [[{ text: "Начать", url: `${env.MINIAPP}?startapp=duel` }]] },
    }];
  } else if (!query) {
    const rnd = all[Math.floor(Math.random() * all.length)];
    const fresh = [...all].sort((x, y) => (y.added || "").localeCompare(x.added || "")).slice(0, 8);
    const pending = q.from ? await env.DB.prepare(
      "SELECT id FROM duels WHERE a_id = ? AND b_id IS NULL AND msg IS NULL ORDER BY created DESC LIMIT 1").bind(q.from.id).first() : null;
    const myUid = q.from ? await hmacHex(env.BOT_TOKEN, String(q.from.id), 16) : "";
    const myPl = q.from ? (await env.DB.prepare("SELECT * FROM playlists WHERE uid = ? ORDER BY updated DESC LIMIT 5").bind(myUid).all()).results : [];
    results = [
      ...(pending ? [duelArticle(pending.id, who, env)] : []),
      ...myPl.map(p => plArticle(p, all, env)),
      card(rnd, env, "🎲 Посоветовать случайный альбом"),
      {
        type: "article", id: "duel-new", title: pending ? "⚔️ Новый вызов" : "⚔️ Сравнить вкусы",
        description: "15 альбомов, потом процент совпадения с другом",
        thumbnail_url: `${env.PAGES}covers/jpg/${fresh[1].id}.jpg`,
        input_message_content: { message_text: "Сравнение музыкальных вкусов: ответь про 15 альбомов из архива @volodyamusicbox и отправь вызов кому хочешь.", parse_mode: "HTML" },
        reply_markup: { inline_keyboard: [[{ text: "Начать", url: `${env.MINIAPP}?startapp=duel` }]] },
      },
      {
        type: "article", id: "open", title: "🗄 Открыть архив",
        description: `${all.length} альбомов канала — с годами, стилями и обложками`,
        thumbnail_url: `${env.PAGES}covers/jpg/${fresh[0].id}.jpg`,
        input_message_content: { message_text: `🗄 <b>Архив канала @volodyamusicbox</b>\n${all.length} альбомов — с годами, стилями и обложками.`, parse_mode: "HTML" },
        reply_markup: { inline_keyboard: [[{ text: "🗄 Открыть архив", url: env.MINIAPP }]] },
      },
      ...fresh.map(a => ({ ...card(a, env), description: "🔥 свежее · " + card(a, env).description })),
    ];
  } else {
    const words = query.split(/\s+/);
    results = all.filter(a => { const h = norm(`${a.title} ${a.artist} ${a.year} ${(a.styles || []).join(" ")}`); return words.every(w => h.includes(w)); })
      .slice(0, 30).map(a => card(a, env));
    if (!results.length) {
      results = [{
        type: "article", id: "none", title: "Ничего не нашлось",
        description: "Попробуй исполнителя, название, год или стиль",
        input_message_content: { message_text: `🗄 Архив канала @volodyamusicbox — ${all.length} альбомов`, parse_mode: "HTML" },
        reply_markup: { inline_keyboard: [[{ text: "🗄 Открыть архив", url: env.MINIAPP }]] },
      }];
    }
  }
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/answerInlineQuery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      inline_query_id: q.id, results,
      // случайный альбом и вызовы — у каждого свои
      cache_time: (query && !duel) ? 120 : 5, is_personal: !query || !!duel,
    }),
  });
}
