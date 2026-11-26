// TS Minicab Bot Bridge.
// Connects to the bot on this PC (ws://127.0.0.1:<port>) and runs its commands in this
// Chrome profile: open/reload tabs, run page scripts and send real clicks/keys through the
// Chrome debugger protocol. Only talks to 127.0.0.1, and only after both sides prove they
// know the shared token in config.js (written by `python run_bot.py --install-extension`).
importScripts("config.js");

let ws = null;
let ready = false;
const attached = new Set();

function badge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function connect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  if (!BRIDGE || !BRIDGE.token || BRIDGE.token === "SET-BY-INSTALL") { badge("cfg", "#b00"); return; }
  ready = false;
  const nonce = crypto.randomUUID();
  try {
    ws = new WebSocket(`ws://127.0.0.1:${BRIDGE.port}/bridge`);
  } catch (e) { badge("off", "#777"); return; }
  ws.onopen = async () => {
    ws.send(JSON.stringify({ type: "hello", token: BRIDGE.token, nonce, version: chrome.runtime.getManifest().version }));
  };
  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (!ready) {
      // The bot must prove it knows the token too, otherwise ignore it.
      if (msg.type === "welcome" && msg.proof === await sha256(BRIDGE.token + nonce)) {
        ready = true; badge("on", "#070");
      } else { ws.close(); }
      return;
    }
    if (msg.type !== "cmd") return;
    let reply;
    try {
      reply = { type: "result", id: msg.id, ok: true, result: await handle(msg.cmd, msg.args || {}) };
    } catch (e) {
      reply = { type: "result", id: msg.id, ok: false, error: String((e && e.message) || e) };
    }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(reply));
  };
  ws.onclose = () => { ready = false; badge("off", "#777"); };
  ws.onerror = () => { ready = false; };
}

function waitComplete(tabId, timeoutMs = 60000) {
  return new Promise((resolve) => {
    const end = Date.now() + timeoutMs;
    const tick = async () => {
      try {
        const t = await chrome.tabs.get(tabId);
        if (t.status === "complete") return resolve(true);
      } catch (e) { return resolve(false); }
      if (Date.now() > end) return resolve(false);
      setTimeout(tick, 300);
    };
    setTimeout(tick, 300);
  });
}

async function ensureAttached(tabId) {
  if (attached.has(tabId)) return;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (e) {
    if (!/already attached/i.test(String(e && e.message))) throw e;
  }
  attached.add(tabId);
}

chrome.debugger.onDetach.addListener((src) => { attached.delete(src.tabId); });
chrome.tabs.onRemoved.addListener((tabId) => { attached.delete(tabId); });

async function handle(cmd, a) {
  switch (cmd) {
    case "ping":
      return "pong";
    case "findTabs": {           // tabs whose URL starts with a prefix
      const tabs = await chrome.tabs.query({});
      return tabs.filter(t => (t.url || "").startsWith(a.prefix)).map(t => ({ tabId: t.id, windowId: t.windowId, url: t.url }));
    }
    case "openWindow": {         // new window with these URLs
      const w = await chrome.windows.create({ url: a.urls, focused: false, state: "normal" });
      return w.tabs.map(t => ({ tabId: t.id, windowId: w.id }));
    }
    case "createTab": {
      let windowId = a.windowId;
      try { if (windowId) await chrome.windows.get(windowId); } catch (e) { windowId = undefined; }
      const t = await chrome.tabs.create({ url: a.url, windowId, active: false });
      return { tabId: t.id, windowId: t.windowId };
    }
    case "tabInfo": {
      try {
        const t = await chrome.tabs.get(a.tabId);
        return { exists: true, url: t.url || t.pendingUrl || "", status: t.status, active: t.active, windowId: t.windowId };
      } catch (e) { return { exists: false }; }
    }
    case "activate": {
      const t = await chrome.tabs.update(a.tabId, { active: true });
      return { windowId: t.windowId };
    }
    case "navigate": {
      await chrome.tabs.update(a.tabId, { url: a.url });
      return await waitComplete(a.tabId, a.timeoutMs);
    }
    case "reload": {
      await chrome.tabs.reload(a.tabId);
      return await waitComplete(a.tabId, a.timeoutMs);
    }
    case "cdp": {
      await ensureAttached(a.tabId);
      return await chrome.debugger.sendCommand({ tabId: a.tabId }, a.method, a.params || {});
    }
    case "detach": {
      if (attached.has(a.tabId)) { try { await chrome.debugger.detach({ tabId: a.tabId }); } catch (e) {} }
      attached.delete(a.tabId);
      return true;
    }
    default:
      throw new Error("unknown command " + cmd);
  }
}

// Keep trying to reach the bot; the bot pings every 20 s while connected.
chrome.alarms.create("bridge-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((al) => { if (al.name === "bridge-reconnect") connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
