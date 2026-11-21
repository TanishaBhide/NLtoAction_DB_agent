"use strict";

/* ------------------------------------------------------------- helpers */
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };

const SQL_KEYWORDS = /\b(SELECT|FROM|WHERE|AND|OR|NOT|IN|IS|NULL|JOIN|ON|LEFT|GROUP BY|ORDER BY|DESC|ASC|LIMIT|OFFSET|INSERT INTO|INSERT|INTO|VALUES|UPDATE|SET|DELETE|RETURNING|COUNT|SUM|AVG|MIN|MAX|AS|ILIKE|LIKE|BETWEEN|DROP TABLE|DROP|TRUNCATE TABLE|TRUNCATE|ALTER TABLE|ALTER|CREATE|GRANT|REVOKE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|ISOLATION LEVEL|READ COMMITTED|READ ONLY|READ WRITE|SET LOCAL|CONSTRAINTS ALL IMMEDIATE|DISTINCT|EXISTS|CASE|WHEN|THEN|ELSE|END)\b/gi;
function highlight(sql) {
  // tokenise strings first so keywords inside literals are not coloured
  return String(sql ?? "").split(/('(?:[^']|'')*')/g).map((part, i) => {
    if (i % 2 === 1) return `<span class="str">${esc(part)}</span>`;
    return esc(part)
      .replace(SQL_KEYWORDS, (m) => `<span class="kw">${m}</span>`)
      .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="num">$1</span>');
  }).join("");
}
const fmt = (v) => v === null || v === undefined ? "NULL" : (typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v));

function table(columns, rows) {
  const head = columns.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + r.map((v) =>
    `<td class="${v === null ? "null" : typeof v === "number" ? "n" : ""}">${esc(fmt(v))}</td>`).join("") + "</tr>").join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body ||
    `<tr><td class="null" colspan="${columns.length || 1}">no rows</td></tr>`}</tbody></table></div>`;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw Object.assign(new Error(data.error || `HTTP ${res.status}`), { status: res.status });
  return data;
}

const newSessionId = () => "web-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

/* --------------------------------------------------------------- state */
const state = { session: newSessionId(), busy: false, runs: [], selected: null, adminPassword: null, view: "user", page: 0, country: null };

const EXAMPLES = [
  ["show customers from France", "read"],
  ["count invoices per country", "aggregate"],
  ["find products with heart in the description", "search"],
  ["set the quantity of stock code 85123A on invoice 536365 to 7", "write"],
  ["delete invoice 536414", "cascade"],
  ["remove the product with stock code 84406B", "FK blocked"],
  ["set the quantity of stock code 22752 on invoice 536365 to -2", "rule"],
  ["show all customer ids with their country", "PII"],
  ["drop the invoices table", "DDL"],
  ["delete all customers", "unscoped"],
  ["show the line items of invoice 537967", "then: delete them"],
];

/* ---------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((b) => { b.classList.toggle("active", b === btn); b.setAttribute("aria-selected", b === btn); });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
  if (btn.dataset.tab === "views") loadView();
}));

/* -------------------------------------------------------------- status */
async function refreshStatus() {
  const box = $("#status"), text = $("#status-text");
  try {
    const s = await api("/api/status");
    if (s.database) {
      box.className = "status ok";
      const t = s.tables;
      text.textContent = `PostgreSQL · ${t.customers} customers · ${t.products} products · ${t.invoices} invoices · ${t.invoice_items} line items · planner: ${s.planner}${s.planner_note ? " (" + s.planner_note + ")" : ""}`;
      box.title = text.textContent;
    } else {
      box.className = "status bad";
      text.textContent = "database not reachable — " + (s.error || "");
    }
  } catch (e) {
    box.className = "status bad"; text.textContent = "server not reachable";
  }
}

/* ---------------------------------------------------------------- chat */
const thread = $("#thread"), input = $("#input");

function renderExamples() {
  $("#examples").innerHTML = EXAMPLES.map(([q, tag]) => `<button class="chip" data-q="${esc(q)}">${esc(q)}<small>${esc(tag)}</small></button>`).join("");
  $("#examples").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip"); if (chip) send(chip.dataset.q);
  });
}

function setSession(id) {
  state.session = id; state.runs = []; state.selected = null;
  $("#session-id").textContent = id;
  thread.querySelectorAll(".msg").forEach((m) => m.remove());
  $("#empty").style.display = "";
  $("#log").innerHTML = '<div class="log-empty">The agent\'s reasoning appears here when you send a request. Click any answer in the chat to show its log.</div>';
  $("#log-sub").textContent = "thinking · actions · observations · SQL · transaction statements";
}

function scrollDown() { thread.scrollTop = thread.scrollHeight; }

function addUser(text) {
  $("#empty").style.display = "none";
  thread.appendChild(el(`<div class="msg user"><div class="bubble">${esc(text)}</div></div>`));
  scrollDown();
}

function addThinking() {
  const node = el(`<div class="msg bot"><div class="thinking"><span class="dots"><span></span><span></span><span></span></span>
    parsing intent → planning SQL → guardrails → blast radius → drift → sandbox …</div></div>`);
  thread.appendChild(node); scrollDown(); return node;
}

const STATE_LABEL = {
  executed: "executed", committed: "committed", rolled_back: "rolled back", failed: "failed",
  blocked: "blocked", approval: "awaiting approval", planned: "planned",
};
const ACCESS_ICON = { granted: "🔓", read: "👁", approval: "⏳", dryrun: "🧪", denied: "⛔" };

function sqlEntry(entry) {
  const meta = [`<span class="badge b-${esc(entry.state)}">${esc(STATE_LABEL[entry.state] || entry.state)}</span>`,
    `attempt ${entry.attempt}`];
  if (entry.verdict) meta.push(`guardrails <b>${esc(entry.verdict)}</b>${entry.risk !== undefined ? ` · risk ${esc(entry.risk)}` : ""}`);
  if (entry.effective_verdict) meta.push(`effective <b>${esc(entry.effective_verdict)}</b>`);
  if (entry.sqlstate) meta.push(`SQLSTATE <b>${esc(entry.sqlstate)}</b>`);
  if (entry.transaction_id) meta.push(`txn ${esc(entry.transaction_id)}`);
  if (entry.source === "reconstructed") meta.push(`<span class="note-rec">reconstructed from your intent — never sent to the database</span>`);
  const sql = entry.sql
    ? `<pre class="sql ${entry.state === "blocked" ? "strike" : ""}">${highlight(entry.sql)}</pre>`
    : `<pre class="sql strike">-- no SQL could be generated for this request</pre>`;
  const counted = entry.count_sql ? `<pre class="sql" style="opacity:.8">${highlight(entry.count_sql)}  <span style="color:#8fa6d6">-- row-count estimate</span></pre>` : "";
  return `<div class="sql-entry"><div class="sql-meta">${meta.join('<span class="sep">|</span>')}</div>${sql}${counted}
    <div class="sql-outcome"><b>${esc(entry.outcome || "")}</b>${entry.reason ? " — " + esc(entry.reason) : ""}</div></div>`;
}

function renderRun(run) {
  const a = run.access;
  const why = run.why_not.length ? `<div class="section"><div class="section-label">Why not</div><ul class="whynot">${
    run.why_not.map((w) => `<li class="${["result", "approval", "drift", "blast_radius"].includes(w.stage) ? "soft" : ""}">
      <div class="stage">${esc(w.stage)} · ${esc(w.code)}</div><div class="wn-head">${esc(w.headline)}</div>
      <div>${esc(w.cause)}</div>${w.fix ? `<div class="fix">→ ${esc(w.fix)}</div>` : ""}</li>`).join("")}</ul></div>` : "";
  const result = run.result ? `<div class="section"><div class="section-label">Result · ${esc(run.result.kind)}</div>
    ${table(run.result.columns, run.result.rows)}
    <div class="caption">${run.result.row_count} row(s)${run.result.shown < run.result.row_count ? `, first ${run.result.shown} shown` : ""}</div></div>` : "";
  const notes = run.notes.length ? `<div class="section"><div class="section-label">Notes</div><ul class="notes">${
    run.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : "";
  const approve = run.pending_id ? `<button class="btn warn approve" data-pending="${esc(run.pending_id)}">Approve &amp; run this exact SQL</button>` : "";
  const node = el(`<div class="msg bot"><div class="card">
    <div class="card-head"><span class="badge b-${esc(run.status)}">${esc(run.status.replace("_", " "))}</span>
      <div><div class="headline">${esc(run.headline)}</div>${run.summary ? `<p class="summary">${esc(run.summary)}</p>` : ""}</div></div>
    <div class="section"><div class="section-label">Database modification access</div>
      <div class="access a-${esc(a.state)}"><div class="access-title">${ACCESS_ICON[a.state] || ""} ${esc(a.title)}</div>
      <ul>${a.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div></div>
    <div class="section"><div class="section-label">SQL ${run.sql_log.length > 1 ? `(${run.sql_log.length} attempts)` : ""}</div>
      ${run.sql_log.map(sqlEntry).join("") || '<div class="caption">no SQL</div>'}</div>
    ${result}${why}${notes}
    <div class="card-foot">${approve}<button class="btn ghost show-log">Show execution log</button><span class="spacer"></span>
      <span>${esc(run.planner)}</span><span>· ${run.attempts} attempt(s)</span><span>· ${Math.round(run.latency_ms || 0)} ms</span>
      ${run.request_id ? `<span>· audit #${run.request_id}</span>` : ""}</div>
  </div></div>`);
  const card = $(".card", node);
  const index = state.runs.push(run) - 1;
  card.addEventListener("click", (e) => {
    if (e.target.closest(".approve")) return;
    selectRun(index, card);
  });
  const btn = $(".approve", node);
  if (btn) btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    btn.disabled = true; btn.textContent = "Running approved plan…";
    await approve(btn.dataset.pending);
    btn.textContent = "Approved";
  });
  return { node, card, index };
}

function selectRun(index, card) {
  state.selected = index;
  document.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
  if (card) card.classList.add("selected");
  renderLog(state.runs[index]);
}

const KIND_LABEL = { thought: "Thinking", action: "Action", observation: "Observation", answer: "Answer" };

function detailHtml(d) {
  if (d.type === "sql") return `<div class="d-sql">${d.label ? `<span class="lbl">${esc(d.label)}</span>` : ""}${highlight(d.text)}</div>`;
  if (d.type === "tx") {
    const first = d.text.split("\n").pop();
    const cls = d.text === "COMMIT" ? "commit" : d.text.startsWith("ROLLBACK") ? "rollback" :
      String(d.outcome).startsWith("ERROR") ? "err" : d.text.startsWith("BEGIN") ? "begin" : "";
    const label = d.text.startsWith("-- ") ? d.text.split("\n")[0].slice(3) + ": " : "";
    return `<div class="d-tx ${cls}"><span>${esc(label)}${highlight(first)}</span><span class="o">${esc(d.outcome)} · ${Number(d.ms).toFixed(1)} ms</span></div>`;
  }
  if (d.type === "check") return `<div class="d-line d-check ${d.ok ? "ok" : "bad"}">${esc(d.text)}</div>`;
  return `<div class="d-line ${d.type === "warn" ? "d-warn" : d.type === "error" ? "d-error" : ""}">${esc(d.text)}</div>`;
}

function renderLog(run) {
  const log = $("#log");
  $("#log-sub").textContent = `“${run.request}”`;
  let html = `<div class="log-request">${esc(run.request)}<small>session ${esc(run.session_id)} · ${esc(run.planner)} · status ${esc(run.status)} (${esc(run.stop_reason)})</small></div>`;
  let attempt = 0, delay = 0;
  for (const s of run.steps) {
    if (s.attempt !== attempt && s.kind !== "answer") {
      attempt = s.attempt;
      html += `<div class="attempt-sep">── attempt ${attempt} ──</div>`;
    }
    const details = s.details.length ? `<div class="detail">${s.details.map(detailHtml).join("")}</div>` : "";
    html += `<div class="entry ${s.kind}" style="animation-delay:${delay}ms"><span class="k k-${s.kind}">${KIND_LABEL[s.kind] || s.kind}</span>
      <div class="entry-text">${esc(s.text)}</div>${details}</div>`;
    delay += 45;
  }
  log.innerHTML = html;
  log.scrollTop = 0;
}

function showError(message) {
  thread.appendChild(el(`<div class="msg bot"><div class="card"><div class="card-head"><span class="badge b-failed">error</span>
    <div class="headline">${esc(message)}</div></div></div></div>`));
  scrollDown();
}

async function send(text) {
  text = (text ?? input.value).trim();
  if (!text || state.busy) return;
  state.busy = true; $("#send").disabled = true;
  input.value = ""; autosize();
  addUser(text);
  const waiting = addThinking();
  try {
    const run = await api("/api/chat", { session_id: state.session, message: text, dry_run: $("#dry-run").checked });
    waiting.remove();
    const { node, card, index } = renderRun(run);
    thread.appendChild(node);
    selectRun(index, card);
    scrollDown();
    refreshStatus();
  } catch (e) {
    waiting.remove(); showError(e.message);
  } finally {
    state.busy = false; $("#send").disabled = false; input.focus();
  }
}

async function approve(pendingId) {
  if (state.busy) return;
  state.busy = true;
  addUser("✔ Approved — run the exact SQL shown above");
  const waiting = addThinking();
  try {
    const run = await api("/api/approve", { session_id: state.session, pending_id: pendingId, dry_run: $("#dry-run").checked });
    waiting.remove();
    const { node, card, index } = renderRun(run);
    thread.appendChild(node); selectRun(index, card); scrollDown(); refreshStatus();
  } catch (e) {
    waiting.remove(); showError(e.message);
  } finally { state.busy = false; }
}

function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("#new-session").addEventListener("click", () => setSession(newSessionId()));

/* --------------------------------------------------------------- views */
document.querySelectorAll(".seg").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b === btn));
  state.view = btn.dataset.view; state.page = 0; loadView();
}));

const money = (v) => v === null || v === undefined ? "–" : "£" + Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (v) => v === null || v === undefined ? "–" : Number(v).toLocaleString("en-GB");
const kb = (v) => v >= 1024 ? (v / 1024).toFixed(1) + " MB" : v + " kB";
const FLAG_CLASS = { "OK": "f-ok", "VIP": "f-vip", "VIP at risk": "f-risk", "At risk (inactive)": "f-risk",
  "Has cancellations": "f-warn", "Frequent refunds": "f-bad" };
const asRow = (v, r) => Object.fromEntries(v.columns.map((c, i) => [c, r[i]]));

function lockedCard(error = "") {
  return `<div class="locked"><div class="big">🔒</div><h3>Database admin view is locked</h3>
    <p>The DBA view shows storage per table, business-rule health, the agent's activity and its full audit trail
    (every request with the exact SQL and verdicts). Enter the admin password to unlock it.</p>
    <form class="pw" id="pw-form"><input type="password" id="pw" placeholder="Admin password" autocomplete="current-password">
    <button class="btn primary" type="submit">Unlock</button></form><div class="err" id="pw-err">${esc(error)}</div></div>`;
}

function viewHeader(v, extra = "") {
  return `<div class="view-title"><span class="audience a-${esc(v.key)}">${esc(v.audience)}</span><h3>${esc(v.title)}</h3>
      <span class="spacer"></span>${extra}</div>
    <p class="summary">${esc(v.description)}</p>
    <div class="meta"><span>SQL view <code>${esc(v.relation)}</code></span><span>database role <code>${esc(v.role)}</code></span>
      <span>${num(v.total)} record(s)${v.country ? " in " + esc(v.country) : ""}</span></div>`;
}

function pager(v) {
  const first = v.total ? v.page * v.page_size + 1 : 0, last = Math.min(v.total, (v.page + 1) * v.page_size);
  return `<div class="pager"><span>${v.key === "admin" ? "Audited requests " : "Showing "}${first}–${last} of ${num(v.total)}</span><span class="spacer"></span>
      <button class="btn" data-page="${v.page - 1}" ${v.page === 0 ? "disabled" : ""}>◀ Prev</button>
      <button class="btn" data-page="${v.page + 1}" ${last >= v.total ? "disabled" : ""}>Next ▶</button>
      <button class="btn ghost" data-page="${v.page}">Refresh</button></div>
    <details class="sqlused"><summary>SQL used for this page</summary><pre class="sql">${highlight(v.sql)}</pre></details>`;
}

function renderCatalogue(v) {
  const rows = v.rows.map((r) => asRow(v, r));
  const top = Math.max(1, ...rows.map((r) => r.units_sold || 0));
  const cards = rows.map((r) => `<div class="product">
      <div class="p-top"><span class="rank">#${r.popularity_rank}</span><span class="label l-${esc(String(r.label).replace(/\s+/g, "-").toLowerCase())}">${esc(r.label)}</span></div>
      <div class="p-name">${esc(r.product)}</div>
      <div class="p-code">stock code ${esc(r.stock_code)}</div>
      <div class="p-price">${money(r.price)}</div>
      <div class="bar"><span style="width:${Math.round(100 * (r.units_sold || 0) / top)}%"></span></div>
      <div class="p-stats">${num(r.units_sold)} sold · on ${num(r.times_ordered)} orders</div></div>`).join("");
  return viewHeader(v) + `<div class="products">${cards || '<div class="caption">no products</div>'}</div>` + pager(v);
}

function renderAccounts(v) {
  const options = ['<option value="">All countries</option>', ...(v.countries || []).map((c) =>
    `<option value="${esc(c.name)}" ${c.name === v.country ? "selected" : ""}>${esc(c.name)} (${c.count})</option>`)].join("");
  const filter = `<label class="filter">Country <select id="country">${options}</select></label>`;
  const body = v.rows.map((r) => asRow(v, r)).map((r) => `<tr>
      <td><code>${esc(r.customer)}</code></td><td>${esc(r.country)}</td><td class="n">${num(r.orders)}</td>
      <td class="n">${r.cancellations ? `<b class="neg">${num(r.cancellations)}</b>` : "0"}</td>
      <td class="n">${money(r.total_spend)}</td><td class="n">${r.refunded ? `<span class="neg">${money(r.refunded)}</span>` : "–"}</td>
      <td>${esc(r.last_order)} <span class="muted">(${r.days_since_last_order === 0 ? "latest day" : num(r.days_since_last_order) + " d before latest"})</span></td>
      <td><span class="flag ${FLAG_CLASS[r.support_flag] || "f-ok"}">${esc(r.support_flag)}</span></td></tr>`).join("");
  return viewHeader(v, filter) + `<div class="table-wrap"><table><thead><tr><th>customer</th><th>country</th><th>orders</th>
      <th>cancellations</th><th>total spend</th><th>refunded</th><th>last order</th><th>support flag</th></tr></thead>
      <tbody>${body || '<tr><td class="null" colspan="8">no customers</td></tr>'}</tbody></table></div>` + pager(v);
}

function bars(obj, cls = "") {
  const entries = Object.entries(obj || {});
  const max = Math.max(1, ...entries.map(([, n]) => n));
  return entries.length ? entries.map(([k, n]) => `<div class="hbar"><span class="hk">${esc(k.replace("_", " "))}</span>
      <span class="ht"><span class="hf ${cls} hb-${esc(k)}" style="width:${Math.round(100 * n / max)}%"></span></span><span class="hn">${num(n)}</span></div>`).join("")
    : '<div class="caption">no agent requests yet. Ask something in the Chat tab.</div>';
}

function renderAdmin(v) {
  const d = v.dashboard;
  const t = Object.fromEntries(d.tables.map((x) => [x.table, x]));
  const kpis = [["Database size", kb(d.database_size_kb), d.server_version],
    ["Line items", num(t.invoice_items && t.invoice_items.rows), `${num(t.invoices && t.invoices.rows)} invoices`],
    ["Customers / products", `${num(t.customers && t.customers.rows)} / ${num(t.products && t.products.rows)}`, "operational tables"],
    ["Agent requests", num(d.requests), `${num(d.agent_sessions)} session(s)`],
    ["Week 9 escalations", num(d.escalated_requests), "ALLOW → CONFIRM"]]
    .map(([k, val, sub]) => `<div class="kpi"><div class="kk">${esc(k)}</div><div class="kv">${esc(val)}</div><div class="ks">${esc(sub)}</div></div>`).join("");
  const health = d.invariants.map((i) => `<li class="${i.violations ? "bad" : "ok"}">${i.violations ? "✗" : "✓"} ${esc(i.description)}
      <span class="muted">${i.violations} violation(s)</span></li>`).join("");
  const shown = d.tables.filter((x) => x.kind === "operational" || x.rows > 0);
  const empty = d.tables.filter((x) => !shown.includes(x));
  const storage = shown.map((x) => `<tr><td><span class="dot-kind k-${esc(x.kind)}" title="${esc(x.kind)} table"></span><code>${esc(x.table)}</code></td>
      <td class="n">${num(x.rows)}</td><td class="n">${kb(x.size_kb)}</td></tr>`).join("")
    + (empty.length ? `<tr><td colspan="3" class="muted">+ ${empty.length} empty agent table(s)</td></tr>` : "");
  const audit = v.rows.map((r) => asRow(v, r)).map((r) => `<tr><td class="n">#${r.request_id}</td><td>${esc(String(r.created_at).slice(5, 16))}</td>
      <td class="wrap">${esc(r.request_text)}</td><td class="wrap"><code class="mini">${r.final_sql ? highlight(r.final_sql) : '<span class="muted">none</span>'}</code></td>
      <td><span class="badge b-neutral">${esc(r.guardrail_verdict || "–")}</span>${r.effective_verdict && r.effective_verdict !== r.guardrail_verdict ? ` → <span class="badge b-approval">${esc(r.effective_verdict)}</span>` : ""}</td>
      <td><span class="badge b-${esc(r.final_status)}">${esc(String(r.final_status).replace("_", " "))}</span></td><td><code>${esc(r.root_cause_code || "")}</code></td></tr>`).join("");
  return viewHeader(v, '<button class="btn ghost" id="lock-btn">Lock</button>') + `
    <div class="kpis">${kpis}</div>
    <div class="admin-grid">
      <div class="panel"><div class="section-label">Business-rule health (live invariant checks)</div><ul class="health">${health}</ul></div>
      <div class="panel"><div class="section-label">Agent requests by outcome</div>${bars(d.requests_by_status)}
        <div class="section-label" style="margin-top:12px">by effective verdict</div>${bars(d.requests_by_verdict, "v")}</div>
      <div class="panel"><div class="section-label">Storage per table</div><div class="legend-kind"><span class="dot-kind k-operational"></span>operational <span class="dot-kind k-agent"></span>agent / audit</div>
        <div class="table-wrap small"><table><thead><tr><th>table</th><th>rows</th><th>size</th></tr></thead><tbody>${storage}</tbody></table></div></div>
    </div>
    <div class="section-label" style="margin-top:18px">Latest audited agent requests (v_dba_activity)</div>
    <div class="table-wrap"><table><thead><tr><th>id</th><th>time</th><th>request</th><th>SQL</th><th>verdict</th><th>status</th><th>root cause</th></tr></thead>
      <tbody>${audit || '<tr><td class="null" colspan="7">no agent requests yet. Ask something in the Chat tab.</td></tr>'}</tbody></table></div>` + pager(v);
}

let viewToken = 0;
async function loadView() {
  const token = ++viewToken;  // ignore responses that arrive after the user switched views
  const card = $("#view-card");
  $("#admin-lock").textContent = state.adminPassword ? "🔓" : "🔒";
  if (state.view === "admin" && !state.adminPassword) {
    card.innerHTML = lockedCard();
    $("#pw-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const pw = $("#pw").value;
      try {
        await api("/api/views/admin", { password: pw, page: 0 });
        state.adminPassword = pw; loadView();
      } catch (err) { $("#pw-err").textContent = err.status === 401 ? "Wrong password." : err.message; }
    });
    $("#pw").focus();
    return;
  }
  card.innerHTML = '<div class="thinking"><span class="dots"><span></span><span></span><span></span></span> loading…</div>';
  try {
    const country = state.view === "agent" && state.country ? `&country=${encodeURIComponent(state.country)}` : "";
    const v = state.view === "admin"
      ? await api("/api/views/admin", { password: state.adminPassword, page: state.page })
      : await api(`/api/views/${state.view}?page=${state.page}${country}`);
    if (token !== viewToken) return;
    card.innerHTML = v.layout === "cards" ? renderCatalogue(v) : v.layout === "admin" ? renderAdmin(v) : renderAccounts(v);
    card.querySelectorAll("[data-page]").forEach((b) => b.addEventListener("click", () => { state.page = Number(b.dataset.page); loadView(); }));
    const select = $("#country");
    if (select) select.addEventListener("change", () => { state.country = select.value || null; state.page = 0; loadView(); });
    const lock = $("#lock-btn");
    if (lock) lock.addEventListener("click", () => { state.adminPassword = null; state.page = 0; loadView(); });
  } catch (e) {
    if (token !== viewToken) return;
    if (state.view === "admin" && e.status === 401) { state.adminPassword = null; return loadView(); }
    card.innerHTML = `<div class="err">Could not load the view: ${esc(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- init */
setSession(state.session);
renderExamples();
refreshStatus();
input.focus();

// Deep links: ?ask=first request|second request  sends them in order; ?tab=views&view=agent opens a view.
(async () => {
  const params = new URLSearchParams(location.search);
  if (params.get("tab") === "views") {
    state.view = ["user", "agent", "admin"].includes(params.get("view")) ? params.get("view") : "user";
    document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
    document.querySelector('.tab[data-tab="views"]').click();
  }
  for (const q of (params.get("ask") || "").split("|").map((s) => s.trim()).filter(Boolean)) await send(q);
})();
