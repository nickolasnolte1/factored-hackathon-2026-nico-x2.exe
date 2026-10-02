"use strict";

const state = { conv: null, language: "es", signedIn: false, personas: [], prefill: null, busy: false, phoneTimer: null, ticketSel: null };
const $ = (sel) => document.querySelector(sel);

const TOOL_LABEL = {
  get_customer_overview: "Resumen del cliente", list_products: "Productos", get_balance: "Saldo",
  list_recent_transactions: "Movimientos recientes", find_candidate_transactions: "Buscar el movimiento",
  explain_decline: "Explicar rechazo", check_dispute_eligibility: "¿Se puede reclamar?",
  prepare_dispute_case: "Preparar reclamo", create_dispute_case: "Crear reclamo", get_case_status: "Estado del caso",
  get_policy_info: "Política del banco", handoff_to_human: "Pasar a un humano",
};
const T = {
  es: {
    pick: "¿Cuál no reconoces?", pickSrc: "movimientos del cliente", choose: "Es este",
    confirmTitle: "Revisa los datos antes de abrir el reclamo", confirmYes: "Sí, confirmo", confirmNo: "No es correcto",
    confirmMsg: "Sí, confirmo que esos datos son correctos y quiero abrir el reclamo.", denyMsg: "No, esos datos no son correctos.",
    caseTitle: "Reclamo creado", ticketTitle: "Te pasamos con un especialista", authTitle: "Verifica tu identidad",
    authExpired: "Tu sesión venció. Verifica tu identidad de nuevo.", authIntro: "Formulario seguro: el asistente no ve tu documento ni el código.",
    docType: "Documento", docNumber: "Número", sendCode: "Enviar código", code: "Código de 6 dígitos", verify: "Verificar",
    codeSent: "Te enviamos un código por SMS.", wrongCode: "El código no es correcto. Intentos restantes: ",
    locked: "Demasiados intentos. Pide un código nuevo.", movement: "Movimiento", date: "Fecha", amount: "Monto",
    product: "Producto", channel: "Canal", merchant: "Comercio", type: "Tipo", priority: "Prioridad",
    firstResponse: "Primera respuesta", hours: "h", status: "Estado", queue: "Cola", verified: "verificado por el servicio",
    intl: "internacional", pickMsg: (m) => `Es este: ${m}`,
  },
  pt: {
    pick: "Qual você não reconhece?", pickSrc: "movimentos do cliente", choose: "É este",
    confirmTitle: "Confira os dados antes de abrir a contestação", confirmYes: "Sim, confirmo", confirmNo: "Não está correto",
    confirmMsg: "Sim, confirmo que os dados estão corretos e quero abrir a contestação.", denyMsg: "Não, esses dados não estão corretos.",
    caseTitle: "Contestação criada", ticketTitle: "Encaminhamos para um especialista", authTitle: "Confirme sua identidade",
    authExpired: "Sua sessão expirou. Confirme sua identidade de novo.", authIntro: "Formulário seguro: o assistente não vê seu documento nem o código.",
    docType: "Documento", docNumber: "Número", sendCode: "Enviar código", code: "Código de 6 dígitos", verify: "Verificar",
    codeSent: "Enviamos um código por SMS.", wrongCode: "Código incorreto. Tentativas restantes: ",
    locked: "Muitas tentativas. Peça um novo código.", movement: "Movimento", date: "Data", amount: "Valor",
    product: "Produto", channel: "Canal", merchant: "Estabelecimento", type: "Tipo", priority: "Prioridade",
    firstResponse: "Primeira resposta", hours: "h", status: "Status", queue: "Fila", verified: "verificado pelo serviço",
    intl: "internacional", pickMsg: (m) => `É este: ${m}`,
  },
};
const PRODUCT = { "Checking Account": ["Cuenta corriente", "Conta corrente"], "Savings Account": ["Cuenta de ahorros", "Poupança"],
  "Credit Card": ["Tarjeta de crédito", "Cartão de crédito"], "Debit Card": ["Tarjeta de débito", "Cartão de débito"],
  "Personal Loan": ["Préstamo personal", "Empréstimo pessoal"], Mortgage: ["Hipoteca", "Financiamento"], Investment: ["Inversión", "Investimento"], Insurance: ["Seguro", "Seguro"] };
const TXN = { Purchase: ["Compra", "Compra"], Withdrawal: ["Retiro", "Saque"], Transfer: ["Transferencia", "Transferência"],
  Payment: ["Pago", "Pagamento"], Deposit: ["Depósito", "Depósito"], Adjustment: ["Cargo del banco", "Tarifa do banco"] };
const REASON = { customer_status_restricted: "Cuenta restringida", suspected_card_compromise: "Posible tarjeta comprometida",
  card_block_request: "Bloqueo de tarjeta", explicit_human_request: "Pidió hablar con una persona", tool_failure: "Falla técnica",
  low_intent_confidence: "No se entendió la solicitud", no_match_after_clarification: "No se encontró el movimiento",
  outside_dispute_window: "Fuera del plazo de 90 días", amount_above_threshold: "Monto sobre el umbral", complaint_routing: "Queja" };
const tr = (map, key) => (map[key] ? map[key][state.language === "pt" ? 1 : 0] : key);
const t = (k) => (T[state.language] || T.es)[k];

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"); }
function money(amount, currency) {
  const loc = { COP: "es-CO", ARS: "es-AR", MXN: "es-MX", BRL: "pt-BR", USD: "es-MX" }[currency] || "es";
  try { return new Intl.NumberFormat(loc, { style: "currency", currency, currencyDisplay: "code" }).format(amount); }
  catch { return `${currency} ${amount}`; }
}
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts.length <= 10 ? ts + "T00:00" : ts);
  const opts = { day: "numeric", month: "short", year: "numeric" };
  if (ts.length > 10) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleString(state.language === "pt" ? "pt-BR" : "es", opts);
}
function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }
function addNode(node) { $("#messages").appendChild(node); scrollDown(); return node; }

// ---- conversation -------------------------------------------------------------------------------------------------
async function newConversation() {
  const out = await api("/api/conversations", { language: state.language, reset_clock: true });
  $("#clock-now").textContent = when(out.now);
  state.conv = out.conversation_id; state.signedIn = false;
  $("#messages").innerHTML = ""; $("#trace").innerHTML = '<div class="trace-empty">Todavía no hay turnos.</div>';
  $("#starters").hidden = false; setSession(null); renderPhone(null);
  addNode(el(`<div class="msg bot">Hola, soy el asistente de LATAM Bank. Puedo ayudarte con cobros que no reconoces, cobros o comisiones incorrectos, tu saldo y tus movimientos. ¿Qué necesitas?</div>`));
}

function setSession(session) {
  const s = $("#session-state");
  state.signedIn = !!session;
  s.className = "sub" + (session ? " on" : "");
  s.innerHTML = session ? `<span class="lock"></span>Identidad verificada · <span class="mono">${esc(session.session_ref)}</span> · vence ${esc(when(session.expires_at))}` : `<span class="lock"></span>Sin identificar`;
}

async function send(text) {
  if (!text.trim() || state.busy) return;
  $("#starters").hidden = true;
  addNode(el(`<div class="msg customer">${esc(text)}</div>`));
  await runTurn(() => api(`/api/conversations/${state.conv}/messages`, { text }));
}

async function runTurn(call) {
  state.busy = true; $("#send").disabled = true;
  const typing = addNode(el(`<div class="typing" aria-label="El asistente está escribiendo"><i></i><i></i><i></i></div>`));
  try {
    const out = await call();
    typing.remove();
    if (out.skip) return;
    if (out.turn && out.turn.language) state.language = out.turn.language;
    renderReply(out);
    if (out.signed_in === false && state.signedIn) setSession(null);
  } catch (e) {
    typing.remove();
    addNode(el(`<div class="msg system">No se pudo contactar al servidor (${esc(e.message.slice(0, 80))}).</div>`));
  } finally {
    state.busy = false; $("#send").disabled = false; $("#input").focus();
    refreshConsoleCount();
  }
}

function renderReply(out) {
  const turn = out.turn;
  const bubble = el(`<div class="msg bot">${md(out.reply)}</div>`);
  if (turn && turn.tools.length) {
    const r = document.createElement("div"); r.className = "receipts";
    turn.tools.forEach((tool, i) => {
      const chip = el(`<button class="rcpt${tool.ok ? "" : " err"}" title="${esc(TOOL_LABEL[tool.tool] || tool.tool)}">${tool.ok ? "✓" : "✕"} ${esc(tool.tool)}${tool.tool_call_id ? " · " + esc(tool.tool_call_id.slice(0, 11)) : ""}${tool.error ? " · " + esc(tool.error) : ""}</button>`);
      chip.addEventListener("click", () => flashTool(turn.turn_index, i));
      r.appendChild(chip);
    });
    bubble.appendChild(r);
  }
  addNode(bubble);
  (out.blocks || []).forEach(renderBlock);
  if (turn) renderTurn(turn);
}

// ---- cards ----------------------------------------------------------------------------------------------------------
function renderBlock(b) {
  if (b.type === "auth_required") { setSession(null); return renderAuth(b.expired); }
  if (b.type === "candidates") return renderCandidates(b);
  if (b.type === "confirm") return renderConfirm(b);
  if (b.type === "case") return renderCase(b);
  if (b.type === "ticket") return renderTicket(b);
}

function mvLabel(m) {
  const what = m.merchant && m.merchant.untrusted_text ? m.merchant.untrusted_text : tr(TXN, m.transaction_type);
  return { what, line: `${tr(TXN, m.transaction_type)} · ${tr(PRODUCT, m.product_type_en)} · ${m.channel || ""}${m.is_international ? " · " + t("intl") : ""}` };
}

function renderCandidates(b) {
  const card = el(`<div class="card"><div class="card-head">${esc(t("pick"))}<span class="src">${esc(t("pickSrc"))}</span></div><div class="card-body"><div class="lineup"></div></div></div>`);
  const list = card.querySelector(".lineup");
  b.items.forEach((m) => {
    const { what, line } = mvLabel(m);
    const btn = el(`<button class="mv"><span class="what">${esc(what)}</span><span class="amt">${esc(money(m.amount, m.currency))}</span><span class="meta">${esc(when(m.event_ts || m.event_date))} · ${esc(line)}</span></button>`);
    btn.addEventListener("click", () => {
      list.querySelectorAll("button").forEach((x) => (x.disabled = true));
      send(t("pickMsg")(`${what}, ${money(m.amount, m.currency)}, ${when(m.event_ts || m.event_date)} (${m.transaction_id})`));
    });
    list.appendChild(btn);
  });
  addNode(card);
}

function renderConfirm(b) {
  const f = b.facts || {}, p = b.preview || {};
  const card = el(`<div class="card"><div class="card-head">${esc(t("confirmTitle"))}<span class="src">${esc(b.evidence || "")}</span></div>
    <div class="card-body">
      <dl class="facts">
        <dt>${esc(t("movement"))}</dt><dd>${esc(tr(TXN, f.transaction_type))}${f.merchant && f.merchant.untrusted_text ? " · " + esc(f.merchant.untrusted_text) : ""}</dd>
        <dt>${esc(t("date"))}</dt><dd>${esc(when(f.event_ts || f.event_date))}</dd>
        <dt>${esc(t("amount"))}</dt><dd class="mono">${esc(money(f.amount, f.currency))}</dd>
        <dt>${esc(t("product"))}</dt><dd>${esc(tr(PRODUCT, f.product_type_en))}${f.number_last4 ? " ••" + esc(f.number_last4) : ""}</dd>
        <dt>${esc(t("channel"))}</dt><dd>${esc(f.channel || "—")}${f.is_international ? " · " + esc(t("intl")) : ""}</dd>
        <dt>${esc(t("type"))}</dt><dd>${esc(p.subcategory || p.dispute_type || "—")}</dd>
        <dt>${esc(t("priority"))}</dt><dd>${esc(p.priority || "—")} · ${esc(t("firstResponse"))} ${esc(p.first_response_hours ?? "—")} ${esc(t("hours"))}</dd>
      </dl>
      <div class="row"><button class="btn primary" data-a="yes">${esc(t("confirmYes"))}</button><button class="btn" data-a="no">${esc(t("confirmNo"))}</button></div>
    </div></div>`);
  card.querySelectorAll("button").forEach((btn) => btn.addEventListener("click", () => {
    card.querySelectorAll("button").forEach((x) => (x.disabled = true));
    send(btn.dataset.a === "yes" ? t("confirmMsg") : t("denyMsg"));
  }));
  addNode(card);
}

function renderCase(b) {
  const c = b.case || {};
  addNode(el(`<div class="card ok"><div class="card-head">✓ ${esc(t("caseTitle"))}<span class="src">${esc(b.evidence || "")}</span></div>
    <div class="card-body"><dl class="facts">
      <dt>Caso</dt><dd class="mono">${esc(b.case_id)}</dd>
      <dt>${esc(t("status"))}</dt><dd>${esc(b.status)} · <span class="note">${esc(t("verified"))}</span></dd>
      <dt>${esc(t("amount"))}</dt><dd class="mono">${esc(money(c.amount, c.currency))}</dd>
      <dt>${esc(t("type"))}</dt><dd>${esc(c.subcategory || c.dispute_type || "—")}</dd>
      <dt>${esc(t("priority"))}</dt><dd>${esc(b.priority)} · ${esc(t("firstResponse"))} ${esc(b.first_response_hours)} ${esc(t("hours"))}</dd>
    </dl></div></div>`));
}

function renderTicket(b) {
  addNode(el(`<div class="card warn"><div class="card-head">${esc(t("ticketTitle"))}<span class="src">${esc(b.evidence || "")}</span></div>
    <div class="card-body"><dl class="facts">
      <dt>Ticket</dt><dd class="mono">${esc(b.ticket_id)}</dd>
      <dt>${esc(t("queue"))}</dt><dd>${esc(b.queue || "—")}</dd>
      ${b.first_response_hours != null ? `<dt>${esc(t("firstResponse"))}</dt><dd>${esc(b.first_response_hours)} ${esc(t("hours"))}</dd>` : ""}
    </dl><div class="note">${esc(t("verified"))}</div></div></div>`));
}

function renderAuth(expired) {
  document.querySelectorAll(".card.secure").forEach((c) => c.remove());
  const card = el(`<div class="card secure"><div class="card-head">🔒 ${esc(t("authTitle"))}<span class="src">start_authentication · verify_otp</span></div>
    <div class="card-body">
      <div class="note">${esc(expired ? t("authExpired") : t("authIntro"))}</div>
      <form class="secure-form" data-step="doc">
        <div class="inline">
          <label class="field">${esc(t("docType"))}<select name="type"><option>DNI</option><option>CC</option><option>CE</option><option>Pasaporte</option></select></label>
          <label class="field">${esc(t("docNumber"))}<input name="number" inputmode="numeric" autocomplete="off" required></label>
        </div>
        <button class="btn primary" type="submit">${esc(t("sendCode"))}</button>
        <div class="form-error" hidden></div>
      </form>
    </div></div>`);
  const form = card.querySelector("form");
  if (state.prefill) { form.type.value = state.prefill.document_type; form.number.value = state.prefill.document_number; }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = form.querySelector(".form-error"); err.hidden = true;
    if (form.dataset.step === "doc") {
      const out = await api(`/api/conversations/${state.conv}/auth/start`, { document_type: form.type.value, document_number: form.number.value });
      if (!out.ok) { err.textContent = out.message || out.error; err.hidden = false; return; }
      form.dataset.step = "code";
      form.innerHTML = `<div class="note">${esc(t("codeSent"))}</div><label class="field">${esc(t("code"))}<input name="code" class="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required></label><button class="btn primary" type="submit">${esc(t("verify"))}</button><div class="form-error" hidden></div>`;
      form.code.focus(); pollPhone();
    } else {
      const btn = form.querySelector("button"); btn.disabled = true;
      await runTurn(async () => {
        const out = await api(`/api/conversations/${state.conv}/auth/verify`, { code: form.code.value });
        if (!out.ok) {
          btn.disabled = false;
          const e2 = form.querySelector(".form-error");
          e2.textContent = out.locked ? t("locked") : t("wrongCode") + (out.attempts_remaining ?? 0); e2.hidden = false;
          return { skip: true };
        }
        card.remove(); renderPhone(null); setSession(out.session);
        addNode(el(`<div class="msg system">🔒 Identidad verificada con documento + código. El asistente continúa.</div>`));
        return out;
      });
    }
  });
  addNode(card);
  form.querySelector("input").focus();
}

// ---- demo phone ---------------------------------------------------------------------------------------------------
function renderPhone(code) {
  $("#phone").innerHTML = code
    ? `<div class="sms"><div class="from">LATAM Bank · SMS</div>Tu código de verificación es<div class="code">${esc(code)}</div><div class="note">Vence en 5 minutos. Nunca lo compartas.</div></div>`
    : `<div class="phone-empty">Aquí llega el código por SMS cuando el cliente lo pide.</div>`;
}
function pollPhone() {
  clearInterval(state.phoneTimer);
  let n = 0;
  state.phoneTimer = setInterval(async () => {
    const out = await api(`/api/conversations/${state.conv}/phone`);
    if (out.code || ++n > 20) { clearInterval(state.phoneTimer); renderPhone(out.code); }
  }, 400);
}

// ---- trace ----------------------------------------------------------------------------------------------------------
function renderTurn(turn) {
  const box = $("#trace");
  box.querySelector(".trace-empty")?.remove();
  const kind = turn.kind === "app_event" ? "evento de la app" : "cliente";
  const calls = turn.model_calls.length;
  const node = el(`<div class="turn" data-turn="${turn.turn_index}-${box.children.length}">
    <div class="turn-head"><span>Turno ${turn.turn_index} · ${kind} · ${esc(turn.language)}</span><span class="mono">${(turn.latency_ms / 1000).toFixed(1)} s</span></div>
    <div class="turn-metrics"><span>${calls} llamada${calls === 1 ? "" : "s"} al modelo</span><span>${turn.tokens.prompt + turn.tokens.completion} tokens</span>${turn.cost_usd_est ? `<span>~US$ ${turn.cost_usd_est.toFixed(4)}</span>` : ""}</div>
  </div>`);
  node.appendChild(el(`<div class="tpol">Clasificador de intención: ${turn.classifier ? esc(JSON.stringify(turn.classifier)) : "aún no conectado"}</div>`));
  turn.tools.forEach((tool) => {
    const pol = tool.policy ? Object.entries(tool.policy).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k}=${v}`).join(" · ") : "";
    node.appendChild(el(`<div class="tool${tool.ok ? "" : " err"}"><span class="st"></span><span><span class="tname">${esc(tool.tool)}</span> <span class="tlabel">${esc(TOOL_LABEL[tool.tool] || "")}${tool.runtime_fallback ? " · fallback del runtime" : ""}</span></span><span class="tms">${tool.latency_ms} ms</span>
      <span class="tpol">${esc(tool.tool_call_id || "")}${tool.error ? " · " + esc(tool.error) : ""}${pol ? " · " + esc(pol) : ""}</span></div>`));
  });
  if (turn.fallback) node.appendChild(el(`<div class="warnline">Fallback seguro activado: ${esc(turn.fallback)}</div>`));
  if (turn.unverified_ids_in_reply && turn.unverified_ids_in_reply.length) node.appendChild(el(`<div class="warnline">Id sin respaldo en la respuesta: ${esc(turn.unverified_ids_in_reply.join(", "))}</div>`));
  box.prepend(node);
}
function flashTool(turnIndex, i) {
  const node = [...document.querySelectorAll(".turn")].find((n) => n.dataset.turn.startsWith(turnIndex + "-"));
  if (!node) return;
  const tool = node.querySelectorAll(".tool")[i];
  node.scrollIntoView({ behavior: "smooth", block: "nearest" });
  if (tool) { tool.classList.add("flash"); setTimeout(() => tool.classList.remove("flash"), 1600); }
}

// ---- personas & controls ----------------------------------------------------------------------------------------------
function renderPersonas() {
  const box = $("#personas");
  if (!state.personas.length) { box.innerHTML = `<div class="hint">Corre <span class="mono">python -m app.build_personas</span> para crear los clientes de prueba.</div>`; return; }
  state.personas.forEach((p) => {
    const status = p.customer_status === "Active" ? "" : `<span class="badge bad">${esc(p.customer_status)}</span>`;
    const node = el(`<div class="persona"><span class="cc">${esc(p.country_code)}</span>
      <span class="who">${esc(p.first_name)} <span class="badge">${esc(p.segment)}</span>${status}</span>
      <span class="story">${esc(p.story.es)}</span>
      <span class="doc"><span>${esc(p.document_type)} ${esc(p.document_number)}</span><button class="btn small">Usar</button></span></div>`);
    node.querySelector("button").addEventListener("click", () => {
      state.prefill = p;
      const form = document.querySelector(".card.secure form[data-step='doc']");
      if (form) { form.type.value = p.document_type; form.number.value = p.document_number; form.querySelector("button").focus(); }
      else renderAuth(false);
    });
    box.appendChild(node);
  });
}

async function advanceClock() {
  const out = await api("/api/demo/clock", { minutes: 16 });
  $("#clock-now").textContent = when(out.now);
  addNode(el(`<div class="msg system">⏩ Reloj del servicio adelantado 16 minutos (${esc(when(out.now))}). La sesión de 15 minutos venció.</div>`));
}

// ---- console --------------------------------------------------------------------------------------------------------
async function loadConsole() {
  const out = await api("/api/console");
  const tk = $("#tickets"), cs = $("#cases");
  tk.innerHTML = out.tickets.length ? "" : '<div class="empty">Todavía no hay transferencias.</div>';
  out.tickets.forEach((r) => {
    const node = el(`<button class="tk"><span class="reason">${esc(REASON[r.reason_code] || r.reason_code)}</span>${r.priority ? `<span class="prio ${esc(r.priority)}">${esc(r.priority)}</span>` : "<span></span>"}
      <span class="sub"><span class="mono">${esc(r.ticket_id)}</span><span>${esc(r.queue || "")}</span><span>${r.identity_verified ? "identidad verificada" : "sin identificar"}</span><span>${esc(r.language || "")}</span><span>${esc(when(r.created_at))}</span></span></button>`);
    node.addEventListener("click", () => { state.ticketSel = r.ticket_id; tk.querySelectorAll(".tk").forEach((x) => x.removeAttribute("aria-current")); node.setAttribute("aria-current", "true"); showTicket(r); });
    if (state.ticketSel === r.ticket_id) node.setAttribute("aria-current", "true");
    tk.appendChild(node);
  });
  cs.innerHTML = out.cases.length ? "" : '<div class="empty">Todavía no hay reclamos.</div>';
  out.cases.forEach((c) => cs.appendChild(el(`<div class="case-row"><span><span class="mono">${esc(c.case_id)}</span> · ${esc(c.subcategory || c.dispute_type)}</span><span class="prio ${esc(c.priority)}">${esc(c.priority)}</span>
    <span class="sub">${esc(money(c.amount, c.currency))} · ${esc(c.status)} · cliente ${esc(c.customer_ref || "—")} · primera respuesta ${esc(when(c.first_response_due_at))}</span></div>`)));
  const sel = out.tickets.find((r) => r.ticket_id === state.ticketSel);
  if (sel) showTicket(sel);
  setCount(out.tickets.length);
}
function list(items) { return items && items.length ? `<ul>${items.map((x) => `<li>${esc(typeof x === "string" ? x : JSON.stringify(x))}</li>`).join("")}</ul>` : '<p class="note">—</p>'; }
function showTicket(r) {
  const a = r.agent_reported || {}, pkg = a.package || a, sv = r.service_verified || {};
  $("#ticket-detail").innerHTML = `<h2>${esc(REASON[r.reason_code] || r.reason_code)}</h2>
    <dl class="kv" style="margin-top:12px">
      <dt>Ticket</dt><dd class="mono">${esc(r.ticket_id)}</dd>${r.priority ? `<dt>Prioridad</dt><dd>${esc(r.priority)}${r.first_response_hours != null ? ` · primera respuesta ${esc(r.first_response_hours)} h` : ""}</dd>` : ""}
      <dt>Cliente</dt><dd>${r.identity_verified ? `identidad verificada · <span class="mono">${esc(r.customer_ref)}</span>` : "sin identificar"}</dd>
      <dt>Idioma</dt><dd>${esc(r.language)}</dd><dt>Motivo comprobado</dt><dd>${esc(r.reason_check || "—")}</dd><dt>Creado</dt><dd>${esc(when(r.created_at))}</dd>
    </dl>
    <div class="dsec"><h3>Qué necesita el cliente</h3><p>${esc(r.request_summary || pkg.request_summary || "—")}</p></div>
    <div class="dsec"><h3>Hechos verificados (según el asistente)</h3>${list(pkg.verified_facts)}</div>
    <div class="dsec"><h3>Acciones tomadas</h3>${list(pkg.actions_taken)}</div>
    <div class="dsec"><h3>Preguntas abiertas</h3>${list(pkg.open_questions)}</div>
    <div class="dsec"><h3>Evidencia comprobada por el servicio</h3>${verifiedEvidence(sv)}
      <details><summary class="note">Registro completo</summary><pre class="mono" style="white-space:pre-wrap;font-size:12px;margin:6px 0 0">${esc(JSON.stringify(sv, null, 2))}</pre></details></div>`;
}
function verifiedEvidence(sv) {
  const ev = sv.evidence || [];
  const rows = ev.map((e) => {
    const pd = e.policy_decision || {};
    const bits = [e.outcome === "ok" ? "ok" : (e.error_code || e.outcome), pd.handoff_reason, pd.next_action].filter(Boolean).join(" · ");
    return `<li><span class="mono">${esc(e.tool)}</span> · ${esc(bits)} <span class="note mono">${esc(e.tool_call_id || "")}</span></li>`;
  });
  const extra = [];
  if (sv.draft) extra.push(`<li>Borrador de reclamo adjunto${sv.draft.transaction_id ? ` · <span class="mono">${esc(sv.draft.transaction_id)}</span>` : ""}</li>`);
  if ((sv.candidates || []).length) extra.push(`<li>${sv.candidates.length} movimiento(s) en discusión</li>`);
  if ((sv.dropped_evidence || []).length) extra.push(`<li class="warnline">${sv.dropped_evidence.length} evidencia(s) descartada(s) por no poder comprobarse</li>`);
  return rows.length || extra.length ? `<ul>${rows.join("")}${extra.join("")}</ul>` : '<p class="note">—</p>';
}
function setCount(n) { const c = $("#console-count"); c.hidden = !n; c.textContent = n; }
async function refreshConsoleCount() { try { const out = await api("/api/console"); setCount(out.tickets.length); } catch { /* ignore */ } }

function showView(name) {
  const isConsole = name === "console";
  $("#view-customer").hidden = isConsole; $("#view-console").hidden = !isConsole;
  $("#tab-customer").setAttribute("aria-selected", String(!isConsole)); $("#tab-console").setAttribute("aria-selected", String(isConsole));
  if (isConsole) loadConsole();
}

// ---- boot -------------------------------------------------------------------------------------------------------------
async function boot() {
  const cfg = await api("/api/config");
  state.personas = cfg.personas || [];
  $("#clock-now").textContent = when(cfg.now);
  $("#model-name").textContent = cfg.model.replace("databricks-", "");
  renderPersonas();
  await newConversation();
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#input").value; $("#input").value = ""; send(v); });
  $("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#starters").querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => send(c.textContent)));
  $("#btn-new").addEventListener("click", newConversation);
  $("#btn-clock").addEventListener("click", advanceClock);
  $("#btn-refresh").addEventListener("click", loadConsole);
  $("#tab-customer").addEventListener("click", () => showView("customer"));
  $("#tab-console").addEventListener("click", () => showView("console"));
  refreshConsoleCount();
}
boot();
