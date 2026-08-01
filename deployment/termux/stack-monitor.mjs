// stack-monitor.mjs
//
// Watches every hop of the bot stack and serves a status page.
//
// The existing napcat watchdog only answers "are QQ messages arriving". That
// missed a real outage: on 2026-08-01 a question sat unanswered for seven
// minutes because agy's persistent terminal stalled and the antigravity bridge
// returned 504, 500, 500 - while message flow, and therefore the watchdog, stayed
// perfectly healthy. AstrBot suppresses those error replies, so the group sees
// silence and nothing anywhere says why.
//
// Each link is probed on its own so a failure points at the hop that broke.
import http from "node:http";
import fs from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

const PORT = Number(process.env.STACK_MONITOR_PORT || 8799);
const INTERVAL_MS = Number(process.env.STACK_MONITOR_INTERVAL_MS || 60000);
const ROOTFS = "/data/data/com.termux/files/home/ubuntu-rootfs";
const ASTRBOT_LOG = `${ROOTFS}/root/AstrBot/astrbot.log`;
const BRIDGE_LOG = `${ROOTFS}/root/antigravity-bridge/bridge.log`;
// start-chat-bridge.sh redirects into $TMPDIR = files/tmp, NOT files/usr/tmp
const CHAT_BRIDGE_LOG = "/data/data/com.termux/files/tmp/chat-bridge.log";
const STATE_FILE = "/data/data/com.termux/files/usr/tmp/stack-monitor.json";

const TAIL_BYTES = 400_000; // enough for a day of errors without reading whole logs

// A link is judged on whether it is failing *now*. Judging on the day's running
// total kept a hop red until midnight even after it had visibly recovered.
const RECENT_MS = Number(process.env.STACK_MONITOR_RECENT_MS || 15 * 60_000);
// How long the group can stay quiet before silence is worth mentioning. Nobody
// asking is the normal state, not a fault, so this is deliberately generous.
const QUIET_GAP_S = Number(process.env.STACK_MONITOR_QUIET_GAP_S || 7200);

const stripAnsi = (s) => s.replace(/\[[0-9;]*m/g, "");
// The phone is UTC+8 while bridge.log stamps UTC, so "today" has to mean the
// local day - matching on the UTC date made the counters roll over at 08:00.
const today = () => new Date().toLocaleDateString("sv");

function localMidnight() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function ago(ms) {
  if (ms == null) return "";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 90) return `${s} 秒前`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} 分钟前`;
  return `${Math.round(m / 60)} 小时前`;
}

async function tail(path, bytes = TAIL_BYTES) {
  try {
    const handle = await fs.open(path, "r");
    try {
      const { size } = await handle.stat();
      const start = Math.max(0, size - bytes);
      const buffer = Buffer.alloc(Math.min(bytes, size));
      await handle.read(buffer, 0, buffer.length, start);
      return stripAnsi(buffer.toString("utf8"));
    } finally {
      await handle.close();
    }
  } catch {
    return "";
  }
}

async function fileSize(path) {
  try {
    return (await fs.stat(path)).size;
  } catch {
    return 0;
  }
}

async function processes() {
  try {
    const { stdout } = await run("/system/bin/ps", ["-ef"], { maxBuffer: 8 << 20 });
    return stdout;
  } catch {
    return "";
  }
}

function pidOf(psOut, needle) {
  for (const line of psOut.split("\n")) {
    if (!line.includes(needle) || line.includes("grep ")) continue;
    const parts = line.trim().split(/\s+/);
    if (parts.length > 1) return parts[1];
  }
  return null;
}

async function httpProbe(url, timeoutMs = 8000) {
  const started = Date.now();
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(url, { signal: controller.signal });
    clearTimeout(timer);
    // drain so the socket closes
    await res.text().catch(() => {});
    return { ok: res.status < 500, status: res.status, ms: Date.now() - started };
  } catch (error) {
    return { ok: false, status: 0, ms: Date.now() - started, error: error.message };
  }
}

// How stale is the newest QQ message AstrBot logged? The log stamps times only,
// so compare against the wall clock and treat a negative gap as a day rollover.
function lastMessageAgeSeconds(astrbotTail) {
  const lines = astrbotTail.split("\n");
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (!lines[i].includes("[qq(aiocqhttp)]")) continue;
    const m = lines[i].match(/^\[(\d{2}):(\d{2}):(\d{2})/);
    if (!m) continue;
    const now = new Date();
    const stamp = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
    // the log is UTC, the phone clock is local
    const nowUtc = now.getUTCHours() * 3600 + now.getUTCMinutes() * 60 + now.getUTCSeconds();
    let age = nowUtc - stamp;
    if (age < -3600) age += 86400;
    return age < 0 ? 0 : age;
  }
  return null;
}

// bridge.log stamps UTC in two shapes:
//   2026-08-01 14:18:03,066 INFO __main__: ...
//   [2026-08-01 14:18:08 +0000] [12567] [INFO] ...
const BRIDGE_TS = /^\[?(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/;

function bridgeTime(line) {
  const m = line.match(BRIDGE_TS);
  if (!m) return null;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
}

// astrbot.log stamps a UTC time of day with no date, so pin each stamp to the
// most recent instant that matches it - anything "ahead" of now is yesterday's.
function astrbotTime(line) {
  const m = line.match(/^\[(\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  const now = Date.now();
  const d = new Date(now);
  const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), +m[1], +m[2], +m[3]);
  return t > now + 60_000 ? t - 86_400_000 : t;
}

// astrbot.log carries no dates, so astrbotTime would map a stamp from three
// days ago onto today. Cut the tail at the last point where the clock jumps
// backwards - that is the newest midnight in the file - and only read past it.
function lastDaySegment(text) {
  const lines = text.split("\n");
  let cut = 0;
  let previous = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const m = lines[i].match(/^\[(\d{2}):(\d{2}):(\d{2})/);
    if (!m) continue;
    const t = Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
    if (previous >= 0 && t < previous - 3600) cut = i;
    previous = t;
  }
  return lines.slice(cut).join("\n");
}

// Split matches into "still happening" and "happened today", so a hop that has
// recovered reads as healthy while its damage for the day stays on record.
function scan(text, pattern, timeOf) {
  const now = Date.now();
  const dayStart = localMidnight();
  let todayCount = 0;
  let recent = 0;
  let last = null;
  for (const line of text.split("\n")) {
    if (!pattern.test(line)) continue;
    const t = timeOf(line);
    if (t === null) continue;
    if (t >= dayStart) todayCount += 1;
    if (now - t <= RECENT_MS) recent += 1;
    if (last === null || t > last) last = t;
  }
  return { today: todayCount, recent, last };
}

// chat-bridge.log carries no timestamps at all, so recency has to come from
// watching the file grow: count only what was appended since the last tick.
const tailOffsets = new Map();
const recentAppends = new Map();

function countAppended(path, text, size, key, pattern) {
  const previous = tailOffsets.get(path);
  tailOffsets.set(path, size);
  const events = recentAppends.get(key) || [];
  // A first sighting (or a truncated log) gives no usable baseline - the lines
  // already there could be from any time, so start the window empty.
  if (previous != null && size > previous) {
    const fresh = text.slice(Math.max(0, text.length - (size - previous)));
    const n = (fresh.match(pattern) || []).length;
    if (n) events.push({ t: Date.now(), n });
  }
  const cutoff = Date.now() - RECENT_MS;
  const kept = events.filter((e) => e.t >= cutoff);
  recentAppends.set(key, kept);
  return kept.reduce((sum, e) => sum + e.n, 0);
}

async function collect() {
  const ps = await processes();
  const [astrbotTail, bridgeTail, chatTail] = await Promise.all([
    tail(ASTRBOT_LOG),
    tail(BRIDGE_LOG),
    tail(CHAT_BRIDGE_LOG, 120_000),
  ]);

  const [astrbotHttp, agyHttp, chatHttp, cdpHttp] = await Promise.all([
    httpProbe("http://127.0.0.1:6185/"),
    httpProbe("http://127.0.0.1:8791/v1/models"),
    httpProbe("http://127.0.0.1:8766/health", 20000),
    httpProbe("http://127.0.0.1:9222/json/version"),
  ]);

  const msgAge = lastMessageAgeSeconds(astrbotTail);

  // What matters is whether the group got an answer, not how much retrying it
  // took to produce one. A stalled terminal that the fallback recovers is a
  // success - today 24 stalls cost only 6 real failures - so the stall count is
  // a footnote here and the completed/failed split drives the light.
  const agyDone = scan(bridgeTail, /Completed chat completion/, bridgeTime);
  const agyFail = scan(bridgeTail, /Failed chat completion/, bridgeTime);
  const stalls = scan(bridgeTail, /Persistent terminal worker failed/, bridgeTime);
  const stallFails = scan(bridgeTail, /Failed chat completion.*antigravity_terminal_/, bridgeTime);
  const recoveredStalls = Math.max(0, stalls.today - stallFails.today);
  const agy5xx = scan(bridgeTail, /POST \/v1\/chat\/completions 1\.1 5\d\d/, bridgeTime);
  const agyOk = scan(bridgeTail, /POST \/v1\/chat\/completions 1\.1 200/, bridgeTime);

  // The end-to-end truth from AstrBot's side: somebody asked and got nothing.
  const silent = scan(lastDaySegment(astrbotTail), /Suppressed runtime error reply/, astrbotTime);

  const astrbotDay = lastDaySegment(astrbotTail);
  const sends = scan(astrbotDay, /发送消息链失败/, astrbotTime);
  const suppressed = scan(astrbotTail, /Suppressed runtime error reply/, astrbotTime);

  const chatSize = await fileSize(CHAT_BRIDGE_LOG);
  const chatErrorsRecent = countAppended(
    CHAT_BRIDGE_LOG, chatTail, chatSize, "chat_errors", /attempt \d+\/\d+ failed/g,
  );
  const chatErrorsTotal = (chatTail.match(/attempt \d+\/\d+ failed/g) || []).length;
  const chatImages = (chatTail.match(/saved image /g) || []).length;

  const windowMin = Math.round(RECENT_MS / 60000);
  const napcatPid = pidOf(ps, "qq --no-sandbox");
  const astrbotPid = pidOf(ps, "AstrBot/main.py");
  const agyPid = pidOf(ps, "antigravity_openai_bridge");
  const chatPid = pidOf(ps, "chat-bridge.mjs");

  const links = [
    {
      id: "napcat",
      name: "NapCat / QQ",
      ok: Boolean(napcatPid),
      detail: napcatPid ? `进程在 (pid ${napcatPid})` : "QQ 进程不见了",
    },
    {
      id: "messages",
      name: "消息流入",
      // Quiet is not broken - nobody talks at 4am, and no @ means no reply is
      // supposed to happen. Only a gap long enough to be genuinely odd gets a
      // colour, and even then it stays yellow: QQ login is the watchdog's job.
      ok: msgAge !== null,
      warn: msgAge !== null && msgAge >= QUIET_GAP_S,
      detail:
        msgAge === null
          ? "日志里找不到 QQ 消息"
          : `最后一条消息 ${ago(Date.now() - msgAge * 1000)}` +
            (msgAge >= QUIET_GAP_S ? "（可能掉线，也可能只是没人说话）" : "（安静时段属正常）"),
    },
    {
      id: "astrbot",
      name: "AstrBot",
      ok: Boolean(astrbotPid) && astrbotHttp.ok,
      detail: `pid ${astrbotPid || "无"} · HTTP ${astrbotHttp.status} (${astrbotHttp.ms}ms)`,
    },
    {
      id: "agy_bridge",
      name: "反重力桥 :8791",
      // Just "is it alive" - whether requests succeed is the next row's job.
      ok: Boolean(agyPid) && agyHttp.ok,
      detail: `pid ${agyPid || "无"} · HTTP ${agyHttp.status} · 今日 200×${agyOk.today} / 5xx×${agy5xx.today}`,
    },
    {
      id: "agy_result",
      name: "反重力出结果",
      // Green as long as something is getting through: internal retries are an
      // implementation detail, only a request that produced nothing is a fault.
      ok: agyFail.recent === 0 || agyDone.recent > 0,
      warn: agyFail.recent > 0,
      detail:
        (agyDone.recent + agyFail.recent === 0
          ? `最近 ${windowMin} 分钟无请求`
          : agyFail.recent === 0
            ? `最近 ${agyDone.recent} 个请求都有回复`
            : `最近 ${agyDone.recent + agyFail.recent} 个请求里 ${agyFail.recent} 个没回复`) +
        ` · 今日 ${agyDone.today} 成 / ${agyFail.today} 败` +
        (agyDone.today + agyFail.today
          ? `（${((agyDone.today / (agyDone.today + agyFail.today)) * 100).toFixed(1)}%）`
          : "") +
        (stalls.today
          ? ` · 内部卡顿 ${stalls.today} 次，其中 ${recoveredStalls} 次自动救回`
          : ""),
    },
    {
      id: "chat_bridge",
      name: "ChatGPT 桥 :8766",
      ok: Boolean(chatPid) && chatHttp.ok,
      warn: chatErrorsRecent > 0,
      // This log carries no timestamps, so "recent" means what the file grew by
      // since the last tick; the totals are whole-log and have no date at all.
      detail:
        `pid ${chatPid || "无"} · HTTP ${chatHttp.status} · ` +
        (chatErrorsRecent > 0
          ? `最近失败 ${chatErrorsRecent} 次`
          : "最近无失败") +
        ` · 日志累计失败 ${chatErrorsTotal} 次 / 出图 ${chatImages} 张`,
    },
    {
      id: "cdp",
      name: "Chrome CDP :9222",
      ok: cdpHttp.ok,
      detail: `HTTP ${cdpHttp.status} (${cdpHttp.ms}ms)` + (cdpHttp.ok ? "" : " · 拔线重启手机后需重建 forward"),
    },
    {
      id: "qq_send",
      name: "QQ 发送",
      ok: sends.recent === 0,
      warn: sends.recent > 0,
      detail:
        sends.today === 0
          ? "今日无发送失败"
          : sends.recent === 0
            ? `现已恢复 · 上次失败 ${ago(sends.last)} · 今日累计 ${sends.today} 次`
            : `最近发送失败 ${sends.recent} 次 · 今日累计 ${sends.today} 次`,
    },
    {
      id: "silent",
      name: "报错被吞掉",
      // NOT "nobody asked" - this only fires when somebody did trigger the bot,
      // handling threw, and the error reply was suppressed, so the group saw
      // nothing. Like every other row, only a *recent* one is worth a colour.
      ok: silent.recent === 0,
      warn: silent.recent > 0,
      detail:
        (silent.recent > 0
          ? `最近 ${silent.recent} 次：有人提问→出错→报错被吞，群里看到的是沉默`
          : `最近 ${windowMin} 分钟没有`) +
        (silent.today
          ? ` · 今日 ${silent.today} 次` + (silent.recent === 0 ? `（上次 ${ago(silent.last)}）` : "")
          : " · 今日也没有"),
    },
  ];

  const bad = links.filter((l) => !l.ok);
  const warn = links.filter((l) => l.ok && l.warn);

  return {
    generated_at: new Date().toISOString(),
    generated_local: new Date().toLocaleString("sv"),
    overall: bad.length ? "down" : warn.length ? "warn" : "ok",
    links,
    today: {
      date: today(),
      agy_ok: agyOk.today,
      agy_5xx: agy5xx.today,
      agy_completed: agyDone.today,
      agy_failed: agyFail.today,
      agy_terminal_stalls: stalls.today,
      agy_stalls_recovered: recoveredStalls,
      qq_send_failures: sends.today,
      suppressed_errors: silent.today,
      chat_bridge_failures: chatErrorsTotal,
      images_generated: chatImages,
    },
    // what is still going wrong right now, as opposed to the day's running total
    recent: {
      window_minutes: windowMin,
      agy_completed: agyDone.recent,
      agy_failed: agyFail.recent,
      agy_terminal_stalls: stalls.recent,
      qq_send_failures: sends.recent,
      silent_replies: silent.recent,
      chat_bridge_failures: chatErrorsRecent,
      last_stall_at: stalls.last ? new Date(stalls.last).toISOString() : null,
    },
  };
}

let latest = { overall: "unknown", links: [], today: {}, generated_at: null };

async function tick() {
  try {
    latest = await collect();
    await fs.writeFile(STATE_FILE, JSON.stringify(latest, null, 2));
  } catch (error) {
    console.error(`[stack-monitor] collect failed: ${error.message}`);
  }
}

const COLOR = { ok: "#1a7f37", warn: "#9a6700", down: "#cf222e", unknown: "#57606a" };
const LABEL = { ok: "正常", warn: "有告警", down: "故障", unknown: "未知" };

function page() {
  const s = latest;
  const rows = (s.links || [])
    .map((l) => {
      const state = !l.ok ? "down" : l.warn ? "warn" : "ok";
      return `<tr>
        <td><span class="dot" style="background:${COLOR[state]}"></span></td>
        <td class="name">${l.name}</td>
        <td class="state" style="color:${COLOR[state]}">${LABEL[state]}</td>
        <td class="detail">${l.detail || ""}</td>
      </tr>`;
    })
    .join("");

  const t = s.today || {};
  const stats = [
    ["有回复", t.agy_completed],
    ["没回复", t.agy_failed],
    ["卡顿(已救回)", t.agy_stalls_recovered],
    ["卡顿(真失败)", (t.agy_terminal_stalls ?? 0) - (t.agy_stalls_recovered ?? 0)],
    ["QQ 发送失败", t.qq_send_failures],
    ["报错被吞掉", t.suppressed_errors],
    ["GPT桥失败(全量)", t.chat_bridge_failures],
    ["出图(全量)", t.images_generated],
  ]
    .map(
      ([k, v]) =>
        `<div class="stat"><div class="v">${v ?? "-"}</div><div class="k">${k}</div></div>`,
    )
    .join("");

  return `<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>机器人链路状态</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.6 -apple-system,Segoe UI,Roboto,"Helvetica Neue",sans-serif;
         margin: 0; padding: 24px; background: #f6f8fa; color: #1f2328; }
  @media (prefers-color-scheme: dark) { body { background:#0d1117; color:#e6edf3; }
    .card { background:#161b22 !important; border-color:#30363d !important; } }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #57606a; font-size: 13px; margin-bottom: 20px; }
  .card { background:#fff; border:1px solid #d0d7de; border-radius:10px; padding:18px; margin-bottom:18px; }
  .badge { display:inline-block; padding:3px 12px; border-radius:20px; color:#fff; font-size:13px; }
  table { width:100%; border-collapse:collapse; }
  td { padding:9px 6px; border-bottom:1px solid rgba(128,128,128,.18); vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; }
  .name { font-weight:600; white-space:nowrap; }
  .state { white-space:nowrap; }
  .detail { color:#57606a; font-size:13px; }
  .stats { display:flex; flex-wrap:wrap; gap:14px; }
  .stat { flex:1 1 96px; text-align:center; padding:10px 6px; border:1px solid rgba(128,128,128,.2); border-radius:8px; }
  .stat .v { font-size:22px; font-weight:600; }
  .stat .k { font-size:12px; color:#57606a; }
</style></head><body><div class="wrap">
<h1>机器人链路状态</h1>
<div class="sub">每 60 秒采集一次，页面每 30 秒自动刷新 · 数据时间 ${s.generated_local || "-"}<br>
灯看的是<b>用户有没有收到回复</b>，而且只看<b>最近 ${s.recent?.window_minutes ?? 15} 分钟</b>。中间卡了几次、重试了几次不算故障，只要最后发出去了就是绿的。下面的数字是今日累计，恢复了也不会归零。</div>
<div class="card">
  <div style="margin-bottom:14px"><span class="badge" style="background:${COLOR[s.overall] || COLOR.unknown}">${LABEL[s.overall] || "未知"}</span></div>
  <table>${rows || "<tr><td>尚未采集</td></tr>"}</table>
</div>
<div class="card">
  <div style="font-weight:600;margin-bottom:12px">今日累计（${t.date || "-"}）</div>
  <div class="stats">${stats}</div>
</div>
<div class="sub">原始数据：<a href="/status.json">/status.json</a></div>
</div></body></html>`;
}

const server = http.createServer((req, res) => {
  if (req.url === "/status.json") {
    const body = JSON.stringify(latest, null, 2);
    res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    res.end(body);
    return;
  }
  const body = page();
  res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  res.end(body);
});

await tick();
setInterval(tick, INTERVAL_MS);
// 0.0.0.0 so the PC can open it over WiFi, not just through adb forward
server.listen(PORT, "0.0.0.0", () => {
  console.log(`[stack-monitor] listening on http://0.0.0.0:${PORT} (interval ${INTERVAL_MS}ms)`);
});
