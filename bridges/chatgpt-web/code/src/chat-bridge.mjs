// chat-bridge.mjs
// Lightweight OpenAI-compatible chat/completions bridge over a real ChatGPT web tab.
//
// Drives an already-logged-in ChatGPT page in Chrome via raw Chrome DevTools
// Protocol (no playwright — connectOverCDP was unreliable against Chrome 149 over
// adb-forwarded endpoints).
//
// Text enters the composer as chunked paste events. execCommand("insertText") was
// measured at 140s for 12k chars (and minutes for a full transcript) because every
// call makes ProseMirror re-process the whole document; a paste is one transaction,
// and the same 35k transcript lands in ~6s. Chunks stay under PASTE_CHUNK_SIZE
// because ChatGPT converts any single paste beyond ~9k chars into a "pasted text"
// attachment instead of inline text.
//
// Env:
//   CDP_ENDPOINT                 default 127.0.0.1:9222
//   BRIDGE_PORT                  default 8766
//   CHATGPT_NO_PROGRESS_TIMEOUT_MS  default 120000  (give up only after this long
//                                                    with no sign of work)
//   CHATGPT_ANSWER_HARD_CAP_MS      default 1800000 (backstop)
//   CHATGPT_STABLE_FOR_MS        default 2500
//   CHATGPT_MAX_TURNS_PER_CHAT   default 10
//   CHATGPT_MAX_CHARS_PER_CHAT   default 180000
//   CHATGPT_SEND_ATTEMPTS        default 2
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";

const CDP_ENDPOINT = process.env.CDP_ENDPOINT || "127.0.0.1:9222";
const PORT = Number(process.env.BRIDGE_PORT || 8766);
// Waiting is driven by progress, not by a fixed deadline. A hard question can
// spend a minute thinking, another minute searching, then ~50s reasoning with the
// visible text completely unchanged, before streaming an answer for several more
// minutes - one measured run was still writing at 362s. Any fixed budget either
// cuts those off or makes genuinely stuck requests hang.
//
// The reliable "still working" signal is the stop button: it stays visible across
// thinking, searching and streaming. Text growth alone is not - it flatlines for
// long stretches mid-reasoning.
const NO_PROGRESS_TIMEOUT_MS = Number(process.env.CHATGPT_NO_PROGRESS_TIMEOUT_MS || 120000);
// Backstop only, for a page that lies about still generating.
const ANSWER_HARD_CAP_MS = Number(process.env.CHATGPT_ANSWER_HARD_CAP_MS || 1800000);
const STABLE_FOR_MS = Number(process.env.CHATGPT_STABLE_FOR_MS || 2500);
const MAX_TURNS = Number(process.env.CHATGPT_MAX_TURNS_PER_CHAT || 10);
const MAX_CHARS = Number(process.env.CHATGPT_MAX_CHARS_PER_CHAT || 180000);
const CHAT_BASE_URL = process.env.CHATGPT_WEB_URL || "https://chatgpt.com/";
const POLL_MS = 600;

// One send may be retried once, from a freshly reset page. Anything beyond that
// has never recovered in practice — it just burns the caller's timeout budget.
const SEND_ATTEMPTS = Number(process.env.CHATGPT_SEND_ATTEMPTS || 2);

const PROMPT_SELECTOR = "#prompt-textarea";
const SEND_SELECTOR = "button[data-testid='send-button']";
const STOP_SELECTOR = "button[data-testid='stop-button']";
// regenerate-thread-error-button is the one that appears under the "Something
// went wrong" card; the other two cover the ordinary regenerate controls.
const RETRY_SELECTOR =
  "button[data-testid='regenerate-thread-error-button'], " +
  "button[data-testid='retry-button'], button[data-testid='regenerate-button']";
const ERROR_RETRY_SELECTOR = "button[data-testid='regenerate-thread-error-button']";

// An image-only answer carries no .markdown, no text and not even a
// data-message-author-role attribute - just an <img>. Without looking for the
// image directly the bridge saw "answer=0 chars", waited out the no-progress
// timeout and failed the request twice, so a generated picture cost four minutes
// and produced nothing.
const ANSWER_IMAGE_MIN_PX = 200;
// chat-bridge runs in Termux; AstrBot runs inside the proot rootfs, which lives
// under Termux's home. Writing here means AstrBot sees the same file at
// IMAGE_PATH_PREFIX, which is the path handed to send_message_to_user.
const IMAGE_OUTPUT_DIR =
  process.env.CHATGPT_IMAGE_DIR ||
  "/data/data/com.termux/files/home/ubuntu-rootfs/root/chatgpt-images";
const IMAGE_PATH_PREFIX = process.env.CHATGPT_IMAGE_PATH_PREFIX || "/root/chatgpt-images";
const IMAGE_DOWNLOAD_TIMEOUT_MS = Number(process.env.CHATGPT_IMAGE_TIMEOUT_MS || 120000);
const IMAGE_RETENTION_MS = Number(process.env.CHATGPT_IMAGE_RETENTION_MS || 24 * 60 * 60 * 1000);
const ASSISTANT_SELECTOR = "[data-testid^='conversation-turn-'] .markdown";
// Search answers carry inline citation pills. On screen they are small chips, but
// innerText flattens each one into the prose as stray lines ("ISO" / "+1") in the
// middle of a sentence, so they are hidden before the answer is read.
const CITATION_PILL_SELECTOR = "[data-testid*='citation-pill']";
const ERROR_SELECTORS = [
  "[data-testid='conversation-turn-error']",
  "[data-testid='error-message']",
];
// The "Something went wrong while generating the response" card is a bare <p>
// with no testid and no class, and it lands *inside* .markdown - so without this
// the bridge read it as the assistant's answer and relayed it to the chat.
const ERROR_TEXT_PATTERN =
  /Something went wrong while generating|生成回复时出错|出了点问题|Something went wrong\./i;
const MAX_WEB_RETRIES = Number(process.env.CHATGPT_WEB_ERROR_RETRIES || 1);

// ChatGPT persists the composer draft here. It survives reload and navigation, so
// text left behind by a failed send comes back and the next attempt appends on top
// of it — that is how the composer reached 23k chars of stale text in the field.
// Dropping this key is the only cheap way to clear a composer that is already big:
// selectAll+delete on 23k chars wedged the renderer for over 90 seconds.
const DRAFT_STORAGE_KEY = "oai/apps/conversationDrafts";

// Kept under the ~9k threshold where ChatGPT turns a paste into an attachment.
const PASTE_CHUNK_SIZE = Number(process.env.CHATGPT_PASTE_CHUNK_SIZE || 4000);

// Timeouts. A slow renderer is normal here, so these are generous; what must not
// happen is a timeout tearing down the link and stacking a duplicate operation
// behind one the page is still executing.
const EVAL_TIMEOUT_MS = 20000;
const PASTE_TIMEOUT_MS = 60000;
const NAV_TIMEOUT_MS = 60000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

process.on("unhandledRejection", (err) => {
  console.error("[chat-bridge] unhandledRejection:", err);
});
process.on("uncaughtException", (err) => {
  console.error("[chat-bridge] uncaughtException:", err);
});

class AbortedError extends Error {
  constructor() {
    super("Request cancelled by client");
    this.name = "AbortedError";
  }
}

class CdpClient {
  constructor(endpoint) {
    this.endpoint = endpoint;
    this.ws = null;
    this.msgId = 0;
    this.pending = new Map();
    this.connecting = null;
    // Bumped per socket so a late onclose from a replaced socket cannot null out
    // the socket that succeeded it (that race threw "Cannot set properties of
    // null (setting 'onclose')" and dropped healthy links).
    this.generation = 0;
  }

  isOpen() {
    return Boolean(this.ws && this.ws.readyState === WebSocket.OPEN);
  }

  // Single-flight: concurrent callers share one attempt instead of stampeding the
  // endpoint with a reconnect each.
  connect() {
    if (this.isOpen()) return Promise.resolve();
    if (this.connecting) return this.connecting;
    this.connecting = this._connect().finally(() => {
      this.connecting = null;
    });
    return this.connecting;
  }

  async _connect() {
    const res = await fetch(`http://${this.endpoint}/json`);
    if (!res.ok) throw new Error(`CDP endpoint unreachable (${res.status})`);
    const targets = await res.json();
    const page =
      targets.find((t) => t.type === "page" && (t.url || "").includes("chatgpt.com")) ||
      targets.find((t) => t.type === "page");
    if (!page) throw new Error("No page target in Chrome");

    const previous = this.ws;
    if (previous) {
      previous.onclose = null;
      try {
        previous.close();
      } catch {}
    }

    const generation = ++this.generation;
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.id && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error
          ? p.reject(new Error(JSON.stringify(msg.error)))
          : p.resolve(msg.result);
      }
    };
    ws.onclose = () => {
      // Only disown the socket if it is still the current one.
      if (this.generation !== generation) return;
      const error = new Error("CDP connection closed");
      for (const p of this.pending.values()) p.reject(error);
      this.pending.clear();
      this.ws = null;
    };
    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = () => reject(new Error("CDP websocket failed to open"));
    });
    this.ws = ws;
    console.log(`[chat-bridge] connected to ${page.url}`);
  }

  // A timeout here means "the page did not answer in time", not "the link is
  // dead". The old code closed the socket on every timeout, which reconnected
  // mid-operation and let a retry stack on top of work the page was still doing.
  // The socket is only dropped when the socket itself fails.
  async send(method, params = {}, timeoutMs = EVAL_TIMEOUT_MS) {
    if (!this.isOpen()) await this.connect();
    return new Promise((resolve, reject) => {
      const id = ++this.msgId;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      try {
        this.ws.send(JSON.stringify({ id, method, params }));
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  async eval(expression, timeoutMs = EVAL_TIMEOUT_MS, { awaitPromise = false } = {}) {
    const r = await this.send(
      "Runtime.evaluate",
      { expression, returnByValue: true, awaitPromise },
      timeoutMs,
    );
    return r.result?.value;
  }
}

const cdp = new CdpClient(CDP_ENDPOINT);

// `dirty` marks a page whose composer may hold leftover text from a failed send.
// Such a page must be reset before reuse, otherwise the next paste appends to the
// leftovers and every operation on the composer gets slower until it wedges.
let chatState = { turnCount: 0, charCount: 0, dirty: false };
let queue = Promise.resolve();

const jsComposer = `(() => {
  const el = document.querySelector(${JSON.stringify(PROMPT_SELECTOR)});
  const btn = document.querySelector(${JSON.stringify(SEND_SELECTOR)});
  const form = el ? el.closest("form") : null;
  return JSON.stringify({
    hasEditor: !!el,
    chars: el ? (el.textContent || "").replace(/\\s/g, "").length : 0,
    sendReady: Boolean(btn && !btn.disabled && btn.offsetParent !== null),
    attachment: /在文本字段中显示|Show in text field/.test(form ? form.innerText || "" : ""),
    draftChars: (localStorage.getItem(${JSON.stringify(DRAFT_STORAGE_KEY)}) || "").length,
    errorCard: !!document.querySelector(${JSON.stringify(ERROR_RETRY_SELECTOR)}),
  });
})()`;

// Serialise the answer subtree instead of reading innerText. The numbers on an
// ordered list are CSS ::marker content, which innerText does not include, so a
// "1. 2. 3." list reached QQ as unlabelled lines. Walking the DOM keeps the list
// markers, nesting and code fences, which is what makes the antigravity replies
// read well - that path gets raw markdown text from the agy terminal, markers and
// all.
const jsSerializeAnswer = `
  const BULLET = "\\u2022 ";
  const INDENT = "  ";
  const isPill = (el) => el.matches && el.matches(${JSON.stringify(CITATION_PILL_SELECTOR)});

  // The markup is pretty-printed, so text nodes carry the source newlines and
  // indentation. Under white-space:normal the browser collapses those; doing the
  // same here stops every <br> from becoming a blank line.
  function tidy(text) {
    return text
      .replace(/[^\\S\\n]+/g, " ")
      .replace(/ *\\n */g, "\\n")
      .trim();
  }

  function renderInline(node) {
    let out = "";
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { out += child.nodeValue.replace(/\\s+/g, " "); continue; }
      if (child.nodeType !== 1) continue;
      if (isPill(child)) continue;
      const tag = child.tagName;
      if (tag === "BR") { out += "\\n"; continue; }
      if (tag === "CODE" && !child.closest("pre")) {
        out += "\\u0060" + child.textContent + "\\u0060";
        continue;
      }
      out += renderInline(child);
    }
    return out;
  }

  // separator is "\\n\\n" between top-level blocks, but "\\n" inside a list item:
  // the heading line, its explanation and its sub-bullets are one unit, and a
  // blank line between each of them made short lists very airy.
  function renderBlocks(container, depth, separator) {
    const sep = separator || "\\n\\n";
    const blocks = [];
    for (const child of container.children) {
      if (isPill(child)) continue;
      const tag = child.tagName;
      if (tag === "OL" || tag === "UL") {
        const ordered = tag === "OL";
        let counter = ordered ? (parseInt(child.getAttribute("start") || "1", 10) || 1) : 0;
        const items = [];
        for (const li of child.children) {
          if (li.tagName !== "LI") continue;
          const marker = ordered ? counter++ + ". " : BULLET;
          const inner = li.children.length
            ? renderBlocks(li, depth + 1, "\\n")
            : tidy(renderInline(li));
          const lines = inner.split("\\n");
          const head = marker + (lines.shift() || "");
          const pad = INDENT.repeat(depth + 1);
          const rest = lines.map((line) => (line.trim() ? pad + line : line));
          items.push([head].concat(rest).join("\\n"));
        }
        if (items.length) blocks.push(items.join("\\n"));
        continue;
      }
      if (tag === "PRE") {
        // A code block is a header (language name, copy/run buttons) plus the
        // code. pre.textContent glues them together - "Bash" + "mvn test" came
        // out as "Bashmvn test" - so read the code from <code>, and recover the
        // language from what is left once code and buttons are removed.
        const code = child.querySelector("code");
        const body = (code ? code.textContent : child.textContent).replace(/\\n+$/, "");
        let lang = "";
        if (code) {
          const header = child.cloneNode(true);
          header.querySelectorAll("code, button, svg").forEach((n) => n.remove());
          const word = (header.textContent || "").trim().split(/\\s+/)[0] || "";
          if (/^[A-Za-z0-9+#._-]{1,20}$/.test(word)) lang = word.toLowerCase();
        }
        const fence = "\\u0060\\u0060\\u0060";
        blocks.push(fence + lang + "\\n" + body + "\\n" + fence);
        continue;
      }
      if (tag === "HR") continue;
      if (tag === "TABLE") {
        const rows = [...child.querySelectorAll("tr")].map((tr) =>
          [...tr.children].map((cell) => tidy(renderInline(cell))).join(" | "),
        );
        if (rows.length) blocks.push(rows.join("\\n"));
        continue;
      }
      if (/^H[1-6]$/.test(tag) || tag === "P" || tag === "BLOCKQUOTE") {
        const text = tidy(renderInline(child));
        if (text) blocks.push(text);
        continue;
      }
      if (child.children.length) {
        const nested = renderBlocks(child, depth);
        if (nested) blocks.push(nested);
      } else {
        const text = tidy(renderInline(child));
        if (text) blocks.push(text);
      }
    }
    return blocks.filter(Boolean).join(sep);
  }
`;

// One round trip per poll instead of the three or four the old loop used, which
// tripled CDP traffic against a renderer that is already busy streaming a reply.
const jsAnswerState = `(() => {
  ${jsSerializeAnswer}
  // Belt and braces: also hide the pills so the tab shows what the bot sees.
  // Re-applied on every poll because a page reset drops the style.
  const styleId = "agy-hide-citation-pills";
  if (!document.getElementById(styleId)) {
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent =
      ${JSON.stringify(CITATION_PILL_SELECTOR)} + "{display:none !important;}";
    document.head.appendChild(style);
  }
  const els = document.querySelectorAll(${JSON.stringify(ASSISTANT_SELECTOR)});
  const generating = [...document.querySelectorAll(${JSON.stringify(STOP_SELECTOR)})]
    .some((e) => e.offsetParent !== null);
  const answer = els.length ? renderBlocks(els[els.length - 1], 0).trim() : "";
  // Pictures live outside .markdown, so look at the turn itself.
  const turns = document.querySelectorAll("[data-testid^='conversation-turn-']");
  const lastTurn = turns.length ? turns[turns.length - 1] : null;
  const images = lastTurn
    ? [...new Set(
        [...lastTurn.querySelectorAll("img")]
          .filter((im) => im.naturalWidth >= ${ANSWER_IMAGE_MIN_PX} && im.src)
          .map((im) => im.src),
      )]
    : [];
  let err = "";
  if (!generating) {
    for (const sel of ${JSON.stringify(ERROR_SELECTORS)}) {
      for (const el of document.querySelectorAll(sel)) {
        const t = (el.innerText || "").trim();
        if (t) { err = t; break; }
      }
      if (err) break;
    }
    // The error card carries no testid, so fall back to the retry button it
    // brings with it, and to the wording itself.
    if (!err && document.querySelector(${JSON.stringify(ERROR_RETRY_SELECTOR)})) {
      err = answer || "ChatGPT reported a generation error";
    }
    if (!err && ${ERROR_TEXT_PATTERN.toString()}.test(answer)) {
      err = answer;
    }
  }
  return JSON.stringify({
    count: els.length,
    text: answer,
    images,
    generating,
    err,
  });
})()`;

const jsRetryClick = `(() => {
  const btns = [...document.querySelectorAll(${JSON.stringify(RETRY_SELECTOR)})];
  for (let i = btns.length - 1; i >= 0; i -= 1) {
    const b = btns[i];
    if (b && b.offsetParent !== null) { b.click(); return true; }
  }
  return false;
})()`;
const jsStopClick = `[...document.querySelectorAll(${JSON.stringify(STOP_SELECTOR)})].forEach(b => b.click())`;

async function readComposer(timeoutMs = EVAL_TIMEOUT_MS) {
  const raw = await cdp.eval(jsComposer, timeoutMs);
  try {
    return JSON.parse(raw || "{}");
  } catch {
    return {};
  }
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw new AbortedError();
}

// The only reliable way back to a known-good page. Dropping the persisted draft is
// O(1) where clearing a large composer in place is not, and Page.navigate is driven
// by the browser process, so it still works while the renderer main thread is stuck.
async function resetPage(reason) {
  console.log(`[chat-bridge] resetting page (${reason})`);
  await cdp
    .eval(`localStorage.removeItem(${JSON.stringify(DRAFT_STORAGE_KEY)}); 1`, 10000)
    .catch(() => {});
  await cdp
    .send("Page.navigate", { url: CHAT_BASE_URL }, NAV_TIMEOUT_MS)
    .catch((error) => console.log(`[chat-bridge] navigate failed: ${error.message}`));

  const deadline = Date.now() + NAV_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(1000);
    const state = await readComposer(10000).catch(() => ({}));
    if (state.hasEditor && state.chars === 0 && !state.attachment) {
      chatState = { turnCount: 0, charCount: 0, dirty: false };
      return true;
    }
  }
  console.log("[chat-bridge] page did not come back clean after reset");
  return false;
}

// Paste beats typing by ~20x here, and each chunk is a single ProseMirror
// transaction. A chunk that times out is never re-pasted: the page may still apply
// it, and a duplicate paste is what used to corrupt the prompt and snowball.
async function pasteIntoEditor(text, signal) {
  for (let offset = 0; offset < text.length; offset += PASTE_CHUNK_SIZE) {
    throwIfAborted(signal);
    const chunk = text.slice(offset, offset + PASTE_CHUNK_SIZE);
    await cdp.eval(
      `(() => {
        const el = document.querySelector(${JSON.stringify(PROMPT_SELECTOR)});
        if (!el) return false;
        el.focus();
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        selection.removeAllRanges();
        selection.addRange(range);
        const dt = new DataTransfer();
        dt.setData("text/plain", ${JSON.stringify(chunk)});
        return el.dispatchEvent(
          new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }),
        );
      })()`,
      PASTE_TIMEOUT_MS,
    );
  }
}

async function clickSend() {
  const clicked = await cdp.eval(`(() => {
    const btn = document.querySelector(${JSON.stringify(SEND_SELECTOR)});
    if (btn && btn.offsetParent !== null) { btn.click(); return true; }
    return false;
  })()`);
  if (!clicked) {
    await cdp.eval(`(() => {
      const el = document.querySelector(${JSON.stringify(PROMPT_SELECTOR)});
      if (el) el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    })()`);
  }
}

// Last line of defence: the error card lives inside .markdown, so if detection
// ever misses it the text would be relayed to the chat as the assistant's reply.
function rejectErrorCard(text) {
  if (ERROR_TEXT_PATTERN.test(text)) {
    throw new Error(`ChatGPT web error: ${String(text).slice(0, 300)}`);
  }
  return text;
}

async function waitForAnswer({ beforeCount, beforeText, beforeImages, signal }) {
  const startedAt = Date.now();
  const hardDeadline = startedAt + ANSWER_HARD_CAP_MS;
  const known = new Set(beforeImages || []);
  let lastText = "";
  let lastImages = [];
  let stableSince = 0;
  let sawNew = false;
  let sawGenerating = false;
  let webRetries = 0;
  let lastProgressAt = startedAt;
  let nextSlowLogAt = startedAt + 120000;
  while (Date.now() < hardDeadline) {
    if (signal?.aborted) {
      await cdp.eval(jsStopClick).catch(() => {});
      throw new AbortedError();
    }
    let state;
    try {
      state = JSON.parse((await cdp.eval(jsAnswerState)) || "{}");
    } catch {
      await sleep(POLL_MS);
      continue;
    }
    const { count = 0, text = "", generating = false, err = "", images = [] } = state;
    const fresh = images.filter((src) => !known.has(src));
    if (!generating && err) {
      if (webRetries < MAX_WEB_RETRIES) {
        webRetries += 1;
        console.log(`[chat-bridge] web error, retrying (${webRetries}/${MAX_WEB_RETRIES})`);
        const clicked = await cdp.eval(jsRetryClick).catch(() => false);
        if (!clicked) console.log("[chat-bridge] no retry button found");
        // Forget the error card so it is not mistaken for the retried answer.
        sawGenerating = false;
        lastText = "";
        lastImages = [];
        stableSince = 0;
        lastProgressAt = Date.now();
        await sleep(2500);
        continue;
      }
      throw new Error(`ChatGPT web error: ${String(err).slice(0, 300)}`);
    }
    // "Still generating" counts as progress on its own: during the reasoning and
    // search phases the stop button is the only thing that moves.
    if (generating) {
      sawGenerating = true;
      lastProgressAt = Date.now();
    }
    if (
      count > beforeCount ||
      (text && text !== beforeText) ||
      (beforeCount === 0 && text) ||
      fresh.length ||
      sawGenerating
    ) {
      sawNew = true;
    }
    if (text && text !== lastText) {
      lastText = text;
      stableSince = Date.now();
      lastProgressAt = Date.now();
    }
    // A picture is an answer in its own right, and often the only one.
    if (fresh.length !== lastImages.length || fresh.some((s, i) => s !== lastImages[i])) {
      lastImages = fresh;
      stableSince = Date.now();
      lastProgressAt = Date.now();
    }
    if (
      sawNew &&
      (lastText || lastImages.length) &&
      stableSince &&
      Date.now() - stableSince >= STABLE_FOR_MS &&
      !generating
    ) {
      return { text: rejectErrorCard(lastText), images: lastImages };
    }

    const now = Date.now();
    if (now >= nextSlowLogAt) {
      nextSlowLogAt = now + 120000;
      console.log(
        `[chat-bridge] still waiting after ${Math.round((now - startedAt) / 1000)}s ` +
          `(generating=${generating}, answer=${lastText.length} chars, images=${lastImages.length})`,
      );
    }
    if (now - lastProgressAt >= NO_PROGRESS_TIMEOUT_MS) {
      const idle = Math.round((now - lastProgressAt) / 1000);
      if (sawNew && (lastText || lastImages.length)) {
        console.log(`[chat-bridge] no progress for ${idle}s; returning what we have`);
        return { text: rejectErrorCard(lastText), images: lastImages };
      }
      throw new Error(`ChatGPT made no progress for ${idle}s`);
    }
    await sleep(POLL_MS);
  }
  if (sawNew && (lastText || lastImages.length)) {
    return { text: rejectErrorCard(lastText), images: lastImages };
  }
  throw new Error("Timed out waiting for a ChatGPT assistant message");
}

// The image URL only works with the page's session cookies, so it has to be
// fetched inside the page and carried back over CDP.
async function downloadImages(srcs) {
  if (!srcs.length) return [];
  await fs.mkdir(IMAGE_OUTPUT_DIR, { recursive: true });
  const saved = [];
  for (const src of srcs) {
    try {
      const raw = await cdp.eval(
        `(async () => {
          const res = await fetch(${JSON.stringify(src)});
          if (!res.ok) return JSON.stringify({ ok: false, status: res.status });
          const bytes = new Uint8Array(await res.arrayBuffer());
          let binary = "";
          const CHUNK = 0x8000;
          for (let i = 0; i < bytes.length; i += CHUNK) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
          }
          return JSON.stringify({
            ok: true,
            type: res.headers.get("content-type") || "",
            b64: btoa(binary),
          });
        })()`,
        IMAGE_DOWNLOAD_TIMEOUT_MS,
        { awaitPromise: true },
      );
      const payload = JSON.parse(raw || "{}");
      if (!payload.ok || !payload.b64) {
        console.log(`[chat-bridge] image fetch failed (status=${payload.status})`);
        continue;
      }
      const ext = /png/i.test(payload.type)
        ? "png"
        : /webp/i.test(payload.type)
          ? "webp"
          : /gif/i.test(payload.type)
            ? "gif"
            : "jpg";
      const name = `chatgpt-${Date.now().toString(36)}-${saved.length}.${ext}`;
      const buffer = Buffer.from(payload.b64, "base64");
      await fs.writeFile(path.join(IMAGE_OUTPUT_DIR, name), buffer);
      saved.push(`${IMAGE_PATH_PREFIX}/${name}`);
      console.log(`[chat-bridge] saved image ${name} (${buffer.length} bytes)`);
    } catch (error) {
      console.log(`[chat-bridge] image download error: ${error.message}`);
    }
  }
  await pruneOldImages();
  return saved;
}

// Nothing else ever deletes these, and they are megabytes each.
async function pruneOldImages() {
  try {
    const cutoff = Date.now() - IMAGE_RETENTION_MS;
    for (const name of await fs.readdir(IMAGE_OUTPUT_DIR)) {
      const full = path.join(IMAGE_OUTPUT_DIR, name);
      const stat = await fs.stat(full).catch(() => null);
      if (stat && stat.isFile() && stat.mtimeMs < cutoff) await fs.unlink(full).catch(() => {});
    }
  } catch {}
}

function extractTextFromContent(content) {
  if (typeof content === "string") return content.trim();
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && part.type === "text" && typeof part.text === "string")
      .map((part) => part.text.trim())
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

const OUTPUT_FORMAT_INSTRUCTION = `【输出格式】请严格按照以下 YAML 信封输出你的回答，不要输出信封以外的任何内容：
<<<AGY_RESPONSE>>>
type: final
content: |-
  你的回答文本
<<<END_AGY_RESPONSE>>>
信封内的 content 每行缩进两个空格。`;

function buildTranscript(messages) {
  const lines = [];
  for (const message of messages || []) {
    if (!message) continue;
    const role = String(message.role || "user").toUpperCase();
    const text = extractTextFromContent(message.content);
    if (!text) continue;
    lines.push(`[${role}] ${text}`);
  }
  if (!lines.length) return "";
  return (
    "以下是我们之前的对话记录，请基于这些内容继续对话，直接回答最后的问题：\n\n" +
    OUTPUT_FORMAT_INSTRUCTION +
    "\n\n" +
    lines.join("\n\n")
  );
}

function buildDelta(messages) {
  // Always carry the persona (system/developer messages) on incremental turns,
  // mirroring antigravity bridge's control-message behaviour, so the persona
  // stays in effect for every message, not just the first.
  const controls = (messages || [])
    .filter((m) => m && (m.role === "system" || m.role === "developer"))
    .map((m) => `[${String(m.role).toUpperCase()}] ${extractTextFromContent(m.content)}`)
    .filter(Boolean);
  const userText = extractUserText(messages);
  if (!userText) return "";
  return [...controls, `[USER] ${userText}`, `[输出格式] ${OUTPUT_FORMAT_INSTRUCTION}`].join(
    "\n\n",
  );
}

const ENVELOPE_OPEN = "<<<AGY_RESPONSE>>>";
const ENVELOPE_CLOSE = "<<<END_AGY_RESPONSE>>>";

// Extract the answer from the AGY-style YAML envelope (same protocol as the
// antigravity bridge). Returns null when the page answered in plain text.
//
// The page half-follows the format often enough that this has to be forgiving:
// ChatGPT regularly ends the message without writing the closing marker, and
// writes the content flush left instead of indented two spaces. Requiring the
// full shape meant one missing marker dumped the raw "<<<AGY_RESPONSE>>> type:
// final ..." scaffolding straight into the chat.
function extractYamlAnswer(text) {
  const source = String(text);
  const openIndex = source.lastIndexOf(ENVELOPE_OPEN);
  if (openIndex < 0) return null;

  let body = source.slice(openIndex + ENVELOPE_OPEN.length);
  const closeIndex = body.indexOf(ENVELOPE_CLOSE);
  if (closeIndex >= 0) body = body.slice(0, closeIndex);

  const typeMatch = body.match(/type:\s*([a-z_]+)/i);
  const type = typeMatch ? typeMatch[1] : "final";
  const lines = body.split("\n");

  // Everything after the `content:` header is the answer. When that header is
  // missing too, fall back to skipping the leading YAML-ish header lines.
  let headerEnd = lines.findIndex((line) => /^\s*content:\s*\|-?\s*$/.test(line));
  if (headerEnd < 0) {
    headerEnd = -1;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || /^type:\s*[a-z_]+$/i.test(trimmed) || /^content:/.test(trimmed)) {
        headerEnd += 1;
        continue;
      }
      break;
    }
  }

  const contentLines = lines.slice(headerEnd + 1);
  while (contentLines.length && !contentLines[contentLines.length - 1].trim()) contentLines.pop();
  while (contentLines.length && !contentLines[0].trim()) contentLines.shift();

  // Dedent by the common indent rather than a fixed two spaces: a YAML block
  // scalar indents every line equally, and stripping a fixed amount would also
  // eat the relative indentation that marks nested list items.
  const indents = contentLines
    .filter((line) => line.trim())
    .map((line) => line.match(/^ */)[0].length);
  const common = indents.length ? Math.min(...indents) : 0;
  const content = normalizeVisibleContent(
    contentLines.map((line) => line.slice(common)).join("\n"),
  );
  return { type, content: content || null };
}

// Mirrors the antigravity bridge's _normalize_visible_content: markdown emphasis
// and heading markers are noise in QQ (nothing renders them), and runs of blank
// lines are what made short replies look so airy.
function normalizeVisibleContent(content) {
  const isItem = (line) => /^[ \t]*(?:[•·▪‣]|[-*+]\s|\d+[.)]\s)/.test(line);
  const isFence = (line) => line.trim().startsWith("```");
  const raw = String(content).replace(/\r\n?/g, "\n").split("\n");

  // Pass 1: drop markdown markers that nothing renders in QQ.
  let inFence = false;
  const cleaned = raw.map((line) => {
    if (isFence(line)) {
      inFence = !inFence;
      return line;
    }
    if (!inFence) {
      line = line.replace(/^(\s*)#{1,6}\s+/, "$1");
      line = line.replace(/\*\*(\S(?:[\s\S]*?\S)?)\*\*/g, "$1");
    }
    return line.replace(/\s+$/, "");
  });

  // Pass 2: collapse blank runs to a single blank line, and remove them entirely
  // between adjacent list items - the model writes each bullet as its own
  // paragraph, so a short list arrived looking very airy. Code fences are copied
  // through untouched, where blank lines are part of the content.
  inFence = false;
  const out = [];
  for (let index = 0; index < cleaned.length; index += 1) {
    const line = cleaned[index];
    if (isFence(line)) {
      inFence = !inFence;
      out.push(line);
      continue;
    }
    if (inFence || line.trim() !== "") {
      out.push(line);
      continue;
    }
    let next = index;
    while (next < cleaned.length && cleaned[next].trim() === "") next += 1;
    const previousLine = out.length ? out[out.length - 1] : "";
    const nextLine = next < cleaned.length ? cleaned[next] : "";
    const betweenItems =
      out.length > 0 && next < cleaned.length && isItem(previousLine) && isItem(nextLine);
    if (!betweenItems && out.length) out.push("");
    index = next - 1;
  }
  return out.join("\n").trim();
}

// Last line of defence: whatever we hand back, the protocol markers must never
// reach the user.
function stripEnvelopeMarkers(text) {
  return String(text)
    .split("\n")
    .filter((line) => {
      const trimmed = line.trim();
      return trimmed !== ENVELOPE_OPEN && trimmed !== ENVELOPE_CLOSE;
    })
    .join("\n")
    .trim();
}

function extractUserText(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || message.role !== "user") continue;
    const text = extractTextFromContent(message.content);
    if (text) return text;
  }
  return "";
}

// Get the composer to a state where `prompt` is the only thing in it.
async function prepareComposer(isFirstTurn, signal) {
  throwIfAborted(signal);
  let state = await readComposer().catch(() => ({}));

  // Anything left in the composer, an attachment chip, or a page we know we
  // abandoned mid-send: go back to a clean chat rather than clearing in place.
  const why = chatState.dirty
    ? "previous send left the page dirty"
    : !state.hasEditor
      ? "no prompt box"
      : state.attachment
        ? "stale attachment in composer"
        // An error card left over from an earlier turn would otherwise be read as
        // *this* request's failure on the first poll, and the retry would be
        // clicked on the wrong turn.
        : state.errorCard
          ? "an error card from an earlier turn is still on screen"
          : state.chars > 0
            ? `composer held ${state.chars} chars`
            : isFirstTurn && state.draftChars > 0
              ? `stale draft of ${state.draftChars} chars before a full transcript`
              : "";
  if (why) {
    if (!(await resetPage(why))) {
      throw new Error("Could not get the ChatGPT page back to a usable state");
    }
    state = await readComposer().catch(() => ({}));
  }
  if (!state.hasEditor) {
    throw new Error("ChatGPT prompt box not found; page may not be on chatgpt.com");
  }
}

async function sendPrompt(prompt, signal) {
  const startedAt = Date.now();
  await pasteIntoEditor(prompt, signal);

  // Wait for ProseMirror to finish ingesting the paste. Compare ignoring
  // whitespace: block-level newlines do not survive into textContent.
  const expected = prompt.replace(/\s/g, "").length;
  const deadline = Date.now() + 60000;
  let state = {};
  while (Date.now() < deadline) {
    throwIfAborted(signal);
    state = await readComposer().catch(() => ({}));
    if (state.attachment) {
      throw new Error("ChatGPT turned the prompt into an attachment instead of text");
    }
    if (state.sendReady && state.chars >= expected * 0.98) break;
    await sleep(250);
  }
  if (!state.sendReady || state.chars < expected * 0.98) {
    throw new Error(
      `Composer did not accept the prompt (got ${state.chars || 0}/${expected} chars, sendReady=${Boolean(state.sendReady)})`,
    );
  }
  console.log(
    `[chat-bridge] send stage took ${(Date.now() - startedAt) / 1000}s (${prompt.length} chars)`,
  );
  await clickSend();
}

async function askOnce(messages, signal) {
  await prepareComposer(chatState.turnCount === 0, signal);

  // Decide transcript-vs-delta only after prepareComposer, because a reset starts a
  // brand new conversation and zeroes the turn counter. Reading this earlier would
  // send an incremental message into an empty chat and drop the whole history.
  const isFirstTurn = chatState.turnCount === 0;
  const prompt = isFirstTurn ? buildTranscript(messages) : buildDelta(messages);
  if (!prompt) throw new Error("No content to send");

  const before = await readAnswerState();
  // From here on the composer holds our text, so any failure leaves the page dirty
  // until it is proven sent.
  chatState.dirty = true;
  await sendPrompt(prompt, signal);

  const answer = await waitForAnswer({
    beforeCount: before.count,
    beforeText: before.text,
    beforeImages: before.images,
    signal,
  });
  const files = await downloadImages(answer.images || []);
  chatState.dirty = false;
  chatState.turnCount += 1;
  chatState.charCount += prompt.length + answer.text.length;
  return { text: answer.text, files };
}

async function readAnswerState() {
  try {
    const parsed = JSON.parse((await cdp.eval(jsAnswerState)) || "{}");
    return {
      count: Number(parsed.count) || 0,
      text: String(parsed.text || ""),
      images: Array.isArray(parsed.images) ? parsed.images : [],
    };
  } catch {
    return { count: 0, text: "", images: [] };
  }
}

async function askChat(messages, signal) {
  // Rotate on turn/char budget so the conversation DOM stays small.
  if (chatState.turnCount >= MAX_TURNS || chatState.charCount >= MAX_CHARS) {
    await resetPage(`rotation (turns=${chatState.turnCount}, chars=${chatState.charCount})`);
  }

  let lastError = null;
  for (let attempt = 1; attempt <= SEND_ATTEMPTS; attempt += 1) {
    throwIfAborted(signal);
    try {
      const { text, files } = await askOnce(messages, signal);
      let content;
      const parsed = extractYamlAnswer(text);
      if (parsed && parsed.content) {
        console.log(
          `[chat-bridge] yaml envelope parsed${text.includes(ENVELOPE_CLOSE) ? "" : " (no closing marker)"}`,
        );
        content = parsed.content;
      } else if (text.includes(ENVELOPE_OPEN) || text.includes(ENVELOPE_CLOSE)) {
        console.log("[chat-bridge] envelope present but unparseable; stripping markers");
        content = normalizeVisibleContent(stripEnvelopeMarkers(text));
      } else {
        content = normalizeVisibleContent(text);
      }
      return { content, files };
    } catch (error) {
      if (error instanceof AbortedError) {
        // The caller is gone; leave the page usable for the next request.
        await resetPage("client cancelled mid-send").catch(() => {});
        throw error;
      }
      lastError = error;
      console.log(`[chat-bridge] attempt ${attempt}/${SEND_ATTEMPTS} failed: ${error.message}`);
      if (attempt < SEND_ATTEMPTS) {
        // Always retry from a clean page; retrying on the dirty one is what used
        // to compound into multi-minute sends.
        await resetPage("retrying after failure").catch(() => {});
      }
    }
  }
  chatState.dirty = true;
  throw lastError || new Error("Send failed");
}

// Only a backstop. The real bound is the no-progress detector in waitForAnswer;
// a fixed 300s here used to cut off answers that were still streaming fine.
const REQUEST_TIMEOUT_MS = Number(process.env.CHATGPT_REQUEST_TIMEOUT_MS || 1800000);

// AstrBot runs an agent loop: once it has executed our send_message_to_user call
// it comes straight back for another turn. Re-prompting the page there makes
// ChatGPT draw a *second* picture, whose delivery triggers a third, and so on -
// the loop never ends. The image has already reached the chat by this point, so
// the turn is simply closed.
function imagesAlreadyDelivered(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message) continue;
    if (message.role === "user") return false;
    if (message.role !== "tool") continue;
    for (let back = index - 1; back >= 0; back -= 1) {
      const earlier = messages[back];
      if (!earlier) continue;
      if (earlier.role === "user") return false;
      if (earlier.role === "assistant" && Array.isArray(earlier.tool_calls)) {
        return earlier.tool_calls.some(
          (call) => call?.function?.name === "send_message_to_user",
        );
      }
    }
    return false;
  }
  return false;
}

async function handleChatCompletion(body, signal) {
  const messages = body.messages || [];
  if (imagesAlreadyDelivered(messages)) {
    console.log("[chat-bridge] image already delivered; closing the turn");
    return {
      id: `chatcmpl-web-${Date.now().toString(36)}`,
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: body.model || "chatgpt-web",
      choices: [
        { index: 0, message: { role: "assistant", content: "" }, finish_reason: "stop" },
      ],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    };
  }
  if (!extractUserText(messages)) throw new Error("No user text message found in request body");
  console.log(`[chat-bridge] request: ${messages.length} messages`);
  const { content, files } = await Promise.race([
    askChat(messages, signal),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Request timed out")), REQUEST_TIMEOUT_MS),
    ),
  ]);

  const message = { role: "assistant", content };
  let finishReason = "stop";
  if (files && files.length) {
    // Plain text cannot carry a picture to the chat platform, but a
    // send_message_to_user tool call can - the same route the antigravity
    // bridge uses for its own images.
    const parts = [];
    if (content) parts.push({ type: "plain", text: content });
    for (const file of files) parts.push({ type: "image", path: file });
    message.content = content || null;
    message.tool_calls = [
      {
        id: `call_${Date.now().toString(36)}`,
        type: "function",
        function: {
          name: "send_message_to_user",
          arguments: JSON.stringify({ messages: parts }),
        },
      },
    ];
    finishReason = "tool_calls";
    console.log(`[chat-bridge] returning ${files.length} image(s) as a tool call`);
  }

  return {
    id: `chatcmpl-web-${Date.now().toString(36)}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: body.model || "chatgpt-web",
    choices: [{ index: 0, message, finish_reason: finishReason }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

function createRequestCancellation(req, res) {
  const controller = new AbortController();
  const abort = () => {
    if (!controller.signal.aborted) controller.abort();
  };
  req.once("aborted", abort);
  res.once("close", () => {
    if (!res.writableEnded) abort();
  });
  return controller;
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  try {
    if (
      req.method === "GET" &&
      (url.pathname === "/health" || url.pathname === "/v1/health" || url.pathname === "/debug")
    ) {
      const pageInfo = await cdp.eval(
        "JSON.stringify({url: location.href, title: document.title})",
      );
      const composer = await readComposer().catch(() => ({}));
      sendJson(res, 200, {
        ok: true,
        page: JSON.parse(pageInfo || "{}"),
        composer,
        chat: chatState,
      });
      return;
    }
    if (req.method === "POST" && url.pathname === "/v1/chat/completions") {
      const body = await readJson(req);
      const controller = createRequestCancellation(req, res);
      const result = await new Promise((resolve, reject) => {
        const next = queue.then(() => {
          // Requests queue behind one another; by the time this one's turn comes
          // the caller may already have timed out and moved on. Running it would
          // just hold up whatever is behind it.
          if (controller.signal.aborted) throw new AbortedError();
          return handleChatCompletion(body, controller.signal);
        });
        queue = next.catch(() => {});
        next.then(resolve, reject);
      });
      sendJson(res, 200, result);
      return;
    }
    sendJson(res, 404, { error: { message: "not found", type: "not_found" } });
  } catch (error) {
    if (error instanceof AbortedError) {
      console.log("[chat-bridge] request cancelled by client");
      if (!res.writableEnded) res.destroy();
      return;
    }
    sendJson(res, 500, {
      error: {
        message: error instanceof Error ? error.message : String(error),
        type: "web_bridge_error",
        param: null,
        code: "web_bridge_error",
      },
    });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[chat-bridge] listening on http://127.0.0.1:${PORT} (cdp=${CDP_ENDPOINT})`);
});
