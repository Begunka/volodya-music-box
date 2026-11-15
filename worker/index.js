// Входная дверь бота @volodyamusicbox_bot на Cloudflare Workers.
//  • инлайн-запросы (@volodyamusicbox_bot …) — отвечает сразу сам;
//  • всё остальное складывает в очередь (D1) и будит бота на GitHub,
//    который делает тяжёлую работу: архив, Discogs, обложки, сайт.
//  • кнопки под сообщениями бота, команды в личке, оплату звёздами, напоминания
//    «послушаю позже» (по расписанию, раз в час) и статистику — тоже сам.
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
      if (u.pre_checkout_query) {   // Telegram ждёт ответ не дольше 10 секунд — отвечаем сразу
        const pq = u.pre_checkout_query;
        const ok = pq.currency === "XTR" && /^don:\d+$/.test(pq.invoice_payload || "");
        await tgCall(env, "answerPreCheckoutQuery", { pre_checkout_query_id: pq.id, ok, ...(ok ? {} : { error_message: "Этот счёт устарел. Открой архив и попробуй ещё раз." }) }).catch(() => {});
        return new Response("ok");
      }
      if (u.callback_query) {
        ctx.waitUntil(onCallback(u.callback_query, env).catch(e => console.log("callback", e)));
        return new Response("ok");
      }
      if (u.my_chat_member) {
        ctx.waitUntil(onChatMember(u.my_chat_member, env).catch(e => console.log("member", e)));
        return new Response("ok");
      }
      if (u.message && u.message.chat.type === "private" && (u.message.successful_payment || /^\//.test(u.message.text || ""))) {
        // команды в личке и оплата — отвечаем сразу; чего не знаем (например, служебные команды владельца) — в очередь
        const handled = await onPrivate(u.message, env).catch(e => { console.log("private", e); return false; });
        if (handled) return new Response("ok");
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

  // раз в час: напоминания «послушаю позже», по понедельникам — сводка владельцу, раз в сутки — уборка
  async scheduled(event, env, ctx) {
    env.BOT_TOKEN = (env.BOT_TOKEN || "").trim();
    ctx.waitUntil(cron(env).catch(e => console.log("cron", e)));
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
  const { results } = await env.DB.prepare("SELECT * FROM playlists WHERE hidden = 0 AND uid NOT IN (SELECT uid FROM bans) ORDER BY updated DESC").all();
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

  // модерация — только админ: все плейлисты (и скрытые) + список забаненных
  if (url.pathname === "/api/pl/admin") {
    if (!owner) return json({ error: "bad" });
    const all = await env.DB.prepare("SELECT p.*, (b.uid IS NOT NULL) AS banned FROM playlists p LEFT JOIN bans b ON b.uid = p.uid WHERE p.official = 0 ORDER BY p.updated DESC LIMIT 300").all();
    const bans = await env.DB.prepare("SELECT uid, author, at FROM bans ORDER BY at DESC").all();
    return json({ playlists: all.results.map(p => plView(p, { banned: !!p.banned, ref: p.uid })), bans: bans.results.map(b => ({ ref: b.uid, author: b.author, at: b.at })) });
  }
  if (req.method !== "POST") return json({ error: "bad" });

  // бан автора: по id плейлиста или по ref из списка забаненных
  if (url.pathname === "/api/pl/ban") {
    if (!owner) return json({ error: "bad" });
    let target = body.ref, author = "";
    if (body.id) {
      const p = await env.DB.prepare("SELECT uid, author FROM playlists WHERE id = ?").bind(body.id).first();
      if (!p) return json({ error: "bad" });
      target = p.uid; author = p.author;
    }
    if (!target || target === uid) return json({ error: "bad" });
    if (!body.on) {
      await env.DB.prepare("DELETE FROM bans WHERE uid = ?").bind(target).run();
      return json({ ok: true });
    }
    await env.DB.prepare("INSERT OR REPLACE INTO bans (uid, author, at) VALUES (?, ?, ?)").bind(target, author, Date.now()).run();
    // убираем всё, что он успел налайкать
    const votes = await env.DB.prepare("SELECT album, v FROM votes WHERE uid = ?").bind(target).all();
    const pls = await env.DB.prepare("SELECT pid FROM playlist_likes WHERE uid = ?").bind(target).all();
    const ops = votes.results.map(r => env.DB.prepare("UPDATE counts SET likes = MAX(likes - ?, 0), dislikes = MAX(dislikes - ?, 0) WHERE album = ?")
      .bind(r.v === 1 ? 1 : 0, r.v === -1 ? 1 : 0, r.album));
    pls.results.forEach(r => ops.push(env.DB.prepare("UPDATE playlists SET likes = MAX(likes - 1, 0) WHERE id = ?").bind(r.pid)));
    ops.push(env.DB.prepare("DELETE FROM votes WHERE uid = ?").bind(target), env.DB.prepare("DELETE FROM playlist_likes WHERE uid = ?").bind(target));
    await env.DB.batch(ops);
    return json({ ok: true, removed: votes.results.length + pls.results.length });
  }

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

// Вызов можно отправить из инлайна, ещё не ответив самому: тогда a_ans = "[]", а автор отвечает
// по той же кнопке. Итог появляется, когда ответили оба.
const aDone = d => d.a_ans !== "[]";
function duelView(d, me) {
  const busy = d.b_ans || claimActive(d);
  const role = me === d.a_id ? "author" : (busy ? (me === d.b_id ? "opponent" : "other") : "new");
  return {
    id: d.id, role, created: d.created, ids: JSON.parse(d.ids), a_name: d.a_name,
    b_name: busy ? d.b_name : null, a_ans: aDone(d) ? JSON.parse(d.a_ans) : null, a_done: aDone(d),
    b_ans: d.b_ans ? JSON.parse(d.b_ans) : null, done: !!d.b_ans && aDone(d), claimed: !!busy && !d.b_ans,
    sent: !!d.msg,
  };
}
const cleanAns = (list, n) => Array.from({ length: n }, (_, i) => [1, 2].includes(Number((list || [])[i])) ? Number(list[i]) : 0);

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
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS bans (uid TEXT PRIMARY KEY, author TEXT, at INTEGER NOT NULL)").run().catch(() => {});
  migrated = true;
}

async function api(req, url, env, ctx) {
  const body = req.method === "POST" ? await req.json().catch(() => ({})) : {};
  const user = await tgUser(body.initData || url.searchParams.get("initData"), env.BOT_TOKEN);
  if (!user) return json({ error: "Открой через Telegram" });
  await migrate(env);

  const me = await hmacHex(env.BOT_TOKEN, String(user.id), 16);
  const banned = !!(await env.DB.prepare("SELECT 1 FROM bans WHERE uid = ?").bind(me).first());
  if (url.pathname === "/api/sub") {
    // этот запрос мини-приложение делает при каждом открытии — заодно считаем открытия и откуда пришли
    const src = (url.searchParams.get("src") || "plain").replace(/[^a-z_]/g, "").slice(0, 16) || "plain";
    if (url.searchParams.get("src")) ctx.waitUntil(track(env, ["open:" + src], [["app", me]]).catch(() => {}));
    // очередь «послушаю позже» отдаём тут же, чтобы не тратить на неё отдельный запрос
    let later = { items: [], dm: false, remind: true };
    try {
      const [items, person] = await env.DB.batch([
        env.DB.prepare("SELECT album, added, done FROM later WHERE tg_id = ? ORDER BY added DESC LIMIT 300").bind(user.id),
        env.DB.prepare("SELECT dm, blocked, remind FROM people WHERE tg_id = ?").bind(user.id)]);
      const p = person.results[0];
      later = { items: items.results, dm: !!(user.allows_write_to_pm || (p && p.dm && !p.blocked)), remind: !p || !!p.remind };
    } catch (e) {}
    return json({ sub: await isSub(env, user.id), ban: banned, later });
  }
  if (url.pathname === "/api/later" || url.pathname.startsWith("/api/later/")) return laterApi(req, url, env, user, body);
  if (url.pathname === "/api/donate" && req.method === "POST") {
    const amount = Math.floor(Number(body.amount));
    if (!(amount >= 1 && amount <= 2500)) return json({ error: "bad" });
    const r = await tgCall(env, "createInvoiceLink", invoice(amount));
    return json(r.ok ? { link: r.result } : { error: "tg" });
  }
  // забаненный может смотреть, но не может ничего менять
  if (banned && req.method === "POST") return json({ error: "ban" });

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
  if (!m[2]) return json(duelView(d, user.id));
  if (user.id === d.a_id) {
    // автор отправил вызов из инлайна и теперь отвечает сам
    if (m[2] === "/answer" && !aDone(d)) {
      if (!(await isSub(env, user.id))) return json({ error: "sub" });
      const ans = cleanAns(body.answers, JSON.parse(d.ids).length);
      const pct = d.b_ans ? duelMatch(ans, JSON.parse(d.b_ans)).pct : null;
      const upd = await env.DB.prepare("UPDATE duels SET a_ans = ?, pct = ? WHERE id = ? AND a_ans = '[]'")
        .bind(JSON.stringify(ans), pct, d.id).run();
      const fresh = await env.DB.prepare("SELECT * FROM duels WHERE id = ?").bind(d.id).first();
      if (upd.meta && upd.meta.changes && fresh.b_ans) ctx.waitUntil(editDuelMessage(fresh, env).catch(e => console.log("edit", e)));
      return json(duelView(fresh, user.id));
    }
    return json(duelView(d, user.id));
  }
  if (d.b_ans) return json(duelView(d, user.id));
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
  const ans = cleanAns(body.answers, JSON.parse(d.ids).length);
  const r = aDone(d) ? duelMatch(JSON.parse(d.a_ans), ans) : { pct: null };
  const upd = await env.DB.prepare(
    "UPDATE duels SET b_id = ?, b_name = ?, b_ans = ?, pct = ? WHERE id = ? AND b_ans IS NULL")
    .bind(user.id, handle(user), JSON.stringify(ans), r.pct, d.id).run();
  const fresh = await env.DB.prepare("SELECT * FROM duels WHERE id = ?").bind(d.id).first();
  if (upd.meta && upd.meta.changes) ctx.waitUntil(editDuelMessage(fresh, env).catch(e => console.log("edit", e)));
  return json(duelView(fresh, user.id));
}

// Сообщение с вызовом в чате живёт вместе с дуэлью: принят → итог. В личку ничего не шлём.
function duelText(d) {
  if (d.b_ans && !aDone(d)) return `${esc(d.b_name)} ответил на вызов ${esc(d.a_name)}.\nИтог появится, когда ${esc(d.a_name)} ответит сам — по этой же кнопке.`;
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
    reply_markup: { inline_keyboard: [[{ text: d.b_ans && aDone(d) ? "Посмотреть ответы" : "Сравнить вкусы", url: `${env.MINIAPP}?startapp=x${d.id}` }]] },
  });
}

// 15 альбомов для вызова: с обложкой и исполнителем, поровну из разных десятилетий
function pickDuel(all, n = 15) {
  const shuffle = l => l.map(x => [Math.random(), x]).sort((a, b) => a[0] - b[0]).map(x => x[1]);
  const byDec = {};
  all.filter(a => a.has_cover && a.artist).forEach(a => { const k = a.year ? a.year.slice(0, 3) : "?"; (byDec[k] = byDec[k] || []).push(a); });
  Object.keys(byDec).forEach(k => byDec[k] = shuffle(byDec[k]));
  const decs = shuffle(Object.keys(byDec)), out = [];
  while (out.length < n && decs.some(k => byDec[k].length))
    for (const k of decs) if (byDec[k].length && out.length < n) out.push(byDec[k].pop());
  return shuffle(out).map(a => a.id);
}

// Telegram сообщает, что человек выбрал карточку из инлайн-списка
async function chosen(c, env) {
  const rid = c.result_id || "";
  const kind = /^a\d+-r$/.test(rid) ? "random" : /^a\d+-s$/.test(rid) ? "album" : /^(duel|dnew)-/.test(rid) ? "duel"
    : rid.startsWith("pl-") ? "playlist" : rid === "open" ? "open" : "other";
  await track(env, ["sent:" + kind], [["inline", await hmacHex(env.BOT_TOKEN, String(c.from.id), 16)]]);
  if (!c.inline_message_id) return;
  // вызов с готовыми ответами — запоминаем сообщение, чтобы потом его обновлять
  const m = rid.match(/^duel-([a-f0-9]{10})$/);
  if (m) {
    await env.DB.prepare("UPDATE duels SET msg = ? WHERE id = ? AND a_id = ? AND msg IS NULL")
      .bind(c.inline_message_id, m[1], c.from.id).run();
    return;
  }
  // вызов прямо из списка: дуэль создаётся в момент отправки, автор ответит по кнопке
  const n = rid.match(/^dnew-([a-f0-9]{10})$/);
  if (n) {
    const ids = pickDuel(await archive(env));
    await env.DB.prepare("INSERT OR IGNORE INTO duels (id, created, a_id, a_name, ids, a_ans, msg) VALUES (?, ?, ?, ?, ?, '[]', ?)")
      .bind(n[1], Date.now(), c.from.id, handle(c.from), JSON.stringify(ids), c.inline_message_id).run();
  }
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

// карточка «Отправить вызов» — вызов принимает тот, кто первым нажмёт.
// ready: ответы автора уже сохранены (прошёл 15 альбомов в архиве); иначе он ответит по той же кнопке.
function duelArticle(id, who, env, ready, thumb) {
  const res = {
    type: "article", id: (ready ? "duel-" : "dnew-") + id, title: "⚔️ Отправить вызов",
    description: ready ? "Твои ответы уже сохранены. Ответит тот, кто первым примет"
                       : "Сравнить вкусы: 15 альбомов, потом процент совпадения. Ответит тот, кто первым примет",
    input_message_content: {
      message_text: `${esc(who)} предлагает сравнить музыкальные вкусы.\n15 альбомов из архива @volodyamusicbox, около минуты. Ответить может один человек — кто первым нажмёт.`,
      parse_mode: "HTML",
    },
    reply_markup: { inline_keyboard: [[{ text: "Сравнить вкусы", url: `${env.MINIAPP}?startapp=x${id}` }]] },
  };
  if (thumb) res.thumbnail_url = thumb;
  return res;
}

async function archive(env) {
  // архив лежит рядом, в статике воркера
  const r = await env.ASSETS.fetch(new Request(env.PAGES + "data.json"));
  return r.json();
}

const norm = s => (s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/ё/g, "е");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const VIA = { sender: "s", private: "p", group: "g", supergroup: "g", channel: "c" };
function card(a, env, title, via) {
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
    reply_markup: { inline_keyboard: [[{ text: "🗄 Открыть в архиве", url: `${env.MINIAPP}?startapp=album_${a.id}${via ? "_" + via : ""}` }]] },
  };
  if (cover) res.thumbnail_url = cover;
  return res;
}

async function inline(q, env) {
  const all = await archive(env);
  const raw = q.query.trim();
  const query = norm(raw);
  const via = VIA[q.chat_type] || "";
  // счётчик пишется параллельно с ответом, чтобы список не появлялся позже
  const tracking = q.from ? hmacHex(env.BOT_TOKEN, String(q.from.id), 16)
    .then(uid => track(env, ["iq:" + (q.chat_type || "unknown")], [["inline", uid]])).catch(() => {}) : null;
  let results;
  const duel = raw.match(/^вызов ([a-f0-9]{10})$/);
  const who = q.from && q.from.username ? "@" + q.from.username : (q.from && q.from.first_name) || "Кто-то";
  if (duel) {
    const d = await env.DB.prepare("SELECT a_id, b_id FROM duels WHERE id = ?").bind(duel[1]).first();
    if (d && d.a_id === q.from.id && !d.b_id) {
      results = [duelArticle(duel[1], who, env, true)];
    } else results = [];
  } else if (/^вызов$/.test(raw) || !query) {
    const rnd = all[Math.floor(Math.random() * all.length)];
    const fresh = [...all].sort((x, y) => (y.added || "").localeCompare(x.added || "")).slice(0, 8);
    const thumb = `${env.PAGES}covers/jpg/${fresh[1].id}.jpg`;
    // вызов — одной карточкой: готовый (ответы уже сохранены в архиве) или новый, создаётся при отправке
    const st = q.from ? await env.DB.prepare(
      `SELECT (SELECT id FROM duels WHERE a_id = ?1 AND b_id IS NULL AND msg IS NULL AND a_ans != '[]' ORDER BY created DESC LIMIT 1) AS pending,
              (SELECT COUNT(*) FROM duels WHERE a_id = ?1 AND created > ?2) AS n`).bind(q.from.id, Date.now() - 86400000).first() : null;
    const duelCard = !q.from ? [] : st && st.pending ? [duelArticle(st.pending, who, env, true, thumb)]
      : (!st || st.n < 10) ? [duelArticle(crypto.randomUUID().replace(/-/g, "").slice(0, 10), who, env, false, thumb)] : [];
    if (query) {
      results = duelCard;
    } else {
      const myUid = q.from ? await hmacHex(env.BOT_TOKEN, String(q.from.id), 16) : "";
      const myPl = q.from ? (await env.DB.prepare("SELECT * FROM playlists WHERE uid = ? ORDER BY updated DESC LIMIT 5").bind(myUid).all()).results : [];
      results = [
        ...(st && st.pending ? duelCard : []),      // только что прошёл 15 альбомов в архиве — вызов первым
        ...myPl.map(p => plArticle(p, all, env)),
        card(rnd, env, "🎲 Посоветовать случайный альбом", via),
        ...(st && st.pending ? [] : duelCard),
        {
          type: "article", id: "open", title: "🗄 Открыть архив",
          description: `${all.length} альбомов канала — с годами, стилями и обложками`,
          thumbnail_url: `${env.PAGES}covers/jpg/${fresh[0].id}.jpg`,
          input_message_content: { message_text: `🗄 <b>Архив канала @volodyamusicbox</b>\n${all.length} альбомов — с годами, стилями и обложками.`, parse_mode: "HTML" },
          reply_markup: { inline_keyboard: [[{ text: "🗄 Открыть архив", url: env.MINIAPP }]] },
        },
        ...fresh.map(a => { const c = card(a, env, null, via); return { ...c, description: "🔥 свежее · " + c.description }; }),
      ];
    }
  } else {
    const words = query.split(/\s+/);
    results = all.filter(a => { const h = norm(`${a.title} ${a.artist} ${a.year} ${(a.styles || []).join(" ")}`); return words.every(w => h.includes(w)); })
      .slice(0, 30).map(a => card(a, env, null, via));
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
      cache_time: (query && !duel && !/^вызов$/.test(raw)) ? 120 : 5, is_personal: !query || !!duel || /^вызов$/.test(raw),
    }),
  });
  await tracking;
}

// ---------------- статистика ----------------
// stats: счётчики по дням (ключ → сколько раз). seen: кто был в этот день (зашифрованный id) —
// из него считаются уникальные люди за день/неделю/месяц. Сырых id и текстов запросов тут нет.
const dayKey = (t = Date.now()) => new Date(t).toISOString().slice(0, 10);
async function track(env, keys, seen) {
  const day = dayKey(), ops = [];
  (keys || []).forEach(k => ops.push(env.DB.prepare(
    "INSERT INTO stats (day, key, n) VALUES (?, ?, 1) ON CONFLICT(day, key) DO UPDATE SET n = n + 1").bind(day, k)));
  (seen || []).forEach(([kind, uid]) => ops.push(env.DB.prepare(
    "INSERT OR IGNORE INTO seen (day, kind, uid) VALUES (?, ?, ?)").bind(day, kind, uid)));
  if (ops.length) await env.DB.batch(ops);
}

// бота добавили в группу или канал (или убрали) — единственный способ узнать, где он стоит
async function onChatMember(m, env) {
  const c = m.chat, st = (m.new_chat_member || {}).status || "";
  if (c.type === "private") {   // человек заблокировал или разблокировал бота
    await env.DB.prepare("UPDATE people SET blocked = ? WHERE tg_id = ?").bind(st === "kicked" ? 1 : 0, c.id).run();
    return track(env, [st === "kicked" ? "bot:blocked" : "bot:unblocked"]);
  }
  await env.DB.prepare("INSERT INTO chats (id, type, title, username, status, at) VALUES (?, ?, ?, ?, ?, ?) " +
    "ON CONFLICT(id) DO UPDATE SET type = excluded.type, title = excluded.title, username = excluded.username, status = excluded.status, at = excluded.at")
    .bind(c.id, c.type, c.title || "", c.username || "", st, Date.now()).run();
  const joined = ["member", "administrator"].includes(st);
  await track(env, [joined ? "chat:added" : "chat:removed"]);
  if (env.OWNER_ID && String(c.id) !== String(env.OWNER_ID))
    await tgCall(env, "sendMessage", { chat_id: env.OWNER_ID, parse_mode: "HTML", disable_notification: true,
      text: `${joined ? "➕ Бота добавили" : "➖ Бота убрали"}: ${c.type === "channel" ? "канал" : "группа"} «${esc(c.title || "?")}»${c.username ? " @" + esc(c.username) : ""}` });
}

const NUM = n => String(n || 0);
const pl = (n, a, b, c) => { const m = n % 100, d = n % 10; return m > 10 && m < 20 ? c : d === 1 ? a : d > 1 && d < 5 ? b : c; };
// Статистика для владельца. days: 1 — сегодня, 7 — неделя, 30 — месяц (сутки по UTC). В цифры входят и твои собственные действия.
async function statsText(env, days = 7) {
  days = days === 1 || days === 30 ? days : 7;
  const now = Date.now(), from = dayKey(now - (days - 1) * 86400000), pFrom = dayKey(now - (2 * days - 1) * 86400000);
  const [st, sn, pst, psn, misc, chats] = await env.DB.batch([
    env.DB.prepare("SELECT key, SUM(n) AS n FROM stats WHERE day >= ?1 GROUP BY key").bind(from),
    env.DB.prepare("SELECT kind, COUNT(DISTINCT uid) AS n FROM seen WHERE day >= ?1 GROUP BY kind").bind(from),
    env.DB.prepare("SELECT key, SUM(n) AS n FROM stats WHERE day >= ?1 AND day < ?2 GROUP BY key").bind(pFrom, from),
    env.DB.prepare("SELECT kind, COUNT(DISTINCT uid) AS n FROM seen WHERE day >= ?1 AND day < ?2 GROUP BY kind").bind(pFrom, from),
    env.DB.prepare(`SELECT (SELECT COUNT(*) FROM people) AS people, (SELECT COUNT(*) FROM people WHERE dm = 1 AND blocked = 0) AS dm,
      (SELECT COUNT(*) FROM later WHERE done IS NULL) AS later, (SELECT COUNT(*) FROM later WHERE done IS NOT NULL) AS listened,
      (SELECT COUNT(DISTINCT tg_id) FROM later) AS later_people, (SELECT COALESCE(SUM(amount), 0) FROM donations WHERE refunded = 0) AS stars,
      (SELECT COUNT(DISTINCT uid) FROM votes) AS voters, (SELECT COUNT(*) FROM votes) AS votes,
      (SELECT COUNT(*) FROM duels) AS duels, (SELECT COUNT(*) FROM duels WHERE b_ans IS NOT NULL) AS duels_done,
      (SELECT COUNT(*) FROM playlists) AS playlists`),
    env.DB.prepare("SELECT type, title, username FROM chats WHERE status IN ('member', 'administrator') ORDER BY at DESC LIMIT 12"),
  ]);
  const k = {}, u = {}, pk = {}, pu = {};
  st.results.forEach(r => k[r.key] = r.n); sn.results.forEach(r => u[r.kind] = r.n);
  pst.results.forEach(r => pk[r.key] = r.n); psn.results.forEach(r => pu[r.kind] = r.n);
  const m = misc.results[0] || {};
  const sum = (o, prefix) => Object.entries(o).filter(([key]) => key.startsWith(prefix)).reduce((t, [, n]) => t + n, 0);
  const trend = (now_, prev) => !prev && !now_ ? "" : ` (раньше: ${prev || 0})`;
  const label = days === 1 ? "сегодня" : days === 7 ? "за 7 дней" : "за 30 дней";
  const prevLabel = days === 1 ? "вчера" : days === 7 ? "предыдущие 7 дней" : "предыдущие 30 дней";
  const where = { "iq:sender": "в личке с ботом", "iq:private": "в личных чатах", "iq:group": "в группах", "iq:supergroup": "в супергруппах", "iq:channel": "в каналах", "iq:unknown": "место неизвестно" };
  const sent = { "sent:album": "альбом из поиска", "sent:random": "случайный альбом", "sent:duel": "вызов на сравнение вкусов", "sent:playlist": "плейлист", "sent:open": "кнопку «открыть архив»" };
  const open = { "open:plain": "просто открыли архив", "open:album": "по ссылке на альбом (комментарии, пересылки)", "open:album_g": "из карточки альбома в группе",
    "open:album_p": "из карточки альбома в личном чате", "open:album_c": "из карточки альбома в канале", "open:album_s": "из карточки альбома в личке бота",
    "open:album_r": "из напоминания «послушаю позже»", "open:duel": "по вызову на сравнение вкусов", "open:pl": "по плейлисту" };
  const list = (obj, map) => Object.keys(map).filter(key => obj[key]).sort((x, y) => obj[y] - obj[x]).map(key => `   • ${map[key]}: ${obj[key]}`);
  const inl = sum(k, "iq:"), apps = sum(k, "open:"), sentN = sum(k, "sent:");
  const lines = [
    `📊 <b>Бот ${label}</b>`, "",
    "<b>Сколько людей пользуется</b>",
    `• Открыли архив: <b>${NUM(u.app)}</b> ${pl(u.app || 0, "человек", "человека", "человек")}${trend(u.app, pu.app)}`,
    `• Набирали @бота в чате: <b>${NUM(u.inline)}</b> ${pl(u.inline || 0, "человек", "человека", "человек")}${trend(u.inline, pu.inline)}`,
    `• Нажали /start: <b>${NUM(k.start)}</b>`, "",
    `<b>Откуда открывали архив</b> — всего <b>${apps}</b> ${pl(apps, "раз", "раза", "раз")}${trend(apps, sum(pk, "open:"))}`,
    ...(list(k, open).length ? list(k, open) : ["   пока никто"]), "",
    `<b>Упоминания бота в чатах</b> — запросов <b>${inl}</b>${trend(inl, sum(pk, "iq:"))}`,
    ...(list(k, where).length ? list(k, where) : ["   пока никто"]),
    `   Из них отправили что-то в чат: <b>${sentN}</b>`,
    ...list(k, sent), "",
    "<b>«Послушаю позже»</b>",
    `• Добавили альбомов ${label}: ${NUM(k["later:add"])}`,
    `• Бот напомнил: ${NUM(k["dm:remind"])}, ответили «послушал»: ${NUM(k["dm:done"])}`,
    `• Сейчас в очередях: ${NUM(m.later)} ${pl(m.later || 0, "альбом", "альбома", "альбомов")} у ${NUM(m.later_people)} ${pl(m.later_people || 0, "человека", "человек", "человек")}, уже послушано ${NUM(m.listened)}`, "",
    "<b>За всё время</b>",
    `• Людей в базе бота: ${NUM(m.people)}, из них можно писать в личку: ${NUM(m.dm)}`,
    `• Оценок: ${NUM(m.votes)} от ${NUM(m.voters)} ${pl(m.voters || 0, "человека", "человек", "человек")}`,
    `• Вызовов: ${NUM(m.duels)}, сыграно до конца: ${NUM(m.duels_done)}; плейлистов людей: ${NUM(m.playlists)}`,
    `• Пожертвований: ${NUM(m.stars)} ⭐ (вывод от 1000 ⭐, каждая звезда доступна через 21 день)`,
  ];
  if (chats.results.length) lines.push("", "<b>Бота добавили в чаты</b>", ...chats.results.map(c => `   • ${c.type === "channel" ? "канал" : "группа"} «${esc(c.title || "?")}»${c.username ? " @" + esc(c.username) : ""}`));
  lines.push("", `<i>В скобках — ${prevLabel}. Цифры включают и твои действия. Другой период: /stats 1 (сегодня), /stats 7, /stats 30.</i>`);
  return lines.join("\n");
}

// ---------------- личка: команды, оплата ----------------
const START_TEXT = "📀 Архив альбомов канала @volodyamusicbox: обложки, годы, стили, оценки, плейлисты и очередь «послушаю позже».";
const TERMS_TEXT = "<b>Условия</b>\nБот и архив бесплатные, платных функций нет. Пожертвование звёздами добровольное: оно ничего не открывает, не ускоряет и не даёт взамен.\n" +
  "Вернуть звёзды можно самому в течение 21 дня — команда /paysupport.\n\n" +
  "<b>Что хранит бот</b>\nТвой Telegram ID и имя — если ты нажал /start, добавил альбом в «послушаю позже», отправил вызов или пожертвование. Оценки и плейлисты хранятся под зашифрованным ID. " +
  "Напоминания приходят только про альбомы, которые ты сам отложил; отключаются в архиве, в разделе «Послушаю позже». Данные никому не передаются.\n" +
  "Удалить всё, что бот о тебе хранит, — команда /forget.";

function invoice(amount) {
  return { title: "Поддержать архив", description: "Добровольное пожертвование автору канала @volodyamusicbox. Ничего не открывает и не даёт взамен.",
    payload: "don:" + amount, provider_token: "", currency: "XTR", prices: [{ label: "Пожертвование", amount }] };
}
const say = (env, chat_id, text, extra) => tgCall(env, "sendMessage", { chat_id, text, parse_mode: "HTML", link_preview_options: { is_disabled: true }, ...(extra || {}) });
// человек написал боту — значит, боту можно писать ему
const knowPerson = (env, u) => env.DB.prepare(
  "INSERT INTO people (tg_id, name, first, dm) VALUES (?, ?, ?, 1) ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name, dm = 1, blocked = 0")
  .bind(u.id, handle(u), Date.now()).run();

// true — сообщение разобрано здесь, в очередь бота на GitHub не идёт
async function onPrivate(m, env) {
  const uid = m.from.id, owner = String(uid) === String(env.OWNER_ID || "");
  if (m.successful_payment) {
    const p = m.successful_payment;
    await env.DB.prepare("INSERT OR IGNORE INTO donations (charge, tg_id, amount, at) VALUES (?, ?, ?, ?)")
      .bind(p.telegram_payment_charge_id, uid, p.total_amount, Date.now()).run();
    await track(env, ["donate"]);
    await say(env, uid, `Спасибо. ${p.total_amount} ⭐ дошли.\nЕсли отправил по ошибке — /paysupport, вернуть можно в течение 21 дня.`);
    if (env.OWNER_ID && !owner) await say(env, env.OWNER_ID, `⭐ Пожертвование: ${p.total_amount} от ${esc(handle(m.from))}`);
    return true;
  }
  const [cmd, arg = ""] = (m.text || "").trim().split(/\s+/, 2);
  const c = cmd.split("@")[0].toLowerCase();
  if (c === "/start") {
    await knowPerson(env, m.from).catch(() => {});
    await track(env, ["start"]).catch(() => {});
    const album = (arg.match(/^album_(\d+)$/) || [])[1];
    await say(env, uid, START_TEXT, { reply_markup: { inline_keyboard: [[{ text: "🗄 Открыть архив", url: env.MINIAPP + (album ? `?startapp=album_${album}` : "") }]] } });
    return true;
  }
  if (c === "/terms" || c === "/privacy") { await say(env, uid, TERMS_TEXT); return true; }
  if (c === "/support") { await say(env, uid, "Вопросы и ошибки — в комментариях канала @volodyamusicbox. По звёздам — /paysupport."); return true; }
  if (c === "/donate") {
    await say(env, uid, "Пожертвование звёздами — добровольное, ничего не открывает и не даёт взамен. Сколько?",
      { reply_markup: { inline_keyboard: [[15, 50, 100, 250].map(n => ({ text: `${n} ⭐`, callback_data: "dn:" + n }))] } });
    return true;
  }
  if (c === "/paysupport") {
    const { results } = await env.DB.prepare("SELECT rowid AS n, amount, at FROM donations WHERE tg_id = ? AND refunded = 0 AND at > ? ORDER BY at DESC LIMIT 8")
      .bind(uid, Date.now() - 21 * 86400000).all();
    if (!results.length) { await say(env, uid, "Пожертвований за последние 21 день от тебя нет — возвращать нечего.\nПожертвования добровольные и ничего не открывают; другие вопросы — в комментариях канала @volodyamusicbox."); return true; }
    await say(env, uid, "Твои пожертвования за 21 день. Любое можно вернуть — звёзды придут обратно сразу.",
      { reply_markup: { inline_keyboard: results.map(r => [{ text: `Вернуть ${r.amount} ⭐ от ${new Date(r.at).toLocaleDateString("ru-RU")}`, callback_data: "rf:" + r.n }]) } });
    return true;
  }
  if (c === "/forget") {
    await say(env, uid, "Удалить всё, что бот о тебе хранит: очередь «послушаю позже», оценки, плейлисты, вызовы и разрешение писать в личку? Вернуть будет нельзя.",
      { reply_markup: { inline_keyboard: [[{ text: "Удалить всё", callback_data: "fg" }]] } });
    return true;
  }
  if (c === "/stats" && owner) { await say(env, uid, await statsText(env, +arg || 7)); return true; }
  return false;
}

// ---------------- «послушаю позже» ----------------
// Очередь человека и напоминания в личку. Тут нужен настоящий Telegram ID: иначе боту некуда писать.
const LATER = { max: 100, first: 3 * 86400000, snooze: 86400000, again: 3 * 86400000, tries: 5, perRun: 20, quiet: 20 * 3600000 };

async function laterApi(req, url, env, user, body) {
  const now = Date.now();
  const person = await env.DB.prepare("SELECT dm, blocked, remind FROM people WHERE tg_id = ?").bind(user.id).first();
  if (req.method !== "POST") return json({ error: "bad" });
  const album = Number(body.album);

  // напоминания вкл/выкл (очередь при этом остаётся)
  if (url.pathname === "/api/later/remind") {
    // dm: человек только что разрешил боту писать ему (запрос разрешения в мини-приложении)
    const dm = body.dm ? 1 : 0;
    await env.DB.prepare("INSERT INTO people (tg_id, name, first, remind, dm) VALUES (?, ?, ?, ?, ?) " +
      "ON CONFLICT(tg_id) DO UPDATE SET remind = excluded.remind, dm = MAX(dm, excluded.dm), blocked = CASE WHEN excluded.dm = 1 THEN 0 ELSE blocked END")
      .bind(user.id, handle(user), now, body.on ? 1 : 0, dm).run();
    return json({ ok: true });
  }
  if (!(album > 0)) return json({ error: "bad" });

  if (url.pathname === "/api/later/done") {
    await env.DB.prepare("UPDATE later SET done = ?, remind_at = NULL WHERE tg_id = ? AND album = ?").bind(body.on === false ? null : now, user.id, album).run();
    return json({ ok: true });
  }
  if (url.pathname !== "/api/later") return json({ error: "not found" });
  if (!body.on) {
    await env.DB.prepare("DELETE FROM later WHERE tg_id = ? AND album = ?").bind(user.id, album).run();
    return json({ ok: true });
  }
  const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM later WHERE tg_id = ? AND done IS NULL").bind(user.id).first();
  if (n && n.n >= LATER.max) return json({ error: "full" });
  // писать в личку можно, если человек нажимал /start или разрешил это в мини-приложении
  const dm = !!(user.allows_write_to_pm || body.dm || (person && person.dm && !person.blocked));
  await env.DB.batch([
    env.DB.prepare("INSERT INTO people (tg_id, name, first, dm) VALUES (?, ?, ?, ?) ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name, dm = MAX(dm, excluded.dm), blocked = CASE WHEN excluded.dm = 1 THEN 0 ELSE blocked END")
      .bind(user.id, handle(user), now, dm ? 1 : 0),
    env.DB.prepare("INSERT INTO later (tg_id, album, added, remind_at) VALUES (?, ?, ?, ?) ON CONFLICT(tg_id, album) DO UPDATE SET done = NULL, added = excluded.added, remind_at = excluded.remind_at, tries = 0")
      .bind(user.id, album, now, now + LATER.first),
  ]);
  await track(env, ["later:add"]).catch(() => {});
  return json({ ok: true, dm, remind: !person || !!person.remind });
}

const REMIND_LINES = [
  "Три дня назад ты отложил этот альбом. Послушал?",
  "Альбом всё ещё лежит в очереди. Послушал?",
  "Третье напоминание. Альбом сам себя не послушает.",
  "Четвёртое. Не давлю — просто веду учёт.",
  "Последнее напоминание. Дальше альбом просто лежит в очереди, без меня.",
];
const albumLine = a => `💽 <b>${esc(a.title)}</b> — ${esc(a.artist || "?")}${a.year ? ` (${a.year})` : ""}`;

async function sendReminders(env) {
  const now = Date.now();
  const { results } = await env.DB.prepare(
    `SELECT l.tg_id, l.album, l.tries FROM later l JOIN people p ON p.tg_id = l.tg_id
     WHERE l.done IS NULL AND l.remind_at IS NOT NULL AND l.remind_at <= ?1 AND p.dm = 1 AND p.blocked = 0 AND p.remind = 1
       AND (p.last_dm IS NULL OR p.last_dm < ?2) ORDER BY l.remind_at LIMIT 200`).bind(now, now - LATER.quiet).all();
  if (!results.length) return;
  const all = await archive(env), seen = new Set(), ops = [];
  let sent = 0;
  for (const r of results) {
    if (seen.has(r.tg_id) || sent >= LATER.perRun) continue;   // одному человеку — одно напоминание в сутки
    seen.add(r.tg_id);
    const a = all.find(x => x.id === r.album);
    if (!a) { ops.push(env.DB.prepare("DELETE FROM later WHERE tg_id = ? AND album = ?").bind(r.tg_id, r.album)); continue; }
    const tries = Math.min(r.tries, REMIND_LINES.length - 1);
    const text = `${REMIND_LINES[tries]}\n\n${albumLine(a)}`;
    const reply_markup = { inline_keyboard: [
      [{ text: "Послушал", callback_data: "ld:" + a.id }, { text: "Напомни завтра", callback_data: "ls:" + a.id }],
      [{ text: "Убрать из очереди", callback_data: "lx:" + a.id }, { text: "Открыть в архиве", url: `${env.MINIAPP}?startapp=album_${a.id}_r` }]] };
    const res = a.has_cover
      ? await tgCall(env, "sendPhoto", { chat_id: r.tg_id, photo: `${env.PAGES}covers/jpg/${a.id}.jpg`, caption: text, parse_mode: "HTML", reply_markup })
      : await tgCall(env, "sendMessage", { chat_id: r.tg_id, text, parse_mode: "HTML", reply_markup });
    if (res.ok) {
      sent++;
      const last = r.tries + 1 >= LATER.tries;
      ops.push(env.DB.prepare("UPDATE later SET tries = tries + 1, remind_at = ? WHERE tg_id = ? AND album = ?").bind(last ? null : now + LATER.again, r.tg_id, r.album),
               env.DB.prepare("UPDATE people SET last_dm = ? WHERE tg_id = ?").bind(now, r.tg_id));
    } else if (res.error_code === 403) {   // человек заблокировал бота — больше не пишем
      ops.push(env.DB.prepare("UPDATE people SET blocked = 1 WHERE tg_id = ?").bind(r.tg_id));
    } else if (res.error_code === 429) break;
  }
  if (sent) ops.push(env.DB.prepare("INSERT INTO stats (day, key, n) VALUES (?, 'dm:remind', ?) ON CONFLICT(day, key) DO UPDATE SET n = n + excluded.n").bind(dayKey(), sent));
  if (ops.length) await env.DB.batch(ops);
}

// кнопки под сообщениями бота в личке
async function onCallback(cq, env) {
  const uid = cq.from.id, data = cq.data || "", msg = cq.message;
  const answer = (text, alert) => tgCall(env, "answerCallbackQuery", { callback_query_id: cq.id, ...(text ? { text, show_alert: !!alert } : {}) });
  const edit = (text, keyboard) => {
    if (!msg) return null;
    const base = { chat_id: msg.chat.id, message_id: msg.message_id, parse_mode: "HTML", reply_markup: { inline_keyboard: keyboard || [] } };
    return msg.photo ? tgCall(env, "editMessageCaption", { ...base, caption: text }) : tgCall(env, "editMessageText", { ...base, text });
  };
  let m;
  if ((m = data.match(/^l([dsx]):(\d+)$/))) {
    const album = Number(m[2]), now = Date.now();
    const a = (await archive(env)).find(x => x.id === album);
    const line = a ? albumLine(a) : "";
    const openBtn = [{ text: "Открыть в архиве", url: `${env.MINIAPP}?startapp=album_${album}_r` }];
    if (m[1] === "d") {
      await env.DB.prepare("UPDATE later SET done = ?, remind_at = NULL WHERE tg_id = ? AND album = ?").bind(now, uid, album).run();
      await edit(`Отмечено: послушал.\n\n${line}`, [[{ text: "Поставить оценку", url: `${env.MINIAPP}?startapp=album_${album}_r` }]]);
      await track(env, ["dm:done"]);
      return answer("Отмечено");
    }
    if (m[1] === "s") {
      await env.DB.prepare("UPDATE later SET remind_at = ? WHERE tg_id = ? AND album = ? AND done IS NULL").bind(now + LATER.snooze, uid, album).run();
      await edit(`Ладно. Напомню завтра.\n\n${line}`, [openBtn]);
      await track(env, ["dm:snooze"]);
      return answer("Напомню завтра");
    }
    await env.DB.prepare("DELETE FROM later WHERE tg_id = ? AND album = ?").bind(uid, album).run();
    await edit(`Убрал из очереди.\n\n${line}`, [openBtn]);
    await track(env, ["dm:drop"]);
    return answer("Убрал");
  }
  if (data === "fg") {   // человек просит удалить его данные
    const h = await hmacHex(env.BOT_TOKEN, String(uid), 16);
    const [votes, likes] = await env.DB.batch([
      env.DB.prepare("SELECT album, v FROM votes WHERE uid = ?").bind(h),
      env.DB.prepare("SELECT pid FROM playlist_likes WHERE uid = ?").bind(h)]);
    const ops = votes.results.map(r => env.DB.prepare("UPDATE counts SET likes = MAX(likes - ?, 0), dislikes = MAX(dislikes - ?, 0) WHERE album = ?")
      .bind(r.v === 1 ? 1 : 0, r.v === -1 ? 1 : 0, r.album));
    likes.results.forEach(r => ops.push(env.DB.prepare("UPDATE playlists SET likes = MAX(likes - 1, 0) WHERE id = ?").bind(r.pid)));
    ops.push(
      env.DB.prepare("DELETE FROM votes WHERE uid = ?").bind(h), env.DB.prepare("DELETE FROM playlist_likes WHERE uid = ?").bind(h),
      env.DB.prepare("DELETE FROM playlist_likes WHERE pid IN (SELECT id FROM playlists WHERE uid = ?)").bind(h),
      env.DB.prepare("DELETE FROM playlists WHERE uid = ?").bind(h),
      env.DB.prepare("DELETE FROM duels WHERE a_id = ? OR b_id = ?").bind(uid, uid),
      env.DB.prepare("DELETE FROM later WHERE tg_id = ?").bind(uid), env.DB.prepare("DELETE FROM people WHERE tg_id = ?").bind(uid));
    await env.DB.batch(ops);
    await edit("Удалил. Оценки в самом приложении хранит Telegram — они сотрутся, если очистить данные мини-приложения.");
    return answer("Удалено");
  }
  if ((m = data.match(/^dn:(\d+)$/))) {
    const amount = Number(m[1]);
    if (amount >= 1 && amount <= 2500) await tgCall(env, "sendInvoice", { chat_id: uid, ...invoice(amount) });
    return answer();
  }
  if ((m = data.match(/^rf:(\d+)$/))) {
    const d = await env.DB.prepare("SELECT rowid AS n, charge, amount, at, refunded FROM donations WHERE rowid = ? AND tg_id = ?").bind(Number(m[1]), uid).first();
    if (!d || d.refunded) return answer("Это пожертвование уже возвращено", true);
    if (Date.now() - d.at > 21 * 86400000) return answer("Прошло больше 21 дня — вернуть уже нельзя", true);
    const r = await tgCall(env, "refundStarPayment", { user_id: uid, telegram_payment_charge_id: d.charge });
    if (!r.ok && !/ALREADY/i.test(r.description || "")) return answer("Не получилось вернуть. Попробуй позже.", true);
    await env.DB.prepare("UPDATE donations SET refunded = 1 WHERE rowid = ?").bind(d.n).run();
    await edit(`Вернул ${d.amount} ⭐.`);
    return answer("Звёзды возвращены");
  }
  return answer();
}

async function cron(env) {
  const now = new Date(), hour = now.getUTCHours();
  // напоминания — только днём по Москве (10:00–22:00), чтобы не будить
  if (hour >= 7 && hour <= 18) await sendReminders(env).catch(e => console.log("remind", e));
  // по понедельникам утром — сводка владельцу
  if (now.getUTCDay() === 1 && hour === 7 && env.OWNER_ID) {
    const done = await env.DB.prepare("SELECT value FROM meta WHERE key = 'digest'").first();
    if (!done || done.value !== dayKey()) {
      await env.DB.prepare("INSERT OR REPLACE INTO meta (key, value) VALUES ('digest', ?)").bind(dayKey()).run();
      await say(env, env.OWNER_ID, await statsText(env), { disable_notification: true });
    }
  }
  // раз в сутки — уборка: «кто был» храним 40 дней, счётчики — год с небольшим
  if (hour === 3) await env.DB.batch([
    env.DB.prepare("DELETE FROM seen WHERE day < ?").bind(dayKey(Date.now() - 40 * 86400000)),
    env.DB.prepare("DELETE FROM stats WHERE day < ?").bind(dayKey(Date.now() - 400 * 86400000)),
  ]);
}
