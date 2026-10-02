"use strict";

const state = {
  conv: null, label: "R-201", language: "es", session: null, now: null, personas: [], prefill: null,
  busy: false, smsTimer: null, ticketSel: null, seen: new Set(), stages: {}, called: false, turnSeq: 0,
};
const $ = (sel) => document.querySelector(sel);
const SESSION_MIN = 15;

// ---- vocabulary ------------------------------------------------------------------------------------------------------
const TOOL = {
  get_customer_overview: "Resumen del cliente", list_products: "Productos", get_balance: "Saldo",
  list_recent_transactions: "Movimientos recientes", find_candidate_transactions: "Buscar el movimiento",
  explain_decline: "Explicar un rechazo", check_dispute_eligibility: "Revisar si se puede reclamar",
  prepare_dispute_case: "Preparar el reclamo", create_dispute_case: "Crear el reclamo", get_case_status: "Estado del caso",
  get_policy_info: "Consultar la política", handoff_to_human: "Pasar a un especialista",
};
const ERR = {
  AUTH_REQUIRED: "Falta verificar la identidad", SESSION_EXPIRED: "La sesión venció", POLICY_BLOCKED: "Bloqueado por la política",
  NOT_FOUND: "No encontrado", INVALID_ARGUMENT: "Argumentos inválidos", RATE_LIMITED: "Demasiados intentos",
  FORBIDDEN: "Herramienta no permitida al modelo", TOOL_UNAVAILABLE: "Servicio no disponible", CONFIRMATION_REQUIRED: "Falta la confirmación del cliente",
};
const NEXT = {
  confirm_candidate: "confirmar el movimiento con el cliente", ask_customer_to_pick: "pedir al cliente que elija",
  ask_one_clarifying_question: "hacer una pregunta para aclarar", handoff: "pasar a un especialista",
  reauthenticate: "pedir que verifique su identidad", ask_customer_to_confirm_then_create: "pedir confirmación y luego crear",
  ask_code_again: "pedir el código de nuevo", create_case: "crear el reclamo",
};
const REASON = {
  customer_status_restricted: "Cuenta restringida", suspected_card_compromise: "Posible tarjeta comprometida",
  card_block_request: "Pide bloquear la tarjeta", explicit_human_request: "Pidió hablar con una persona", tool_failure: "Falla técnica",
  low_intent_confidence: "No se entendió la solicitud", no_match_after_clarification: "No apareció el movimiento",
  outside_dispute_window: "Fuera del plazo de 90 días", amount_above_threshold: "Monto sobre el umbral", complaint_routing: "Queja",
};
const QUEUE = { account_restrictions: "Restricciones de cuenta", disputes: "Disputas", cards: "Tarjetas", complaints: "Quejas", fraud: "Fraude", general: "General" };
const PRIO = { high: "Alta", medium: "Media", low: "Baja" };
const STATUS = { Open: "Abierto", "In Process": "En proceso", Closed: "Cerrado", Resolved: "Resuelto", open: "Abierto" };
const PRODUCT = { "Checking Account": ["Cuenta corriente", "Conta corrente"], "Savings Account": ["Cuenta de ahorros", "Poupança"],
  "Credit Card": ["Tarjeta de crédito", "Cartão de crédito"], "Debit Card": ["Tarjeta de débito", "Cartão de débito"],
  "Personal Loan": ["Préstamo personal", "Empréstimo pessoal"], Mortgage: ["Hipoteca", "Financiamento"], Investment: ["Inversión", "Investimento"], Insurance: ["Seguro", "Seguro"] };
const TXN = { Purchase: ["Compra", "Compra"], Withdrawal: ["Retiro", "Saque"], Transfer: ["Transferencia", "Transferência"],
  Payment: ["Pago", "Pagamento"], Deposit: ["Depósito", "Depósito"], Adjustment: ["Cargo del banco", "Tarifa do banco"] };
const CHANNEL = { ATM: ["Cajero", "Caixa eletrônico"], POS: ["Comercio", "Maquininha"], Web: ["Web", "Web"], App: ["App", "App"], Branch: ["Sucursal", "Agência"], Transfer: ["Transferencia", "Transferência"] };
const DISPUTE = { unrecognized: ["Cargo no reconocido", "Compra não reconhecida"], incorrect: ["Cobro incorrecto", "Cobrança incorreta"] };
const T = {
  es: {
    pick: "¿Cuál movimiento no reconoces?", pickLede: "Son tus movimientos según el registro del banco. Elige uno.",
    chosen: "Elegido", intl: "Internacional", confirmTitle: "Revisa los datos antes de abrir el reclamo",
    confirmLede: "Estos datos salen del registro del banco, no de lo que escribiste.", confirmYes: "Sí, confirmo", confirmNo: "No es correcto",
    confirmMsg: "Sí, confirmo que esos datos son correctos y quiero abrir el reclamo.", denyMsg: "No, esos datos no son correctos.",
    caseTitle: "Reclamo abierto", ticketTitle: "Te atenderá un especialista", authTitle: "Verificación segura",
    authExpired: "Tu sesión venció. Verifica tu identidad otra vez para seguir.", authIntro: "El asistente no ve tu documento ni el código.",
    docType: "Tipo de documento", docNumber: "Número de documento", sendCode: "Enviar código", code: "Código que llegó por SMS", verify: "Verificar",
    codeSent: "Te enviamos un código de 6 dígitos por SMS.", wrongCode: (n) => `El código no coincide. Te quedan ${n} intentos.`,
    locked: "Demasiados intentos. Pide un código nuevo.", movement: "Movimiento", date: "Fecha", amount: "Monto", product: "Producto",
    channel: "Canal", kind: "Tipo de reclamo", priority: "Prioridad", firstResponse: "Primera respuesta", inHours: (h) => `en ${h} h`,
    number: "Número", status: "Estado", queue: "Cola", verifiedCase: "El servicio confirmó el reclamo al releerlo.",
    verifiedTicket: "El servicio confirmó el ticket al releerlo.", backedBy: "Respaldado por",
    confirmedState: "Confirmaste estos datos.", deniedState: "Marcaste que los datos no son correctos.", pickMsg: (m) => `Es este: ${m}`,
  },
  pt: {
    pick: "Qual movimento você não reconhece?", pickLede: "São seus movimentos segundo o registro do banco. Escolha um.",
    chosen: "Escolhido", intl: "Internacional", confirmTitle: "Confira os dados antes de abrir a contestação",
    confirmLede: "Esses dados vêm do registro do banco, não do que você escreveu.", confirmYes: "Sim, confirmo", confirmNo: "Não está correto",
    confirmMsg: "Sim, confirmo que os dados estão corretos e quero abrir a contestação.", denyMsg: "Não, esses dados não estão corretos.",
    caseTitle: "Contestação aberta", ticketTitle: "Um especialista vai atender você", authTitle: "Verificação segura",
    authExpired: "Sua sessão expirou. Confirme sua identidade de novo para continuar.", authIntro: "O assistente não vê seu documento nem o código.",
    docType: "Tipo de documento", docNumber: "Número do documento", sendCode: "Enviar código", code: "Código recebido por SMS", verify: "Verificar",
    codeSent: "Enviamos um código de 6 dígitos por SMS.", wrongCode: (n) => `O código não confere. Restam ${n} tentativas.`,
    locked: "Muitas tentativas. Peça um novo código.", movement: "Movimento", date: "Data", amount: "Valor", product: "Produto",
    channel: "Canal", kind: "Tipo de contestação", priority: "Prioridade", firstResponse: "Primeira resposta", inHours: (h) => `em ${h} h`,
    number: "Número", status: "Status", queue: "Fila", verifiedCase: "O serviço confirmou a contestação ao reler o registro.",
    verifiedTicket: "O serviço confirmou o chamado ao reler o registro.", backedBy: "Com base em",
    confirmedState: "Você confirmou esses dados.", deniedState: "Você indicou que os dados não estão corretos.", pickMsg: (m) => `É este: ${m}`,
  },
};
const t = (k) => (T[state.language] || T.es)[k];
const tr = (map, key) => (map[key] ? map[key][state.language === "pt" ? 1 : 0] : key || "");

// ---- helpers -----------------------------------------------------------------------------------------------------------
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"); }
function icon(name) { return `<svg class="ic" aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function money(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  const loc = { COP: "es-CO", ARS: "es-AR", MXN: "es-MX", BRL: "pt-BR", USD: "es-MX" }[currency] || "es";
  try { return new Intl.NumberFormat(loc, { style: "currency", currency, currencyDisplay: "code" }).format(amount); }
  catch { return `${currency} ${amount}`; }
}
function when(ts, withTime = true) {
  if (!ts) return "—";
  const d = new Date(ts.length <= 10 ? ts + "T00:00" : ts);
  const opts = { day: "numeric", month: "short", year: "numeric" };
  if (withTime && ts.length > 10) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleString(state.language === "pt" ? "pt-BR" : "es", opts);
}
function hhmm(ts) { return ts ? new Date(ts).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" }) : "—"; }
function secs(ms) { return (ms / 1000).toLocaleString("es", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " s"; }
function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }
function add(node) { $("#messages").appendChild(node); scrollDown(); return node; }

// ---- rail: turn display, stages, session --------------------------------------------------------------------------
function setStage(name, value) { state.stages[name] = value; renderStages(); }
function renderStages() {
  document.querySelectorAll("#stages li").forEach((li) => {
    li.className = state.stages[li.dataset.stage] || "";
    li.setAttribute("aria-current", state.stages[li.dataset.stage] === "current" ? "step" : "false");
  });
}
function setDisplay(label, caption, called = false) {
  const d = $("#display");
  $("#display-label").textContent = label;
  $("#display-caption").textContent = caption;
  $("#turn-number").textContent = state.label;
  d.classList.remove("called");
  if (called) { void d.offsetWidth; d.classList.add("called"); }
}
function renderSession() {
  const box = $("#session"), meter = $("#session-meter");
  box.classList.remove("on", "expired");
  if (!state.session) {
    $("#session-state").textContent = "Sin identificar";
    $("#session-detail").textContent = "El cliente verifica su identidad en el formulario seguro.";
    meter.hidden = true;
    return;
  }
  const left = (new Date(state.session.expires_at) - new Date(state.now)) / 60000;
  if (left <= 0) {
    box.classList.add("expired");
    $("#session-state").textContent = "Sesión vencida";
    $("#session-detail").textContent = "Pasaron los 15 minutos de la sesión. Debe verificar su identidad otra vez.";
    meter.hidden = true;
    return;
  }
  box.classList.add("on");
  $("#session-state").textContent = "Identidad verificada";
  $("#session-detail").textContent = `Sesión ${state.session.session_ref}, vence a las ${hhmm(state.session.expires_at)} (reloj del servicio).`;
  meter.hidden = false;
  $("#session-fill").style.transform = `scaleX(${Math.max(0, Math.min(1, left / SESSION_MIN))})`;
}
async function refreshClock() {
  try {
    const out = await api("/api/clock");
    state.now = out.now;
    $("#clock-now").textContent = when(out.now);
    renderSession();
  } catch { /* the clock is informational */ }
}

// ---- conversation ----------------------------------------------------------------------------------------------------
async function newConversation() {
  const out = await api("/api/conversations", { language: "es", reset_clock: true });
  Object.assign(state, { conv: out.conversation_id, label: out.label, language: "es", session: null, now: out.now, stages: {}, called: false, turnSeq: 0 });
  $("#messages").innerHTML = "";
  $("#trace").innerHTML = '<p class="trace-empty">Cuando el cliente escriba, aquí aparece cada paso del asistente.</p>';
  $("#starters").hidden = false;
  $("#ticket-number").textContent = out.label;
  $("#ticket-time").textContent = when(out.now);
  $("#clock-now").textContent = when(out.now);
  setDisplay("Tu turno", "Atención de reclamos");
  $("#stage-result-name").textContent = "Resultado";
  renderStages(); renderSession(); showSms(null);
  add(el(`<div class="msg bot"><p>Hola, soy el asistente de LATAM Bank. Te ayudo con cargos que no reconoces y con cobros o comisiones incorrectos. También puedo revisar tu saldo y tus movimientos. ¿Qué necesitas?</p></div>`));
}

async function send(text) {
  text = text.trim();
  if (!text || state.busy) return;
  $("#starters").hidden = true;
  document.querySelectorAll(".turn.lit").forEach((n) => n.classList.remove("lit"));  // acting on the reply counts as having seen it
  add(el(`<div class="msg customer"><p>${esc(text)}</p></div>`));
  await runTurn(() => api(`/api/conversations/${state.conv}/messages`, { text }));
}

async function runTurn(call) {
  state.busy = true; $("#send").disabled = true;
  const typing = add(el(`<div class="typing" role="status" aria-label="El asistente está escribiendo"><i></i><i></i><i></i></div>`));
  try {
    const out = await call();
    typing.remove();
    if (out.skip) return;
    if (out.turn && out.turn.language) state.language = out.turn.language;
    renderReply(out);
  } catch (e) {
    typing.remove();
    add(el(`<p class="note warn">${icon("clock")}No hubo respuesta del servidor. Revisa que la app siga corriendo y vuelve a enviar el mensaje.</p>`));
  } finally {
    state.busy = false; $("#send").disabled = false; $("#input").focus();
    refreshClock(); refreshConsoleCount();
  }
}

function renderReply(out) {
  const turn = out.turn;
  const msg = el(`<div class="msg bot"><p>${md(out.reply)}</p></div>`);
  if (turn && turn.tools.length) {
    const proof = el(`<div class="proof"><span class="proof-label">${esc(t("backedBy"))}</span></div>`);
    const seq = state.turnSeq + 1;
    turn.tools.forEach((tool, i) => {
      const chip = el(`<button type="button" class="rcpt${tool.ok ? "" : " err"}" title="${esc(tool.tool)} ${esc(tool.tool_call_id || "")}"><span class="dot"></span>${esc(TOOL[tool.tool] || tool.tool)}</button>`);
      chip.addEventListener("click", () => flashStep(seq, i));
      proof.appendChild(chip);
    });
    msg.appendChild(proof);
  }
  add(msg);
  (out.blocks || []).forEach(renderBlock);
  if (turn) renderTurn(turn);
}

// ---- cards -------------------------------------------------------------------------------------------------------------
function renderBlock(b) {
  if (b.type === "auth_required") {
    if (b.expired) { state.session = null; renderSession(); }
    setStage("identity", "current");
    return renderAuth(b.expired);
  }
  if (b.type === "candidates") { setStage("identity", "done"); setStage("movement", "current"); return renderCandidates(b); }
  if (b.type === "confirm") { setStage("identity", "done"); setStage("movement", "done"); setStage("confirm", "current"); return renderConfirm(b); }
  if (b.type === "case") {
    ["identity", "movement", "confirm"].forEach((s) => (state.stages[s] = "done"));
    $("#stage-result-name").textContent = "Reclamo abierto";
    setStage("result", "done");
    setDisplay("Turno atendido", `Reclamo ${b.case_id} abierto`);
    return renderCase(b);
  }
  if (b.type === "ticket") {
    if (state.stages.identity !== "done" && state.session) state.stages.identity = "done";
    $("#stage-result-name").textContent = "Con especialista";
    ["movement", "confirm"].forEach((st) => { if (state.stages[st] === "current") state.stages[st] = ""; });
    setStage("result", "called");
    state.called = true;
    setDisplay("Llamando", "Pase con un especialista", true);
    return renderTicket(b);
  }
}

function mvParts(m) {
  const what = m.merchant && m.merchant.untrusted_text ? m.merchant.untrusted_text : tr(TXN, m.transaction_type);
  return { what, type: tr(TXN, m.transaction_type), product: tr(PRODUCT, m.product_type_en), channel: tr(CHANNEL, m.channel) };
}

function renderCandidates(b) {
  const card = el(`<section class="card" aria-label="${esc(t("pick"))}">
    <header class="card-head"><h3>${esc(t("pick"))}</h3></header>
    <div class="card-body"><p class="card-lede">${esc(t("pickLede"))}</p><ul class="movements"></ul></div></section>`);
  const list = card.querySelector(".movements");
  b.items.forEach((m) => {
    const p = mvParts(m);
    const li = el(`<li><button type="button" class="mv">
      <span class="mv-what">${esc(p.what)}</span>
      <span class="mv-amt">${esc(money(m.amount, m.currency))}</span>
      <span class="mv-meta"><span>${esc(when(m.event_ts || m.event_date))}</span><span>${esc(p.type)}</span><span>${esc(p.product)}</span><span>${esc(p.channel)}</span>${m.is_international ? `<span class="tag-intl">${esc(t("intl"))}</span>` : ""}</span>
    </button></li>`);
    const btn = li.querySelector("button");
    btn.addEventListener("click", () => {
      list.querySelectorAll("button").forEach((x) => (x.disabled = true));
      btn.classList.add("chosen");
      send(t("pickMsg")(`${p.what}, ${money(m.amount, m.currency)}, ${when(m.event_ts || m.event_date)} (${m.transaction_id})`));
    });
    list.appendChild(li);
  });
  add(card);
}

function renderConfirm(b) {
  const f = b.facts || {}, p = b.preview || {};
  const what = f.merchant && f.merchant.untrusted_text ? `${tr(TXN, f.transaction_type)} en ${f.merchant.untrusted_text}` : tr(TXN, f.transaction_type);
  const card = el(`<section class="card" aria-label="${esc(t("confirmTitle"))}">
    <header class="card-head"><h3>${esc(t("confirmTitle"))}</h3><code>${esc(b.evidence || "")}</code></header>
    <div class="card-body">
      <p class="card-lede">${esc(t("confirmLede"))}</p>
      <dl class="facts">
        <dt>${esc(t("amount"))}</dt><dd class="big">${esc(money(f.amount, f.currency))}</dd>
        <dt>${esc(t("movement"))}</dt><dd>${esc(what)}</dd>
        <dt>${esc(t("date"))}</dt><dd>${esc(when(f.event_ts || f.event_date))}</dd>
        <dt>${esc(t("product"))}</dt><dd>${esc(tr(PRODUCT, f.product_type_en))}${f.number_last4 ? ` terminada en ${esc(f.number_last4)}` : ""}</dd>
        <dt>${esc(t("channel"))}</dt><dd>${esc(tr(CHANNEL, f.channel) || "—")}${f.is_international ? `, ${esc(t("intl").toLowerCase())}` : ""}</dd>
        <dt>${esc(t("kind"))}</dt><dd>${esc(p.subcategory || tr(DISPUTE, p.dispute_type) || "—")}</dd>
        <dt>${esc(t("priority"))}</dt><dd><span class="prio ${esc(p.priority || "")}">${esc(PRIO[p.priority] || p.priority || "—")}</span> ${p.first_response_hours != null ? `${esc(t("firstResponse").toLowerCase())} ${esc(t("inHours")(p.first_response_hours))}` : ""}</dd>
      </dl>
      <div class="actions"><button type="button" class="btn primary" data-a="yes">${icon("check")}${esc(t("confirmYes"))}</button><button type="button" class="btn" data-a="no">${esc(t("confirmNo"))}</button></div>
    </div></section>`);
  card.querySelectorAll("[data-a]").forEach((btn) => btn.addEventListener("click", () => {
    const yes = btn.dataset.a === "yes";
    card.querySelector(".actions").replaceWith(el(`<p class="resolved${yes ? "" : " no"}">${icon(yes ? "check" : "x")}${esc(yes ? t("confirmedState") : t("deniedState"))}</p>`));
    send(yes ? t("confirmMsg") : t("denyMsg"));
  }));
  add(card);
}

function renderCase(b) {
  const c = b.case || {};
  add(el(`<section class="card result" aria-label="${esc(t("caseTitle"))}">
    <header class="card-head"><h3>${icon("check")}${esc(t("caseTitle"))}</h3><code>${esc(b.evidence || "")}</code></header>
    <div class="card-body">
      <dl class="facts">
        <dt>${esc(t("number"))}</dt><dd class="case-id">${esc(b.case_id)}</dd>
        <dt>${esc(t("kind"))}</dt><dd>${esc(c.subcategory || tr(DISPUTE, c.dispute_type) || "—")}</dd>
        <dt>${esc(t("amount"))}</dt><dd>${esc(money(c.amount, c.currency))}</dd>
        <dt>${esc(t("status"))}</dt><dd>${esc(STATUS[b.status] || b.status)}</dd>
        <dt>${esc(t("priority"))}</dt><dd><span class="prio ${esc(b.priority || "")}">${esc(PRIO[b.priority] || b.priority)}</span> ${esc(t("firstResponse").toLowerCase())} ${esc(t("inHours")(b.first_response_hours))}</dd>
      </dl>
      <p class="card-lede">${esc(t("verifiedCase"))}</p>
    </div></section>`));
}

function renderTicket(b) {
  add(el(`<section class="card called-card" aria-label="${esc(t("ticketTitle"))}">
    <header class="card-head"><h3>${icon("handoff")}${esc(t("ticketTitle"))}</h3><code>${esc(b.evidence || "")}</code></header>
    <div class="card-body">
      <dl class="facts">
        <dt>Ticket</dt><dd class="case-id">${esc(b.ticket_id)}</dd>
        <dt>${esc(t("queue"))}</dt><dd>${esc(QUEUE[b.queue] || b.queue || "—")}</dd>
        ${b.first_response_hours != null ? `<dt>${esc(t("firstResponse"))}</dt><dd>${esc(t("inHours")(b.first_response_hours))}</dd>` : ""}
      </dl>
      <p class="card-lede">${esc(t("verifiedTicket"))}</p>
    </div></section>`));
}

function renderAuth(expired) {
  document.querySelectorAll(".card.secure").forEach((c) => c.remove());
  const card = el(`<section class="card secure" aria-label="${esc(t("authTitle"))}">
    <header class="card-head"><h3>${icon("lock")}${esc(t("authTitle"))}</h3></header>
    <div class="card-body">
      <p class="card-lede">${esc(expired ? t("authExpired") : t("authIntro"))}</p>
      <form class="secure-form" data-step="doc" novalidate>
        <div class="pair">
          <label class="field">${esc(t("docType"))}<select name="type" id="auth-type"><option>DNI</option><option>CC</option><option>CE</option><option>Pasaporte</option></select></label>
          <label class="field">${esc(t("docNumber"))}<input name="number" id="auth-number" inputmode="numeric" autocomplete="off" required></label>
        </div>
        <div class="actions"><button class="btn primary" type="submit">${esc(t("sendCode"))}</button></div>
        <p class="form-error" role="alert" hidden></p>
      </form>
    </div></section>`);
  const form = card.querySelector("form");
  if (state.prefill) { form.type.value = state.prefill.document_type; form.number.value = state.prefill.document_number; }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = form.querySelector(".form-error");
    err.hidden = true;
    if (form.dataset.step === "doc") {
      if (!form.number.value.trim()) { err.textContent = "Escribe el número de documento."; err.hidden = false; form.number.focus(); return; }
      const btn = form.querySelector("button"); btn.disabled = true;
      let out;
      try { out = await api(`/api/conversations/${state.conv}/auth/start`, { document_type: form.type.value, document_number: form.number.value }); }
      catch { out = { ok: false, message: "No se pudo enviar el código. Intenta de nuevo." }; }
      btn.disabled = false;
      if (!out.ok) { err.textContent = out.error === "RATE_LIMITED" ? "Demasiados intentos. Espera unos minutos." : (out.message || "No se pudo enviar el código."); err.hidden = false; return; }
      form.dataset.step = "code";
      form.innerHTML = `<p class="card-lede">${esc(t("codeSent"))}</p>
        <label class="field">${esc(t("code"))}<input name="code" id="auth-code" class="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required></label>
        <div class="actions"><button class="btn primary" type="submit">${esc(t("verify"))}</button></div>
        <p class="form-error" role="alert" hidden></p>`;
      form.code.focus();
      pollSms();
    } else {
      const btn = form.querySelector("button"), e2 = form.querySelector(".form-error");
      if (!/^\d{6}$/.test(form.code.value.trim())) { e2.textContent = "El código tiene 6 dígitos."; e2.hidden = false; return; }
      btn.disabled = true;
      await runTurn(async () => {
        const out = await api(`/api/conversations/${state.conv}/auth/verify`, { code: form.code.value });
        if (!out.ok) {
          btn.disabled = false;
          e2.textContent = out.locked ? t("locked") : t("wrongCode")(out.attempts_remaining ?? 0);
          e2.hidden = false;
          return { skip: true };
        }
        card.remove(); showSms(null);
        state.session = out.session;
        setStage("identity", "done");
        if (!state.stages.movement) setStage("movement", "current");
        renderSession();
        add(el(`<p class="note">${icon("check")}Identidad verificada con documento y código. El asistente retoma tu solicitud.</p>`));
        return out;
      });
    }
  });
  add(card);
  (form.number.value ? form.querySelector("button") : form.number).focus();
}

// ---- simulated phone -------------------------------------------------------------------------------------------------------
function showSms(code) {
  $("#sms").hidden = !code;
  $("#sms-code").textContent = code || "";
}
function pollSms() {
  clearInterval(state.smsTimer);
  let n = 0;
  state.smsTimer = setInterval(async () => {
    try {
      const out = await api(`/api/conversations/${state.conv}/phone`);
      if (out.code || ++n > 20) { clearInterval(state.smsTimer); showSms(out.code); }
    } catch { clearInterval(state.smsTimer); }
  }, 350);
}

// ---- trace ---------------------------------------------------------------------------------------------------------------
function policyText(p) {
  if (!p) return "";
  const bits = [];
  if (p.eligible === true) bits.push("se puede reclamar");
  if (p.eligible === false) bits.push("no se puede reclamar");
  if (p.handoff_required === true) bits.push("requiere especialista");
  if (p.handoff_reason) bits.push(`motivo: ${(REASON[p.handoff_reason] || p.handoff_reason).toLowerCase()}`);
  if (p.next_action) bits.push(`siguiente: ${NEXT[p.next_action] || p.next_action.replace(/_/g, " ")}`);
  return bits.join("; ");
}
function renderTurn(turn) {
  const box = $("#trace");
  box.querySelector(".trace-empty")?.remove();
  state.turnSeq += 1;
  const seq = state.turnSeq;
  const kind = turn.kind === "app_event" ? "inicio de sesión" : "mensaje del cliente";
  const tl = turn.timeline && turn.timeline.length ? turn.timeline : turn.model_calls.map((m) => ({ kind: "model", ms: m.latency_ms }));
  const total = tl.reduce((a, s) => a + s.ms, 0) || 1;
  const modelMs = tl.filter((s) => s.kind === "model").reduce((a, s) => a + s.ms, 0);
  const toolMs = tl.filter((s) => s.kind === "tool").reduce((a, s) => a + s.ms, 0);
  const tokens = turn.tokens.prompt + turn.tokens.completion;
  const node = el(`<article class="turn lit" data-seq="${seq}">
    <div class="turn-top"><h3 class="turn-title">Paso ${seq} <span class="turn-kind">${kind}</span><span class="turn-new">Nuevo</span></h3><span class="turn-dur">${secs(turn.latency_ms)}</span></div>
    <div class="timeline" role="img" aria-label="Modelo ${secs(modelMs)}, herramientas ${toolMs} ms"></div>
    <div class="turn-stats"><span class="key"><i></i>Modelo ${secs(modelMs)}</span><span class="key tool"><i></i>Herramientas ${toolMs} ms</span><span>${turn.model_calls.length} llamada${turn.model_calls.length === 1 ? "" : "s"} al modelo</span><span>${tokens.toLocaleString("es")} tokens</span>${turn.cost_usd_est ? `<span>US$ ${turn.cost_usd_est.toFixed(4)}</span>` : ""}</div>
    <ol class="steps"></ol>
  </article>`);
  const bar = node.querySelector(".timeline");
  tl.forEach((s) => {
    const seg = document.createElement("span");
    seg.className = s.kind === "model" ? "model" : "tool" + (s.ok === false ? " err" : "");
    seg.style.flex = `${Math.max(s.ms, 1) / total} 1 0`;
    seg.title = s.kind === "model" ? `Modelo, ${secs(s.ms)}` : `${s.name}, ${s.ms} ms`;
    bar.appendChild(seg);
  });
  const steps = node.querySelector(".steps");
  if (turn.classifier) steps.appendChild(el(`<li class="step"><span class="step-dot"></span><span class="step-name">Clasificador de intención</span><span class="step-ms"></span><span class="step-detail">${esc(JSON.stringify(turn.classifier))}</span></li>`));
  turn.tools.forEach((tool) => {
    const detail = [tool.error ? `<span class="bad">${esc(ERR[tool.error] || tool.error)}</span>` : "", esc(policyText(tool.policy)), tool.runtime_fallback ? "lo llamó la app, no el modelo" : ""].filter(Boolean).join("; ");
    steps.appendChild(el(`<li class="step${tool.ok ? "" : " err"}"><span class="step-dot"></span>
      <span class="step-name">${esc(TOOL[tool.tool] || tool.tool)}</span><span class="step-ms">${tool.latency_ms} ms</span>
      <span class="step-detail">${detail ? detail + "<br>" : ""}<code>${esc(tool.tool)}</code> <code>${esc(tool.tool_call_id || "")}</code></span></li>`));
  });
  if (!turn.tools.length && !turn.classifier) steps.appendChild(el(`<li class="step"><span class="step-dot"></span><span class="step-name">Solo conversación</span><span class="step-ms"></span><span class="step-detail">El asistente respondió sin consultar datos.</span></li>`));
  if (turn.fallback) node.appendChild(el(`<p class="turn-flag">Respuesta de respaldo: el modelo falló y la app pasó el caso a un especialista.</p>`));
  if (turn.unverified_ids_in_reply && turn.unverified_ids_in_reply.length) node.appendChild(el(`<p class="turn-flag">La respuesta menciona ${esc(turn.unverified_ids_in_reply.join(", "))} sin respaldo de una herramienta.</p>`));
  const clear = () => node.classList.remove("lit");
  node.addEventListener("mouseenter", clear);
  node.addEventListener("focusin", clear);
  node.tabIndex = -1;
  box.prepend(node);
}
function flashStep(seq, i) {
  const node = document.querySelector(`.turn[data-seq="${seq}"]`);
  if (!node) return;
  node.classList.remove("lit");
  node.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
  node.classList.add("flash");
  setTimeout(() => node.classList.remove("flash"), 1400);
}

// ---- personas sheet ----------------------------------------------------------------------------------------------------------
function renderPersonas() {
  const box = $("#personas");
  if (!state.personas.length) { box.innerHTML = `<li class="sheet-note">Corre <code>python -m app.build_personas</code> para crear los clientes de prueba.</li>`; return; }
  const country = { MX: "México", CO: "Colombia", AR: "Argentina" };
  state.personas.forEach((p) => {
    const li = el(`<li class="persona"><span class="persona-cc" title="${esc(country[p.country_code] || p.country_code)}">${esc(p.country_code)}</span>
      <span class="persona-name">${esc(p.first_name)}${p.customer_status !== "Active" ? `<span class="persona-flag">${p.customer_status === "Suspended" ? "Suspendida" : esc(p.customer_status)}</span>` : ""}</span>
      <span class="persona-story">${esc(p.story.es)}</span>
      <span class="persona-doc">${esc(p.document_type)} ${esc(p.document_number)}</span>
      <button type="button" class="btn">Usar</button></li>`);
    li.querySelector("button").addEventListener("click", () => {
      state.prefill = p;
      closeSheet();
      const form = document.querySelector(".card.secure form[data-step='doc']");
      if (form) { form.type.value = p.document_type; form.number.value = p.document_number; form.querySelector("button").focus(); }
      else renderAuth(false);
    });
    box.appendChild(li);
  });
}
function openSheet() { $("#personas-sheet").hidden = false; $("#personas-sheet .btn")?.focus(); }
function closeSheet() { $("#personas-sheet").hidden = true; }

async function advanceClock() {
  const out = await api("/api/demo/clock", { minutes: 16 });
  state.now = out.now;
  $("#clock-now").textContent = when(out.now);
  renderSession();
  add(el(`<p class="note warn">${icon("clock")}Reloj del servicio adelantado 16 minutos: ahora son las ${esc(hhmm(out.now))}.</p>`));
}

// ---- specialist console ------------------------------------------------------------------------------------------------------
async function loadConsole() {
  const out = await api("/api/console");
  const calls = $("#calls"), cases = $("#cases");
  calls.innerHTML = out.tickets.length ? "" : '<li><p class="empty">Todavía nadie fue llamado a un especialista.</p></li>';
  out.tickets.forEach((r) => {
    const fresh = !state.seen.has(r.ticket_id);
    const li = el(`<li><button type="button" class="call${fresh ? " lit" : ""}">
      <span class="call-turn${r.turn_label ? "" : " old"}">${esc(r.turn_label || "Previo")}</span>
      <span class="call-reason">${esc(REASON[r.reason_code] || r.reason_code)}</span>
      <span class="call-time">${esc(when(r.created_at))}</span>
      <span class="call-meta"><span>${esc(QUEUE[r.queue] || r.queue || "")}</span><span>${r.identity_verified ? "Identidad verificada" : "Sin identificar"}</span><span>${r.language === "pt" ? "Portugués" : "Español"}</span>${r.priority ? `<span class="prio ${esc(r.priority)}">${esc(PRIO[r.priority] || r.priority)}</span>` : ""}</span>
    </button></li>`);
    const btn = li.querySelector("button");
    if (state.ticketSel === r.ticket_id) btn.setAttribute("aria-current", "true");
    btn.addEventListener("click", () => {
      state.ticketSel = r.ticket_id; state.seen.add(r.ticket_id);
      calls.querySelectorAll(".call").forEach((x) => x.removeAttribute("aria-current"));
      btn.classList.remove("lit"); btn.setAttribute("aria-current", "true");
      showFile(r); setCount(out.tickets.filter((x) => !state.seen.has(x.ticket_id)).length);
    });
    calls.appendChild(li);
  });
  cases.innerHTML = out.cases.length ? "" : '<tr><td colspan="6" class="empty-row">Todavía no se abrió ningún reclamo.</td></tr>';
  out.cases.forEach((c) => cases.appendChild(el(`<table><tbody><tr>
    <td class="nowrap"><code>${esc(c.case_id)}</code></td><td class="nowrap">${esc(c.turn_label || "Previo")}</td><td>${esc(c.subcategory || c.dispute_type)}</td>
    <td class="num">${esc(money(c.amount, c.currency))}</td><td><span class="prio ${esc(c.priority)}">${esc(PRIO[c.priority] || c.priority)}</span></td><td>${esc(when(c.first_response_due_at))}</td>
  </tr></tbody></table>`).querySelector("tr")));
  const sel = out.tickets.find((r) => r.ticket_id === state.ticketSel);
  if (sel) showFile(sel);
  setCount(out.tickets.filter((x) => !state.seen.has(x.ticket_id)).length);
}
function items(list) {
  return list && list.length ? `<ul>${list.map((x) => `<li>${esc(typeof x === "string" ? x : JSON.stringify(x))}</li>`).join("")}</ul>` : '<p class="none">Nada registrado.</p>';
}
function showFile(r) {
  const a = r.agent_reported || {}, pkg = a.package || a, sv = r.service_verified || {};
  const ev = (sv.evidence || []).map((e) => {
    const pd = e.policy_decision || {};
    const ok = e.outcome === "ok";
    const bits = [ok ? "Correcto" : (ERR[e.error_code] || e.error_code || e.outcome), pd.handoff_reason ? `motivo: ${(REASON[pd.handoff_reason] || pd.handoff_reason).toLowerCase()}` : "", pd.next_action ? `siguiente: ${NEXT[pd.next_action] || pd.next_action}` : ""].filter(Boolean).join("; ");
    return `<li><span class="step-dot" style="background:var(--${ok ? "green" : "red"})"></span><span><strong>${esc(TOOL[e.tool] || e.tool)}</strong>, ${esc(bits)}<br><code>${esc(e.tool)} ${esc(e.tool_call_id || "")}</code></span></li>`;
  });
  if (sv.draft) ev.push(`<li><span class="step-dot"></span><span>Borrador de reclamo adjunto${sv.draft.transaction_id ? ` <code>${esc(sv.draft.transaction_id)}</code>` : ""}</span></li>`);
  if ((sv.dropped_evidence || []).length) ev.push(`<li><span class="step-dot" style="background:var(--red)"></span><span>${sv.dropped_evidence.length} evidencia(s) descartada(s) porque el servicio no pudo comprobarlas</span></li>`);
  $("#file").innerHTML = `<div class="file-head">
      <span class="file-turn${r.turn_label ? "" : " old"}">${esc(r.turn_label || "Previo")}</span>
      <div><h2>${esc(REASON[r.reason_code] || r.reason_code)}</h2><p>Ticket <code>${esc(r.ticket_id)}</code>, cola ${esc((QUEUE[r.queue] || r.queue || "").toLowerCase())}</p></div>
    </div>
    <dl class="file-facts">
      <dt>Cliente</dt><dd>${r.identity_verified ? `Identidad verificada, referencia <code>${esc(r.customer_ref)}</code>` : "Sin identificar"}</dd>
      <dt>Idioma</dt><dd>${r.language === "pt" ? "Portugués" : "Español"}</dd>
      ${r.priority ? `<dt>Prioridad</dt><dd><span class="prio ${esc(r.priority)}">${esc(PRIO[r.priority] || r.priority)}</span>${r.first_response_hours != null ? `, primera respuesta en ${esc(r.first_response_hours)} h` : ""}</dd>` : ""}
      <dt>Motivo</dt><dd>${r.reason_check === "consistent" ? "Coincide con lo que registró el servicio" : esc(r.reason_check || "—")}</dd>
      <dt>Recibido</dt><dd>${esc(when(r.created_at))}</dd>
    </dl>
    <section class="file-sec"><h3>Lo que necesita el cliente</h3><p>${esc(r.request_summary || pkg.request_summary || "—")}</p></section>
    <section class="file-sec"><h3>Hechos verificados</h3>${items(pkg.verified_facts)}</section>
    <section class="file-sec"><h3>Lo que hizo el asistente</h3>${items(pkg.actions_taken)}</section>
    <section class="file-sec"><h3>Para revisar</h3>${items(pkg.open_questions)}</section>
    <section class="file-sec"><h3>Evidencia comprobada por el servicio</h3>${ev.length ? `<ul class="evidence">${ev.join("")}</ul>` : '<p class="none">Nada registrado.</p>'}
      <details class="raw"><summary>Registro completo del servicio</summary><pre>${esc(JSON.stringify(sv, null, 2))}</pre></details></section>`;
}
function setCount(n) { const c = $("#console-count"); c.hidden = !n; c.textContent = n; }
async function refreshConsoleCount() {
  try { const out = await api("/api/console"); setCount(out.tickets.filter((x) => !state.seen.has(x.ticket_id)).length); } catch { /* informational */ }
}

function showView(name) {
  const isConsole = name === "console";
  $("#view-customer").hidden = isConsole;
  $("#view-console").hidden = !isConsole;
  $("#nav-customer").toggleAttribute("aria-current", !isConsole);
  $("#nav-console").toggleAttribute("aria-current", isConsole);
  if (!isConsole) $("#nav-customer").setAttribute("aria-current", "page");
  else $("#nav-console").setAttribute("aria-current", "page");
  if (isConsole) loadConsole();
}

// ---- boot ------------------------------------------------------------------------------------------------------------------
async function boot() {
  const cfg = await api("/api/config");
  state.personas = cfg.personas || [];
  $("#model-name").textContent = cfg.model.replace("databricks-", "");
  renderPersonas();
  await newConversation();
  const input = $("#input");
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const v = input.value; input.value = ""; input.style.height = ""; send(v); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; });
  $("#starters").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
  $("#btn-new").addEventListener("click", newConversation);
  $("#btn-clock").addEventListener("click", advanceClock);
  $("#btn-personas").addEventListener("click", () => ($("#personas-sheet").hidden ? openSheet() : closeSheet()));
  $("#btn-close-personas").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
  document.addEventListener("click", (e) => {
    const sheet = $("#personas-sheet");
    if (!sheet.hidden && !sheet.contains(e.target) && !$("#btn-personas").contains(e.target)) closeSheet();
  });
  $("#btn-refresh").addEventListener("click", loadConsole);
  $("#nav-customer").addEventListener("click", () => showView("customer"));
  $("#nav-console").addEventListener("click", () => showView("console"));
  refreshConsoleCount();
}
boot();
