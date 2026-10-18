"use strict";

// ---------------------------------------------------------------------------
// Hilfsfunktionen (Mail-Daten sind fremd kontrolliert: nie innerHTML!)
// ---------------------------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const nf = new Intl.NumberFormat("de-DE");
const dtf = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium" });
const dtfl = new Intl.DateTimeFormat("de-DE", { dateStyle: "short", timeStyle: "short" });
const rtf = new Intl.RelativeTimeFormat("de-DE", { numeric: "auto" });

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return `${(b / 1024 ** i).toLocaleString("de-DE", { maximumFractionDigits: i ? 1 : 0 })} ${u[i]}`;
}
function fmtRel(ts) {
  if (!ts) return "–";
  const diff = ts - Date.now() / 1000;
  const abs = Math.abs(diff);
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 45) return rtf.format(Math.round(diff / 86400), "day");
  return dtf.format(new Date(ts * 1000));
}
function safeUrl(u) {
  try {
    const url = new URL(u);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
}

async function api(path, opts = {}) {
  const init = { headers: { "X-Mailsweep": "1" }, ...opts };
  if (opts.body && typeof opts.body !== "string") {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* leer */ }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : (Array.isArray(d) ? d.map(x => x.msg).join(", ") : `HTTP ${r.status}`));
  }
  return data;
}

function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 4000);
}

// Dialog: gibt den Wert des gedrückten Buttons zurück
function dialog(title, body, buttons) {
  const dlg = $("#dlg");
  $("#dlgTitle").textContent = title;
  const b = $("#dlgBody");
  b.replaceChildren(...(Array.isArray(body) ? body : [body]).map(x => (x instanceof Node ? x : h("p", {}, x))));
  $("#dlgMenu").replaceChildren(
    ...buttons.map(([label, value, cls]) => h("button", { class: "btn sm " + (cls || ""), value }, label))
  );
  return new Promise(res => {
    dlg.addEventListener("close", () => res(dlg.returnValue), { once: true });
    dlg.returnValue = "";
    dlg.showModal();
  });
}

// ---------------------------------------------------------------------------
// Zustand
// ---------------------------------------------------------------------------
const S = {
  group: "addr", q: "", sort: "count", folder: "", onlyUnsub: false, showKept: false, hideUnsub: false,
  offset: 0, limit: 100, rows: [], total: 0, sel: new Set(), status: null, special: {}, folders: [],
  maxCnt: 1, lastFinished: 0,
};
try {
  const saved = JSON.parse(localStorage.getItem("mailsweep.view") || "{}");
  for (const k of ["group", "sort", "onlyUnsub", "showKept", "hideUnsub"]) if (k in saved) S[k] = saved[k];
} catch { /* egal */ }
function saveView() {
  try {
    localStorage.setItem("mailsweep.view", JSON.stringify({
      group: S.group, sort: S.sort, onlyUnsub: S.onlyUnsub, showKept: S.showKept, hideUnsub: S.hideUnsub,
    }));
  } catch { /* egal */ }
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------
let statusTimer = null;
async function loadStatus() {
  clearTimeout(statusTimer);
  try {
    const st = await api("/api/status");
    S.status = st;
    $("#account").textContent = `${st.account} · ${st.host}`;
    $("#dryrun").classList.toggle("hidden", !st.dry_run);
    $("#version").textContent = "v" + st.version;
    $("#stMessages").textContent = nf.format(st.messages);
    $("#stBytes").textContent = fmtBytes(st.bytes);
    $("#stSenders").textContent = nf.format(st.senders);
    $("#stUnsub").textContent = nf.format(st.senders_with_unsub);

    const sy = st.sync;
    const prog = $("#progress");
    if (sy.running) {
      const pct = sy.total ? Math.round((sy.done / sy.total) * 100) : 0;
      $("#syncText").textContent = sy.total
        ? `${sy.phase} ${sy.folder ? "· " + sy.folder : ""} · ${nf.format(sy.done)} / ${nf.format(sy.total)}`
        : `${sy.phase || "Sync startet"}…`;
      prog.classList.remove("hidden");
      $("#progressBar").style.width = pct + "%";
      $("#syncBtn").disabled = true;
    } else {
      prog.classList.add("hidden");
      $("#syncBtn").disabled = false;
      if (sy.error) {
        $("#syncText").textContent = `Sync-Fehler: ${sy.error}`;
        $("#syncText").classList.add("log-err");
      } else {
        $("#syncText").classList.remove("log-err");
        $("#syncText").textContent = sy.finished ? `Letzter Sync ${fmtRel(sy.finished)}` : "Noch kein Sync";
      }
      if (sy.finished && sy.finished !== S.lastFinished) {
        const first = S.lastFinished === 0;
        S.lastFinished = sy.finished;
        if (!first) { loadSenders(true); toast("Sync abgeschlossen"); }
      }
    }
    // Während eines laufenden Erst-Syncs Liste regelmäßig auffrischen
    if (sy.running && sy.done && !S.sel.size && $("#tab-senders").offsetParent) loadSenders(true, true);
    statusTimer = setTimeout(loadStatus, sy.running ? 2500 : 15000);
  } catch (e) {
    $("#syncText").textContent = "Status nicht abrufbar: " + e.message;
    statusTimer = setTimeout(loadStatus, 10000);
  }
}

async function loadFolders() {
  try {
    const r = await api("/api/folders");
    S.folders = r.folders; S.special = r.special;
    const scan = (S.status && S.status.scan_folders) || [];
    const sel = $("#folder");
    sel.replaceChildren(h("option", { value: "" }, "Alle Ordner"),
      ...scan.map(f => h("option", { value: f }, (r.folders.find(x => x.name === f) || {}).display || f)));
    sel.value = S.folder;
    $("#ruleTarget").replaceChildren(...r.folders.map(f => h("option", { value: f.name }, f.display)));
  } catch (e) { /* Ordner kommen nach dem ersten Sync */ }
}

// ---------------------------------------------------------------------------
// Absenderliste
// ---------------------------------------------------------------------------
let loadSeq = 0;
async function loadSenders(reset = true, quiet = false) {
  if (reset) { S.offset = 0; if (!quiet) S.sel.clear(); }
  const seq = ++loadSeq;
  const p = new URLSearchParams({
    group: S.group, q: S.q, sort: S.sort, only_unsub: S.onlyUnsub, show_kept: S.showKept,
    show_unsubscribed: !S.hideUnsub, folder: S.folder,
    limit: reset ? Math.max(S.limit, quiet ? S.rows.length : 0) : S.limit, offset: reset ? 0 : S.offset,
  });
  try {
    const r = await api("/api/senders?" + p);
    if (seq !== loadSeq) return;
    S.total = r.total;
    S.rows = reset ? r.rows : S.rows.concat(r.rows);
    S.offset = S.rows.length;
    if (reset) S.maxCnt = Math.max(1, ...S.rows.map(x => x.cnt));
    S.totalMessages = r.total_messages;
    renderSenders();
  } catch (e) { toast(e.message, true); }
}

function methodBadges(r) {
  const out = [];
  if (r.oneclick) out.push(h("span", { class: "badge ok", title: "RFC 8058: Abmeldung per HTTPS-POST, ohne Webseite" }, "One-Click"));
  if (r.mailto) out.push(h("span", { class: "badge acc", title: "Abmeldung per E-Mail" }, "Mail"));
  if (r.link && !r.oneclick) out.push(h("span", { class: "badge", title: "Nur Web-Link, muss selbst geöffnet werden" }, "Link"));
  if (!r.has_unsub) out.push(h("span", { class: "muted" }, "–"));
  return out;
}

// Was mit diesem Absender schon passiert ist: abgemeldet, im Papierkorb, archiviert
function statusBadges(r, opts = {}) {
  const out = [];
  if (r.unsubscribed_at) out.push(h("span", { class: "badge acc", title: r.unsub_result }, "abgemeldet " + fmtRel(r.unsubscribed_at)));
  const m = r.moves;
  if (m) {
    const pre = m.dry ? "Test: " : "";
    const tip = m.dry ? "Nur im Testmodus (DRY_RUN), im Postfach ist nichts passiert" : `zuletzt ${fmtRel(m.last)}`;
    const cls = m.dry ? "badge warn" : "badge";
    if (m.trash) out.push(h("span", { class: cls + (m.dry ? "" : " err"), title: tip }, `${pre}${nf.format(m.trash)} im Papierkorb`));
    if (m.archive) out.push(h("span", { class: cls + (m.dry ? "" : " ok"), title: tip }, `${pre}${nf.format(m.archive)} archiviert`));
    if (m.move) out.push(h("span", { class: cls, title: tip }, `${pre}${nf.format(m.move)} verschoben`));
  }
  if (!out.length && !opts.quiet) out.push(h("span", { class: "muted" }, "offen"));
  return out;
}

function dkimBadge(r) {
  if (!r.dkim_known) return h("span", { class: "muted", title: "Server liefert keinen Authentication-Results-Header" }, "?");
  const ratio = r.dkim_pass / r.cnt;
  if (ratio >= 0.95) return h("span", { class: "badge ok", title: "laut Authentication-Results des Mailservers" }, "pass");
  if (ratio > 0) return h("span", { class: "badge warn", title: "Nur ein Teil der Mails ist gültig signiert" }, `${Math.round(ratio * 100)} %`);
  if (r.dkim_foreign) return h("span", { class: "badge err", title: "Signiert von einer fremden Domain: typisch für Spoofing" }, "fremd");
  return h("span", { class: "badge err", title: "Keine gültige DKIM-Signatur laut Mailserver" }, "fehlt");
}

function renderSenders() {
  const tb = $("#senderRows");
  const isAddr = S.group === "addr";
  tb.replaceChildren(...S.rows.map(r => {
    const selected = S.sel.has(r.key);
    const bar = h("span", { class: "bar" });
    bar.style.width = Math.max(4, Math.round((r.cnt / S.maxCnt) * 64)) + "px";
    const actions = h("div", { class: "act" },
      isAddr && r.has_unsub ? h("button", { class: "pri", onclick: e => { e.stopPropagation(); doUnsubscribe(r.key); } }, "Abmelden") : null,
      h("button", { onclick: e => { e.stopPropagation(); doAction([r.key], "archive"); } }, "Archivieren"),
      h("button", { class: "del", onclick: e => { e.stopPropagation(); doAction([r.key], "trash"); } }, "Löschen"),
      h("button", { title: r.kept ? "Wieder anzeigen" : "Aus der Liste ausblenden", onclick: e => { e.stopPropagation(); doAction([r.key], r.kept ? "unkeep" : "keep"); } }, r.kept ? "Einblenden" : "Ausblenden"),
    );
    return h("tr", { class: (selected ? "sel " : "") + (r.kept ? "dim" : "") },
      h("td", { class: "c-sel" }, h("input", {
        type: "checkbox", checked: selected, "aria-label": "Auswählen",
        onchange: e => { e.target.checked ? S.sel.add(r.key) : S.sel.delete(r.key); e.target.closest("tr").classList.toggle("sel", e.target.checked); renderBulk(); },
      })),
      h("td", {}, h("div", { class: "who", onclick: () => openDrawer(r) },
        h("span", { class: "n" }, r.name || r.key),
        h("span", { class: "a" }, isAddr ? r.key : `${r.key} · ${r.n_addr} Adresse${r.n_addr === 1 ? "" : "n"}`),
        r.rule_id ? h("span", {}, h("span", { class: "badge acc" }, "Regel aktiv")) : null,
      )),
      h("td", { class: "num" }, h("span", { class: "cnt" }, nf.format(r.cnt)), bar),
      h("td", { class: "num hide-s" }, fmtBytes(r.bytes)),
      h("td", { class: "hide-s nowrap", title: dtfl.format(new Date(r.last * 1000)) }, fmtRel(r.last)),
      h("td", { class: "hide-m" }, ...methodBadges(r)),
      h("td", { class: "hide-s" }, ...statusBadges(r)),
      h("td", { class: "hide-m" }, dkimBadge(r)),
      h("td", { class: "c-act" }, actions),
    );
  }));
  const empty = $("#emptyState");
  if (!S.rows.length) {
    empty.textContent = S.status && S.status.messages
      ? "Keine Absender für diese Filter."
      : "Noch keine Daten. Der erste Sync läuft automatisch kurz nach dem Start oder über „Jetzt synchronisieren“.";
    empty.classList.remove("hidden");
  } else empty.classList.add("hidden");
  $("#pagerInfo").textContent = `${nf.format(S.rows.length)} von ${nf.format(S.total)} ${isAddr ? "Absendern" : "Domains"} · ${nf.format(S.totalMessages || 0)} Mails`;
  $("#moreBtn").classList.toggle("hidden", S.rows.length >= S.total);
  $("#selAll").checked = S.rows.length > 0 && S.rows.every(r => S.sel.has(r.key));
  renderBulk();
}

function renderBulk() {
  const n = S.sel.size;
  $("#bulk").classList.toggle("hidden", n === 0);
  $("#bulkCount").textContent = `${n} ${S.group === "addr" ? "Absender" : "Domain" + (n === 1 ? "" : "s")} ausgewählt`;
}

// ---------------------------------------------------------------------------
// Aktionen
// ---------------------------------------------------------------------------
function countFor(keys) {
  return keys.reduce((s, k) => s + ((S.rows.find(r => r.key === k) || {}).cnt || 0), 0);
}

async function doAction(keys, action, opts = {}) {
  const labels = { archive: "archivieren", trash: "in den Papierkorb verschieben" };
  if (action === "archive" || action === "trash") {
    const n = opts.count ?? countFor(keys);
    const target = action === "archive" ? (S.special.archive || "Archiv") : (S.special.trash || "Papierkorb");
    if (!opts.skipConfirm) {
      const who = keys.length === 1 ? keys[0] : `${keys.length} ${S.group === "addr" ? "Absender" : "Domains"}`;
      const body = [
        `${nf.format(n)} Mails von ${who} ${labels[action]}?`,
        h("p", { class: "muted" }, `Ziel: „${target}“. ` + (action === "trash"
          ? "iCloud leert den Papierkorb nach 30 Tagen, bis dahin lässt sich alles zurückholen."
          : "Die Mails bleiben im Archiv vollständig erhalten.")),
      ];
      const v = await dialog(action === "archive" ? "Archivieren" : "In Papierkorb", body,
        [["Abbrechen", "", "ghost"], [action === "archive" ? "Archivieren" : "In Papierkorb", "ok", action === "trash" ? "danger" : ""]]);
      if (v !== "ok") return false;
    }
  }
  try {
    const r = await api("/api/action", { method: "POST", body: { group: S.group, keys, action } });
    if (action === "keep") toast(`${keys.length} ausgeblendet`);
    else if (action === "unkeep") toast(`${keys.length} wieder eingeblendet`);
    else toast(`${r.dry_run ? "[DRY-RUN] " : ""}${nf.format(r.moved)} Mails nach „${r.target}“ verschoben`);
    keys.forEach(k => S.sel.delete(k));
    await loadSenders(true, true);
    loadStatus();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

async function createRule(matchType, value, action, apply = true, target = "") {
  const r = await api("/api/rules", { method: "POST", body: { match_type: matchType, value, action, target_folder: target, apply_now: apply } });
  return r;
}

async function doBulkRule(action) {
  const keys = [...S.sel];
  const v = await dialog("Regel anlegen",
    [`Für ${keys.length} ${S.group === "addr" ? "Absender" : "Domains"} künftig automatisch ${action === "archive" ? "archivieren" : "in den Papierkorb verschieben"}?`,
     h("p", { class: "muted" }, "Vorhandene Mails werden sofort mit verschoben. Neue Mails erwischt die Regel beim nächsten Sync.")],
    [["Abbrechen", "", "ghost"], ["Regeln anlegen", "ok", action === "trash" ? "danger" : ""]]);
  if (v !== "ok") return;
  let moved = 0;
  for (const k of keys) {
    try { moved += (await createRule(S.group, k, action)).moved; }
    catch (e) { toast(`${k}: ${e.message}`, true); }
  }
  toast(`${keys.length} Regeln angelegt, ${nf.format(moved)} Mails verschoben`);
  S.sel.clear();
  loadSenders(true); loadStatus();
}

async function doUnsubscribe(addr) {
  const confirmV = await dialog("Abmelden",
    [h("p", {}, "Von ", h("strong", {}, addr), " abmelden?"),
     h("p", { class: "muted" }, "mailsweep lädt die neueste Mail dieses Absenders, prüft die DKIM-Signatur und meldet nur ab, wenn sie gültig ist, zum Absender passt und den Abmelde-Header abdeckt.")],
    [["Abbrechen", "", "ghost"], ["Abmelden", "ok"]]);
  if (confirmV !== "ok") return;

  let req = { addr, method: "auto", dns: "system" };
  toast("Prüfe Signatur und melde ab …");
  // Schleife: Nach einem Fehler bietet der Dialog Alternativen an, die direkt neu versuchen.
  for (;;) {
    let r;
    try { r = await api("/api/unsubscribe", { method: "POST", body: req }); }
    catch (e) { toast(e.message, true); return; }
    const v = await unsubResultDialog(addr, r);
    if (v === "fallback") {
      req = { addr, method: "oneclick", dns: "fallback" };
      toast(`Versuche erneut, Auflösung über ${r.alternatives.fallback_dns} …`);
      continue;
    }
    if (v === "mailto") {
      req = { addr, method: "mailto", dns: "system" };
      toast("Sende Abmelde-Mail …");
      continue;
    }
    if (v === "mark") {
      try {
        await api("/api/unsubscribe/mark", { method: "POST", body: { addr, done: true } });
        toast("Als abgemeldet markiert");
      } catch (e) { toast(e.message, true); }
    } else if (v === "archive" || v === "trash") {
      try {
        const rr = await createRule("addr", addr, v);
        toast(`Regel angelegt, ${nf.format(rr.moved)} Mails verschoben`);
      } catch (e) { toast(e.message, true); }
      loadStatus();
    }
    loadSenders(true, true);
    return;
  }
}

function unsubResultDialog(addr, r) {
  const row = S.rows.find(x => x.key === addr) || {};
  const alt = r.alternatives || {};
  const details = (r.dkim && r.dkim.detail) || [];
  const failed = r.status !== "ok" && r.status !== "manual";
  const body = [];

  if (r.status === "ok") body.push(h("p", {}, h("span", { class: "badge ok" }, r.via === "mailto" ? "per Mail" : "One-Click"), " ", r.message));
  else if (r.status === "manual") body.push(h("p", {}, r.message));
  else body.push(h("p", {}, h("span", { class: "badge err" }, r.status === "refused" ? "abgelehnt" : "Fehler"), " ", r.message));

  if (failed && r.code === "dkim")
    body.push(h("p", { class: "hint warn" }, "Ohne gültige Signatur kann das Spam sein. Spam nicht abmelden, sondern löschen: Ein Abmeldeklick bestätigt nur, dass deine Adresse aktiv ist."));

  const url = safeUrl(alt.url || r.url);
  if (url && (r.status === "manual" || failed)) {
    const note = [];
    if (!alt.url_verified) note.push("Signatur nicht bestätigt, nur öffnen, wenn du dem Absender traust.");
    if (r.code === "dns_blocked" || r.code === "dns_internal") note.push("Läuft der DNS-Filter netzweit, blockt er die Seite auch in deinem Browser.");
    body.push(h("div", { class: "unsub-link" },
      h("div", { class: "lbl" }, r.status === "manual" ? "Abmelde-Link" : "Selbst abmelden über den Link"),
      h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url),
      note.length ? h("div", { class: "muted small" }, note.join(" ")) : null));
  }
  if (details.length) body.push(h("details", {}, h("summary", { class: "muted" }, "DKIM-Prüfung"), h("ul", {}, ...details.map(d => h("li", { class: "mono" }, d)))));

  const n = row.cnt || 0;
  if (n) body.push(h("p", { class: "muted" }, `Und die ${nf.format(n)} vorhandenen Mails? Eine Regel fängt auch Nachzügler ab, die noch kommen.`));

  const buttons = [["Schließen", "", "ghost"]];
  if (failed && alt.fallback_dns) buttons.push([`Erneut über DNS ${alt.fallback_dns}`, "fallback", ""]);
  if (failed && alt.mailto && r.via !== "mailto") buttons.push(["Per Mail abmelden", "mailto", ""]);
  if (r.status !== "ok") buttons.push(["Als abgemeldet markieren", "mark", "ghost"]);
  buttons.push(["Archivieren + Regel", "archive", failed ? "ghost" : ""], ["Löschen + Regel", "trash", "danger"]);
  return dialog("Abmelden: " + addr, body, buttons);
}

// ---------------------------------------------------------------------------
// Detail-Drawer
// ---------------------------------------------------------------------------
async function openDrawer(row) {
  const key = row.key;
  $("#dTitle").textContent = row.name || key;
  $("#dSub").textContent = `${key} · ${nf.format(row.cnt)} Mails · ${fmtBytes(row.bytes)}`;
  const isAddr = S.group === "addr";
  $("#dActions").replaceChildren(
    isAddr && row.has_unsub ? h("button", { class: "btn sm", onclick: () => { closeDrawer(); doUnsubscribe(key); } }, "Abmelden") : null,
    h("button", { class: "btn sm ghost", onclick: async () => { if (await doAction([key], "archive")) closeDrawer(); } }, "Archivieren"),
    h("button", { class: "btn sm ghost", onclick: async () => { if (await doAction([key], "trash")) closeDrawer(); } }, "In Papierkorb"),
    h("button", { class: "btn sm ghost", onclick: () => ruleFromDrawer(key, "archive") }, "Regel: archivieren"),
    h("button", { class: "btn sm ghost", onclick: () => ruleFromDrawer(key, "trash") }, "Regel: löschen"),
  );
  $("#dBody").replaceChildren(h("p", { class: "muted" }, "Lade …"));
  $("#drawer").classList.remove("hidden"); $("#scrim").classList.remove("hidden");
  try {
    const d = await api(`/api/sender?${new URLSearchParams({ key, group: S.group })}`);
    const parts = [];
    parts.push(h("h4", {}, "Letzte Mails"));
    parts.push(h("ul", { class: "mlist" }, ...d.messages.map(m => h("li", {},
      h("span", { class: "s", title: m.subject }, m.subject || "(kein Betreff)"),
      h("span", { class: "d" }, dtfl.format(new Date(m.date * 1000))),
      h("span", { class: "f" }, `${m.folder} · ${fmtBytes(m.size)}${isAddr ? "" : " · " + m.from_addr}${m.list_id ? " · " + m.list_id : ""}`),
    ))));
    if (d.addresses.length > 1 || !isAddr) {
      parts.push(h("h4", {}, "Adressen"));
      parts.push(h("ul", { class: "mlist" }, ...d.addresses.map(a => h("li", {}, h("span", { class: "s mono" }, a.from_addr), h("span", { class: "d" }, nf.format(a.cnt))))));
    }
    if (d.list_unsubscribe) {
      parts.push(h("h4", {}, "List-Unsubscribe (neueste Mail)"));
      parts.push(h("div", { class: "raw" }, d.list_unsubscribe.list_unsub + (d.list_unsubscribe.list_unsub_post ? "\nList-Unsubscribe-Post: " + d.list_unsubscribe.list_unsub_post : "")));
    }
    if (d.state && d.state.unsub_result) {
      parts.push(h("h4", {}, "Abmeldung"));
      parts.push(h("p", {}, `${dtfl.format(new Date(d.state.unsubscribed_at * 1000))}: ${d.state.unsub_result}`));
    }
    $("#dBody").replaceChildren(...parts);
  } catch (e) { $("#dBody").replaceChildren(h("p", { class: "log-err" }, e.message)); }
}
function closeDrawer() { $("#drawer").classList.add("hidden"); $("#scrim").classList.add("hidden"); }
async function ruleFromDrawer(key, action) {
  try {
    const r = await createRule(S.group, key, action);
    toast(`Regel angelegt, ${nf.format(r.moved)} Mails verschoben`);
    closeDrawer(); loadSenders(true, true); loadStatus();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------------------
// Regeln & Protokoll
// ---------------------------------------------------------------------------
const actionLabel = r => r.action === "archive" ? `archivieren (${S.special.archive || "Archiv"})`
  : r.action === "trash" ? `Papierkorb (${S.special.trash || "Papierkorb"})` : `verschieben nach „${r.target_folder}“`;

async function loadRules() {
  try {
    const { rules } = await api("/api/rules");
    $("#ruleRows").replaceChildren(...(rules.length ? rules.map(r => h("tr", {},
      h("td", {}, h("span", { class: "muted" }, r.match_type === "addr" ? "Absender " : "Domain "), h("span", { class: "mono" }, r.value)),
      h("td", {}, actionLabel(r)),
      h("td", { class: "num" }, nf.format(r.hits)),
      h("td", { class: "hide-s" }, dtf.format(new Date(r.created * 1000))),
      h("td", { class: "c-act" }, h("div", { class: "act" }, h("button", { class: "del", onclick: async () => {
        const v = await dialog("Regel löschen", `Regel für ${r.value} löschen? Bereits verschobene Mails bleiben, wo sie sind.`, [["Abbrechen", "", "ghost"], ["Löschen", "ok", "danger"]]);
        if (v !== "ok") return;
        await api(`/api/rules/${r.id}`, { method: "DELETE" }); loadRules(); loadSenders(true, true);
      } }, "Löschen"))),
    )) : [h("tr", {}, h("td", { colspan: 5, class: "muted" }, "Noch keine Regeln. Am schnellsten legst du sie direkt nach dem Abmelden an."))]));
  } catch (e) { toast(e.message, true); }
}

async function loadDone() {
  try {
    const r = await api(`/api/done?${new URLSearchParams({ group: S.group, q: $("#doneQ").value.trim() })}`);
    const t = r.totals;
    $("#doneSummary").replaceChildren(
      h("div", {}, h("span", { class: "num" }, nf.format(t.trash)), h("span", { class: "lbl" }, "Mails im Papierkorb")),
      h("div", {}, h("span", { class: "num" }, nf.format(t.archive)), h("span", { class: "lbl" }, "Mails archiviert")),
      h("div", {}, h("span", { class: "num" }, fmtBytes(t.bytes)), h("span", { class: "lbl" }, "aus der Inbox geräumt")),
      h("div", {}, h("span", { class: "num" }, nf.format(t.unsubscribed)), h("span", { class: "lbl" }, S.group === "addr" ? "Absender abgemeldet" : "Domains abgemeldet")),
    );
    $("#doneHint").classList.toggle("hidden", !r.dry_run);
    const isAddr = S.group === "addr";
    $("#doneRows").replaceChildren(...(r.rows.length ? r.rows.map(x => {
      const m = x.moves;
      const cell = (n, cls) => n ? h("span", { class: "badge " + (m.dry ? "warn" : cls), title: m.dry ? "Nur Testmodus" : "" }, (m.dry ? "Test: " : "") + nf.format(n)) : h("span", { class: "muted" }, "–");
      return h("tr", {},
        h("td", {}, h("div", { class: "who", onclick: () => openDrawer({ key: x.key, name: x.name, cnt: x.remaining, bytes: 0, has_unsub: false }) },
          h("span", { class: "n" }, x.name || x.key), h("span", { class: "a" }, x.key))),
        h("td", { class: "nowrap" }, x.unsubscribed_at ? h("span", { class: "badge acc", title: x.unsub_result }, fmtRel(x.unsubscribed_at)) : h("span", { class: "muted" }, "–")),
        h("td", { class: "num" }, cell(m.trash, "err")),
        h("td", { class: "num" }, cell(m.archive, "ok")),
        h("td", { class: "hide-s" }, x.rule ? h("span", { class: "badge acc", title: `${nf.format(x.rule.hits)} Treffer` }, x.rule.action === "trash" ? "künftig löschen" : x.rule.action === "archive" ? "künftig archivieren" : "verschieben") : h("span", { class: "muted" }, "–")),
        h("td", { class: "num" }, x.remaining ? h("span", { class: "badge warn", title: "Liegen noch in den analysierten Ordnern" }, nf.format(x.remaining)) : h("span", { class: "muted" }, "0")),
        h("td", { class: "hide-s nowrap" }, fmtRel(x.last)),
        h("td", { class: "c-act" }, x.remaining ? h("div", { class: "act" },
          h("button", { class: "del", onclick: async () => { if (await doAction([x.key], "trash", { count: x.remaining })) loadDone(); } }, "Rest löschen")) : null),
      );
    }) : [h("tr", {}, h("td", { colspan: 8, class: "muted" }, "Noch nichts bearbeitet. Abgemeldete, gelöschte und archivierte Absender erscheinen hier."))]));
    $("#doneGroupLabel").textContent = isAddr ? "Absender" : "Domain";
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------------------
// Einstellungen
// ---------------------------------------------------------------------------
function showTab(name) { $(`.tab[data-tab="${name}"]`).click(); }

function renderDryCard(on) {
  $("#dryCard").classList.toggle("on", on);
  $("#dryTitle").textContent = on ? "Testmodus ist AN" : "Testmodus ist aus";
  $("#dryText").textContent = on
    ? "mailsweep verschiebt nichts und meldet nicht ab. Alle Aktionen landen nur als „Test“ im Protokoll."
    : "Aktionen werden wirklich ausgeführt: Mails werden verschoben, Abmeldungen verschickt.";
  const b = $("#dryToggle");
  b.textContent = on ? "Testmodus ausschalten" : "Testmodus einschalten";
  b.className = "btn" + (on ? " danger" : " ghost");
}

async function saveSettings(values, msg) {
  try {
    const r = await api("/api/settings", { method: "POST", body: { values } });
    toast(r.changed.length ? (msg || "Gespeichert") : "Keine Änderung");
    renderSettings(r); loadStatus();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

function renderSettings(r) {
  const byKey = Object.fromEntries(r.settings.map(x => [x.key, x]));
  renderDryCard(byKey.dry_run.value);
  const folderNames = (r.folders || []).map(f => f.name);
  const rows = r.settings.filter(x => x.key !== "dry_run").map(x => {
    let input;
    if (x.type === "bool") {
      input = h("label", { class: "chk" }, h("input", { type: "checkbox", name: x.key, checked: x.value }), "aktiv");
    } else if (x.type === "int") {
      input = h("input", { type: "number", name: x.key, value: x.value, min: x.min, max: x.max });
    } else if (x.type === "list" && folderNames.length) {
      const all = [...new Set([...folderNames, ...x.value])];
      input = h("div", { class: "folderchecks" }, ...all.map(n => h("label", { class: "chk" },
        h("input", { type: "checkbox", name: x.key, value: n, checked: x.value.includes(n) }),
        ((r.folders || []).find(f => f.name === n) || {}).display || n)));
    } else {
      input = h("input", { type: "text", name: x.key, value: Array.isArray(x.value) ? x.value.join(", ") : x.value, placeholder: x.placeholder ?? (x.type === "str" ? "automatisch" : "") });
    }
    const envVal = Array.isArray(x.env_value) ? x.env_value.join(", ") : (x.env_value === "" ? "leer" : String(x.env_value));
    return h("div", { class: "setrow" },
      h("div", {}, h("div", { class: "lbl" }, x.label), h("div", { class: "help" }, x.help)),
      h("div", {}, input),
      h("div", { class: "src" },
        h("span", { class: "badge " + (x.source === "app" ? "acc" : ""), title: `Wert aus mailsweep.env: ${envVal}` }, x.source === "app" ? "in App gesetzt" : "aus Env"),
        x.source === "app" ? h("button", { type: "button", class: "linkbtn", onclick: async () => {
          try { renderSettings(await api(`/api/settings/${x.key}`, { method: "DELETE" })); toast("Auf Env-Wert zurückgesetzt"); loadStatus(); }
          catch (e) { toast(e.message, true); }
        } }, "Zurücksetzen") : null),
    );
  });
  $("#settingsForm").replaceChildren(...rows, h("div", { class: "setfoot" }, h("button", { class: "btn" }, "Speichern")));
  $("#settingsForm").dataset.keys = JSON.stringify(r.settings.filter(x => x.key !== "dry_run").map(x => [x.key, x.type, folderNames.length > 0]));
  $("#aboutLine").textContent = `mailsweep v${r.version}`;
}

async function loadSettings() {
  try { renderSettings(await api("/api/settings")); } catch (e) { toast(e.message, true); }
}

async function loadLog() {
  try {
    const { log } = await api("/api/log?limit=300");
    $("#logRows").replaceChildren(...log.map(l => h("tr", {},
      h("td", { class: "nowrap mono" }, dtfl.format(new Date(l.ts * 1000))),
      h("td", {}, l.action),
      h("td", { class: "mono" }, l.target),
      h("td", { class: l.ok ? "" : "log-err" }, l.detail),
    )));
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------------------
// Verdrahtung
// ---------------------------------------------------------------------------
function initControls() {
  $$(".tab").forEach(t => t.addEventListener("click", () => {
    $$(".tab").forEach(x => x.classList.toggle("active", x === t));
    $$(".panel").forEach(p => p.classList.add("hidden"));
    $("#tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "rules") loadRules();
    if (t.dataset.tab === "log") loadLog();
    if (t.dataset.tab === "done") loadDone();
    if (t.dataset.tab === "settings") loadSettings();
    if (t.dataset.tab === "senders") loadSenders(true, true);
  }));

  let qTimer;
  $("#q").addEventListener("input", e => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = e.target.value.trim(); loadSenders(); }, 250); });
  $$("#groupSeg button").forEach(b => {
    b.classList.toggle("on", b.dataset.group === S.group);
    b.addEventListener("click", () => {
      S.group = b.dataset.group; $$("#groupSeg button").forEach(x => x.classList.toggle("on", x === b));
      saveView(); loadSenders();
    });
  });
  $("#sort").value = S.sort;
  $("#sort").addEventListener("change", e => { S.sort = e.target.value; saveView(); loadSenders(); });
  $("#folder").addEventListener("change", e => { S.folder = e.target.value; loadSenders(); });
  for (const [id, k] of [["onlyUnsub", "onlyUnsub"], ["showKept", "showKept"], ["hideUnsubscribed", "hideUnsub"]]) {
    $("#" + id).checked = S[k];
    $("#" + id).addEventListener("change", e => { S[k] = e.target.checked; saveView(); loadSenders(); });
  }
  $("#selAll").addEventListener("change", e => {
    S.rows.forEach(r => e.target.checked ? S.sel.add(r.key) : S.sel.delete(r.key));
    renderSenders();
  });
  $("#bulkClear").addEventListener("click", () => { S.sel.clear(); renderSenders(); });
  $$("[data-bulk]").forEach(b => b.addEventListener("click", () => {
    const a = b.dataset.bulk;
    if (a.startsWith("rule-")) doBulkRule(a.slice(5));
    else doAction([...S.sel], a);
  }));
  $("#moreBtn").addEventListener("click", () => loadSenders(false));
  $("#dryrun").addEventListener("click", () => showTab("settings"));
  $("#dryToggle").addEventListener("click", async () => {
    const on = $("#dryCard").classList.contains("on");
    if (on) {
      const v = await dialog("Testmodus ausschalten?", [
        "Ab jetzt führt mailsweep Aktionen wirklich aus: Mails werden verschoben, Abmeldungen verschickt.",
        h("p", { class: "muted" }, "Testeinträge („Test: …“) werden entfernt. Angelegte Regeln bleiben und greifen beim nächsten Sync. Prüf sie vorher im Tab „Regeln“."),
      ], [["Abbrechen", "", "ghost"], ["Regeln ansehen", "rules", "ghost"], ["Testmodus aus", "ok", "danger"]]);
      if (v === "rules") return showTab("rules");
      if (v !== "ok") return;
      await saveSettings({ dry_run: false }, "Testmodus ist aus");
    } else {
      await saveSettings({ dry_run: true }, "Testmodus ist an");
    }
    loadSenders(true, true);
  });
  $("#settingsForm").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, values = {};
    for (const [key, type, hasFolders] of JSON.parse(f.dataset.keys || "[]")) {
      if (type === "bool") values[key] = f.querySelector(`[name="${key}"]`).checked;
      else if (type === "list" && hasFolders) values[key] = [...f.querySelectorAll(`[name="${key}"]:checked`)].map(i => i.value);
      else values[key] = f.querySelector(`[name="${key}"]`).value;
    }
    await saveSettings(values);
  });
  let dTimer;
  $("#doneQ").addEventListener("input", () => { clearTimeout(dTimer); dTimer = setTimeout(loadDone, 250); });
  $("#syncBtn").addEventListener("click", async () => {
    try { await api("/api/sync", { method: "POST" }); toast("Sync gestartet"); setTimeout(loadStatus, 500); }
    catch (e) { toast(e.message, true); }
  });
  $("#dClose").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });

  $("#ruleAction").addEventListener("change", e => $("#ruleTarget").classList.toggle("hidden", e.target.value !== "move"));
  $("#ruleForm").addEventListener("submit", async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await createRule(f.get("match_type"), f.get("value"), f.get("action"), f.get("apply_now") === "on", f.get("action") === "move" ? f.get("target_folder") : "");
      toast(`Regel angelegt${r.moved ? `, ${nf.format(r.moved)} Mails verschoben` : ""}`);
      e.target.reset(); $("#ruleTarget").classList.add("hidden");
      loadRules(); loadStatus();
    } catch (err) { toast(err.message, true); }
  });
}

(async function main() {
  initControls();
  await loadStatus();
  loadFolders();
  loadSenders();
})();
