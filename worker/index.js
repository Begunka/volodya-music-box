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
  if (m.chat.type === "private") return true;                       // личка: /start, оценки, пересылки
  if (m.is_automatic_forward) return !isAudio(m);                   // копия поста — для комментария
  return String(m.from && m.from.id) === String(env.OWNER_ID || ""); // в группах — только твои сообщения (дневник, /comment)
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

// ---------------- инлайн-режим ----------------

async function archive(env) {
  // архив берём с сайта; Cloudflare кэширует его на 5 минут, чтобы не дёргать GitHub зря
  const r = await fetch(env.PAGES + "data.json", { cf: { cacheTtl: 300, cacheEverything: true } });
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
  const query = norm(q.query.trim());
  let results;
  if (!query) {
    const rnd = all[Math.floor(Math.random() * all.length)];
    const fresh = [...all].sort((x, y) => (y.added || "").localeCompare(x.added || "")).slice(0, 8);
    results = [
      card(rnd, env, "🎲 Посоветовать случайный альбом"),
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
      cache_time: query ? 120 : 5, is_personal: !query,   // случайный альбом — у каждого свой
    }),
  });
}
