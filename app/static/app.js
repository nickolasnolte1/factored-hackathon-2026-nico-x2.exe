"use strict";

const state = {
  conv: null, key: null, label: "R-201", language: "es", session: null, now: null, personas: [], prefill: null,
  busy: false, smsTimer: null, ticketSel: null, seen: new Set(), stages: {}, called: false, turnSeq: 0,
  demo: true, console: true, limits: {}, pendingDoc: null, signedDoc: null, lastDoc: null, lastCandidates: null,
};
const $ = (sel) => document.querySelector(sel);
const SESSION_MIN = 15;
const TURN_TIMEOUT_MS = 90000;  // a model turn; the server bounds each model call to about 30 s
const SLOW_MS = 20000;

// ---- vocabulary ------------------------------------------------------------------------------------------------------
// Pairs are [Spanish, Portuguese]. The trace, the rail and the specialist console are for the presenter and the
// human agent, so they stay in Spanish; the chat and its cards follow the customer's language.
const TOOL = {
  get_customer_overview: "Resumen del cliente", list_products: "Productos", get_balance: "Saldo",
  list_recent_transactions: "Movimientos recientes", find_candidate_transactions: "Buscar el movimiento",
  explain_decline: "Explicar un rechazo", check_dispute_eligibility: "Revisar si se puede reclamar",
  prepare_dispute_case: "Preparar el reclamo", create_dispute_case: "Crear el reclamo", get_case_status: "Estado del caso",
  get_policy_info: "Consultar la política", handoff_to_human: "Pasar a un especialista",
};
const ERR = {
  AUTH_REQUIRED: "Falta verificar la identidad", SESSION_EXPIRED: "La sesión venció", POLICY_BLOCKED: "Bloqueado por la política",
  NOT_FOUND: "No encontrado", VALIDATION_ERROR: "Argumentos inválidos", INVALID_ARGUMENT: "Argumentos inválidos",
  RATE_LIMITED: "Demasiados intentos", FORBIDDEN: "Herramienta no permitida al modelo", UNAVAILABLE: "Servicio no disponible",
  INTERNAL: "Error interno del servicio", AUTH_FAILED: "Código incorrecto", CONFIRMATION_REQUIRED: "Falta la confirmación del cliente",
};
const NEXT = {
  confirm_candidate: "confirmar el movimiento con el cliente", ask_customer_to_pick: "pedir al cliente que elija",
  ask_one_clarifying_question: "hacer una pregunta para aclarar", handoff: "pasar a un especialista",
  reauthenticate: "pedir que verifique su identidad", ask_customer_to_confirm_then_create: "pedir confirmación y luego crear",
  ask_customer_to_confirm_then_handoff: "pedir confirmación y pasar a un especialista",
  ask_code_again: "pedir el código de nuevo", create_case: "crear el reclamo", explain_decline: "explicar el rechazo",
};
const REASON = {
  customer_status_restricted: "Cuenta restringida", suspected_card_compromise: "Posible tarjeta comprometida",
  card_block_request: "Pide bloquear la tarjeta", explicit_human_request: "Pidió hablar con una persona", tool_failure: "Falla técnica",
  low_intent_confidence: "No se entendió la solicitud", no_match_after_clarification: "No apareció el movimiento",
  outside_dispute_window: "Fuera del plazo de 90 días", amount_above_threshold: "Monto sobre el umbral", complaint_routing: "Queja",
};
const REASON_CHECK = {
  consistent: "Coincide con lo que registró el servicio", inconsistent: "No coincide con lo que registró el servicio",
  not_verifiable: "El servicio no pudo comprobarlo",
};
const QUEUE = { account_restrictions: ["Restricciones de cuenta", "Restrições de conta"], disputes: ["Disputas", "Contestações"],
  cards: ["Tarjetas", "Cartões"], complaints: ["Quejas", "Reclamações"], fraud: ["Fraude", "Fraude"], general: ["General", "Geral"] };
const PRIO = { high: ["Alta", "Alta"], medium: ["Media", "Média"], low: ["Baja", "Baixa"] };
const STATUS = { Open: ["Abierto", "Aberta"], open: ["Abierto", "Aberta"], "In Process": ["En proceso", "Em andamento"],
  Closed: ["Cerrado", "Encerrada"], Resolved: ["Resuelto", "Resolvida"], queued: ["En cola", "Na fila"] };
const PRODUCT = { "Checking Account": ["Cuenta corriente", "Conta corrente"], "Savings Account": ["Cuenta de ahorros", "Poupança"],
  "Credit Card": ["Tarjeta de crédito", "Cartão de crédito"], "Debit Card": ["Tarjeta de débito", "Cartão de débito"],
  "Personal Loan": ["Préstamo personal", "Empréstimo pessoal"], Mortgage: ["Hipoteca", "Financiamento"], Investment: ["Inversión", "Investimento"], Insurance: ["Seguro", "Seguro"] };
const TXN = { Purchase: ["Compra", "Compra"], Withdrawal: ["Retiro", "Saque"], Transfer: ["Transferencia", "Transferência"],
  Payment: ["Pago", "Pagamento"], Deposit: ["Depósito", "Depósito"], Adjustment: ["Cargo del banco", "Tarifa do banco"] };
const CHANNEL = { ATM: ["Cajero", "Caixa eletrônico"], POS: ["Comercio", "Maquininha"], Web: ["Web", "Web"], App: ["App", "App"], Branch: ["Sucursal", "Agência"], Transfer: ["Transferencia", "Transferência"] };
const DISPUTE = { unrecognized: ["Cargo no reconocido", "Compra não reconhecida"], incorrect: ["Cobro incorrecto", "Cobrança incorreta"] };
const HANDOFF_WHY = { amount_above_threshold: ["Por el monto", "Pelo valor"], outside_dispute_window: ["Por la fecha del movimiento", "Pela data do movimento"],
  suspected_card_compromise: ["Porque tu tarjeta podría estar comprometida", "Como seu cartão pode estar comprometido"] };
const MONTHS = { es: ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
  pt: ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"] };
const T = {
  es: {
    greeting: "Hola, soy el asistente de LATAM Bank. Te ayudo con cargos que no reconoces y con cobros o comisiones incorrectos. También puedo revisar tu saldo y tus movimientos. ¿Qué necesitas?",
    pick: "¿Cuál movimiento no reconoces?", pickDecline: "¿Cuál pago quieres revisar?", pickLede: "Son tus movimientos según el registro del banco. Elige uno.",
    pickAgain: "Puedes elegir otro movimiento de la lista.",
    pickMsg: (type, date, amount, id) => `Es este: ${type.toLowerCase()} del ${date}, ${amount} (${id}).`,
    chosen: "Elegido", intl: "Internacional", confirmTitle: "Revisa los datos antes de abrir el reclamo",
    confirmLede: "Estos datos salen del registro del banco, no de lo que escribiste.", confirmYes: "Sí, confirmo", confirmNo: "No es correcto",
    confirmMsg: "Sí, confirmo que esos datos son correctos y quiero abrir el reclamo.", denyMsg: "No, esos datos no son correctos.",
    confirmTitleHandoff: "Revisa los datos antes de pasarlo a un especialista",
    confirmLedeHandoff: (why) => `${why}, este reclamo lo revisa un especialista. Los datos salen del registro del banco, no de lo que escribiste.`,
    whyGeneric: "Por las reglas del banco", confirmYesHandoff: "Sí, confirmo y paso con un especialista",
    confirmMsgHandoff: "Sí, confirmo que esos datos son correctos y quiero que lo revise un especialista.",
    priorityHandoff: "Prioridad con el especialista",
    caseTitle: "Reclamo abierto", caseExisting: "Ya tenías un reclamo abierto",
    caseExistingLede: "Es el reclamo que ya estaba abierto por este movimiento; no se creó uno nuevo.",
    ticketTitle: "Te atenderá un especialista", ticket: "Ticket", authTitle: "Verificación segura",
    authExpired: "Tu sesión venció. Verifica tu identidad otra vez para seguir.", authIntro: "El asistente no ve tu documento ni el código.",
    docType: "Tipo de documento", docNumber: "Número de documento", passport: "Pasaporte", sendCode: "Enviar código",
    code: "Código que llegó por SMS", verify: "Verificar",
    codeSent: "Te enviamos un código de 6 dígitos por SMS.", wrongCode: (n) => `El código no coincide. Te quedan ${n} intentos.`,
    locked: "Demasiados intentos con este código.", codeExpired: "Este código ya no sirve.", newCode: "Pedir otro código",
    needDoc: "Escribe el número de documento.", codeDigits: "El código tiene 6 dígitos.",
    sendFailed: "No se pudo enviar el código. Intenta de nuevo.", badDoc: "Revisa el tipo y el número de documento.",
    rateLimited: (m) => `Se pidieron demasiados códigos para este documento o en este turno. Podrás pedir otro en ${m} min.`,
    rateLimitedDemo: "Mientras tanto, empieza un turno nuevo o usa otro cliente de prueba.",
    otherCustomer: "En este turno ya se identificó otro cliente. Para atender a otro cliente, empieza un turno nuevo.",
    verifiedNote: "Identidad verificada con documento y código. El asistente retoma tu solicitud.",
    movement: "Movimiento", date: "Fecha", amount: "Monto", product: "Producto", channel: "Canal", kind: "Tipo de reclamo",
    priority: "Prioridad", firstResponse: "Primera respuesta", inHours: (h) => `en ${h} h`, number: "Número", status: "Estado",
    queue: "Cola", verifiedCase: "El servicio confirmó el reclamo al releerlo.", verifiedTicket: "El servicio confirmó el ticket al releerlo.",
    backedBy: "Respaldado por", confirmedState: "Confirmaste estos datos.", deniedState: "Marcaste que los datos no son correctos.",
    movementAt: (type, merchant) => `${type} en ${merchant}`, endingIn: (n) => `, terminada en ${n}`,
    typing: "El asistente está escribiendo", slow: "Está tardando más de lo normal…",
    waitHint: "Espera la respuesta del asistente para enviar este mensaje. Tu texto sigue aquí.",
    newTurn: "Nuevo turno",
    err404: "Este turno ya no existe en el servidor: la app se reinició o pasó mucho tiempo sin actividad. Empieza un turno nuevo.",
    errBusy: "El asistente todavía está terminando tu mensaje anterior. Espera unos segundos y vuelve a enviarlo.",
    errTurns: (n) => `Este turno llegó al máximo de ${n} mensajes. Empieza un turno nuevo para seguir.`,
    errTooLong: "El mensaje es demasiado largo. Acórtalo y vuelve a enviarlo.",
    errTimeout: "El asistente está tardando demasiado en responder. Espera unos segundos y vuelve a enviar el mensaje, o empieza un turno nuevo.",
    errNetwork: "No hay conexión con el servidor. Revisa que la app siga corriendo y vuelve a enviar el mensaje.",
    errServer: "El servidor tuvo un error al procesar el mensaje. Vuelve a intentarlo; si se repite, empieza un turno nuevo.",
    errServerBusy: "El servidor está atendiendo muchas conversaciones a la vez. Espera unos segundos y vuelve a intentarlo.",
    errNewLimit: (s) => `Se abrieron muchos turnos seguidos desde este navegador. Espera ${Math.ceil(s / 60)} min y vuelve a intentarlo.`,
    placeholder: "Escribe tu mensaje",
  },
  pt: {
    greeting: "Olá, sou o assistente do LATAM Bank. Ajudo com compras que você não reconhece e com cobranças ou tarifas incorretas. Também posso consultar seu saldo e seus movimentos. Do que você precisa?",
    pick: "Qual movimento você não reconhece?", pickDecline: "Qual pagamento você quer verificar?", pickLede: "São seus movimentos segundo o registro do banco. Escolha um.",
    pickAgain: "Você pode escolher outro movimento da lista.",
    pickMsg: (type, date, amount, id) => `É este: ${type.toLowerCase()} de ${date}, ${amount} (${id}).`,
    chosen: "Escolhido", intl: "Internacional", confirmTitle: "Confira os dados antes de abrir a contestação",
    confirmLede: "Esses dados vêm do registro do banco, não do que você escreveu.", confirmYes: "Sim, confirmo", confirmNo: "Não está correto",
    confirmMsg: "Sim, confirmo que os dados estão corretos e quero abrir a contestação.", denyMsg: "Não, esses dados não estão corretos.",
    confirmTitleHandoff: "Confira os dados antes de passar para um especialista",
    confirmLedeHandoff: (why) => `${why}, esta contestação será analisada por um especialista. Os dados vêm do registro do banco, não do que você escreveu.`,
    whyGeneric: "Pelas regras do banco", confirmYesHandoff: "Sim, confirmo e quero um especialista",
    confirmMsgHandoff: "Sim, confirmo que os dados estão corretos e quero que um especialista analise.",
    priorityHandoff: "Prioridade com o especialista",
    caseTitle: "Contestação aberta", caseExisting: "Você já tinha uma contestação aberta",
    caseExistingLede: "É a contestação que já estava aberta para este movimento; nenhuma nova foi criada.",
    ticketTitle: "Um especialista vai atender você", ticket: "Protocolo", authTitle: "Verificação segura",
    authExpired: "Sua sessão expirou. Confirme sua identidade de novo para continuar.", authIntro: "O assistente não vê seu documento nem o código.",
    docType: "Tipo de documento", docNumber: "Número do documento", passport: "Passaporte", sendCode: "Enviar código",
    code: "Código recebido por SMS", verify: "Verificar",
    codeSent: "Enviamos um código de 6 dígitos por SMS.", wrongCode: (n) => `O código não confere. Restam ${n} tentativas.`,
    locked: "Tentativas demais com este código.", codeExpired: "Este código não vale mais.", newCode: "Pedir outro código",
    needDoc: "Digite o número do documento.", codeDigits: "O código tem 6 dígitos.",
    sendFailed: "Não foi possível enviar o código. Tente de novo.", badDoc: "Confira o tipo e o número do documento.",
    rateLimited: (m) => `Foram pedidos códigos demais para este documento ou neste atendimento. Você poderá pedir outro em ${m} min.`,
    rateLimitedDemo: "Enquanto isso, comece um novo atendimento ou use outro cliente de teste.",
    otherCustomer: "Outro cliente já se identificou neste atendimento. Para atender outro cliente, comece um novo atendimento.",
    verifiedNote: "Identidade verificada com documento e código. O assistente retoma sua solicitação.",
    movement: "Movimento", date: "Data", amount: "Valor", product: "Produto", channel: "Canal", kind: "Tipo de contestação",
    priority: "Prioridade", firstResponse: "Primeira resposta", inHours: (h) => `em ${h} h`, number: "Número", status: "Status",
    queue: "Fila", verifiedCase: "O serviço confirmou a contestação ao reler o registro.", verifiedTicket: "O serviço confirmou o protocolo ao reler o registro.",
    backedBy: "Com base em", confirmedState: "Você confirmou esses dados.", deniedState: "Você indicou que os dados não estão corretos.",
    movementAt: (type, merchant) => `${type} em ${merchant}`, endingIn: (n) => `, final ${n}`,
    typing: "O assistente está digitando", slow: "Está demorando mais que o normal…",
    waitHint: "Espere a resposta do assistente para enviar esta mensagem. Seu texto continua aqui.",
    newTurn: "Novo atendimento",
    err404: "Este atendimento não existe mais no servidor: o app foi reiniciado ou ficou muito tempo sem atividade. Comece um novo atendimento.",
    errBusy: "O assistente ainda está terminando sua mensagem anterior. Espere alguns segundos e envie de novo.",
    errTurns: (n) => `Este atendimento chegou ao máximo de ${n} mensagens. Comece um novo atendimento para continuar.`,
    errTooLong: "A mensagem está longa demais. Encurte e envie de novo.",
    errTimeout: "O assistente está demorando demais para responder. Espere alguns segundos e envie a mensagem de novo, ou comece um novo atendimento.",
    errNetwork: "Sem conexão com o servidor. Verifique se o app continua rodando e envie a mensagem de novo.",
    errServer: "O servidor teve um erro ao processar a mensagem. Tente de novo; se continuar, comece um novo atendimento.",
    errServerBusy: "O servidor está atendendo muitas conversas ao mesmo tempo. Espere alguns segundos e tente de novo.",
    errNewLimit: (s) => `Muitos atendimentos foram abertos seguidos neste navegador. Espere ${Math.ceil(s / 60)} min e tente de novo.`,
    placeholder: "Escreva sua mensagem",
  },
};
const t = (k) => (T[state.language] || T.es)[k];
const tr = (map, key, lang = state.language) => (map[key] ? map[key][lang === "pt" ? 1 : 0] : key || "");

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
// One date format everywhere: "15 jun 2026" or "15 jun 2026, 18:30". Service times are naive (the data's local
// time), so they are read as text, never shifted by the browser's time zone.
function when(ts, withTime = true, lang = state.language) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(String(ts || ""));
  if (!m) return "—";
  const day = `${Number(m[3])} ${MONTHS[lang === "pt" ? "pt" : "es"][Number(m[2]) - 1]} ${m[1]}`;
  return withTime && m[4] ? `${day}, ${m[4]}:${m[5]}` : day;
}
function hhmm(ts) { const m = /[T ](\d{2}):(\d{2})/.exec(String(ts || "")); return m ? `${m[1]}:${m[2]}` : "—"; }
function minutesBetween(from, to) {
  const n = (s) => Date.parse(String(s || "").slice(0, 19).replace(" ", "T") + "Z");
  return (n(to) - n(from)) / 60000;
}
function secs(ms) { return (ms / 1000).toLocaleString("es", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " s"; }
function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
function docKey(type, number) { return `${String(type || "").trim().toUpperCase()}|${String(number || "").toUpperCase().replace(/[^A-Z0-9]/g, "")}`; }

class ApiError extends Error {
  constructor(status, detail) { super(String(status)); this.status = status; this.detail = detail || {}; }
}
async function api(path, body, timeoutMs = 20000) {
  const headers = {};
  if (state.key) headers["X-Conversation-Key"] = state.key;
  const init = { headers };
  if (body !== undefined) { init.method = "POST"; headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const ctrl = new AbortController();
  init.signal = ctrl.signal;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try { res = await fetch(path, init); }
  catch (e) { throw new ApiError(e.name === "AbortError" ? "timeout" : "network"); }
  finally { clearTimeout(timer); }
  if (!res.ok) {
    let detail = null;
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    throw new ApiError(res.status, detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null);
  }
  return res.json();
}
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }
function add(node) { $("#messages").appendChild(node); scrollDown(); return node; }
function note(text, kind = "", iconName = "check") { return add(el(`<div class="note ${kind}">${icon(iconName)}<span>${esc(text)}</span></div>`)); }
function newTurnButton() {
  const btn = el(`<button type="button" class="btn small">${icon("plus")}${esc(t("newTurn"))}</button>`);
  btn.addEventListener("click", newConversation);
  return btn;
}

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
  $("#clock-now").textContent = when(state.now, true, "es");
  if (!state.session) {
    $("#session-state").textContent = "Sin identificar";
    $("#session-detail").textContent = "El cliente verifica su identidad en el formulario seguro.";
    meter.hidden = true;
    return;
  }
  const left = minutesBetween(state.now, state.session.expires_at);
  if (left <= 0) {
    box.classList.add("expired");
    $("#session-state").textContent = "Sesión vencida";
    $("#session-detail").textContent = "Pasaron los 15 minutos de la sesión. Debe verificar su identidad otra vez.";
    meter.hidden = true;
    return;
  }
  box.classList.add("on");
  $("#session-state").textContent = "Identidad verificada";
  $("#session-detail").textContent = `Sesión ${state.session.session_ref}, vence a las ${hhmm(state.session.expires_at)} (reloj de este turno).`;
  meter.hidden = false;
  $("#session-fill").style.transform = `scaleX(${Math.max(0, Math.min(1, left / SESSION_MIN))})`;
}
async function refreshClock() {
  if (!state.conv) return;
  try {
    const out = await api(`/api/conversations/${state.conv}/clock`);
    state.now = out.now;
    renderSession();
  } catch { /* the clock is informational */ }
}

// ---- conversation ----------------------------------------------------------------------------------------------------
async function newConversation() {
  clearInterval(state.smsTimer);
  let out;
  try { out = await api("/api/conversations", state.conv ? { language: "es", previous_id: state.conv } : { language: "es" }); }
  catch (e) { showError(e); return; }
  Object.assign(state, { conv: out.conversation_id, key: out.conversation_key, label: out.label, language: "es", session: null,
    now: out.now, stages: {}, called: false, turnSeq: 0, busy: false, pendingDoc: null, signedDoc: null, lastDoc: null, lastCandidates: null });
  $("#send").disabled = false;
  $("#composer-hint").hidden = true;
  $("#messages").innerHTML = "";
  $("#trace").innerHTML = '<p class="trace-empty">Cuando el cliente escriba, aquí aparece cada paso del asistente.</p>';
  $("#starters").hidden = false;
  $("#ticket-number").textContent = out.label;
  $("#ticket-time").textContent = when(out.now, true, "es");
  setDisplay("Tu turno", "Atención de reclamos");
  $("#stage-result-name").textContent = "Resultado";
  renderStages(); renderSession(); showSms(null);
  add(el(`<div class="msg bot"><p>${esc(T.es.greeting)}</p><p class="alt">Também atendo em português.</p></div>`));
}

function showHint() { const h = $("#composer-hint"); h.textContent = t("waitHint"); h.hidden = false; }
function canAct() { if (state.busy) { showHint(); return false; } return true; }

async function send(text) {
  text = text.trim();
  if (!text || !canAct()) return null;
  $("#starters").hidden = true;
  document.querySelectorAll(".turn.lit").forEach((n) => n.classList.remove("lit"));  // acting on the reply counts as having seen it
  add(el(`<div class="msg customer"><p>${esc(text)}</p></div>`));
  return runTurn(() => api(`/api/conversations/${state.conv}/messages`, { text }, TURN_TIMEOUT_MS));
}

// Runs one request that ends in an assistant reply. Returns the response, or null when it failed (the error is shown).
async function runTurn(call) {
  const conv = state.conv;
  state.busy = true; $("#send").disabled = true;
  const typing = add(el(`<div class="typing" role="status" aria-label="${esc(t("typing"))}"><i></i><i></i><i></i><span class="typing-slow" hidden>${esc(t("slow"))}</span></div>`));
  const slow = setTimeout(() => { typing.querySelector(".typing-slow").hidden = false; scrollDown(); }, SLOW_MS);
  try {
    const out = await call();
    if (state.conv !== conv) return null;  // a new turn started meanwhile: this reply belongs to the old one
    if (out && !out.skip) {
      if (out.turn && out.turn.language) state.language = out.turn.language;
      if (out.now) state.now = out.now;
      renderReply(out);
    }
    return out;
  } catch (e) {
    if (state.conv === conv) showError(e);
    return null;
  } finally {
    clearTimeout(slow);
    typing.remove();
    if (state.conv === conv) {
      state.busy = false; $("#send").disabled = false; $("#composer-hint").hidden = true;
      $("#input").placeholder = t("placeholder");
      $("#input").focus();
      refreshClock(); refreshConsoleCount();
    }
  }
}

function errorText(e) {
  const code = e.detail && e.detail.code;
  if (e.status === 404) return t("err404");
  if (e.status === 409) return t("errBusy");
  if (e.status === 429 && code === "turn_limit") return t("errTurns")(e.detail.max_turns);
  if (e.status === 429 && code === "too_many_conversations") return t("errNewLimit")(e.detail.retry_after_s || 60);
  if (e.status === 503 && code === "server_busy") return t("errServerBusy");
  if (e.status === 413 || e.status === 422) return t("errTooLong");
  if (e.status === "timeout") return t("errTimeout");
  if (e.status === "network") return t("errNetwork");
  return t("errServer");
}
function showError(e) {
  const n = add(el(`<div class="note warn" role="alert">${icon("clock")}<span>${esc(errorText(e))}</span></div>`));
  if (e.status === 404 || (e.status === 429 && e.detail.code === "turn_limit")) n.appendChild(newTurnButton());
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
    setDisplay("Turno atendido", b.already_existed ? `Reclamo ${b.case_id} ya estaba abierto` : `Reclamo ${b.case_id} abierto`);
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
function enableCandidates() { (state.lastCandidates || []).forEach((x) => { x.disabled = false; x.classList.remove("chosen"); }); }

function renderCandidates(b) {
  const title = b.purpose === "decline_inquiry" ? t("pickDecline") : t("pick");
  const card = el(`<section class="card" aria-label="${esc(title)}">
    <header class="card-head"><h3>${esc(title)}</h3></header>
    <div class="card-body"><p class="card-lede">${esc(t("pickLede"))}</p><ul class="movements"></ul></div></section>`);
  const list = card.querySelector(".movements");
  const buttons = [];
  b.items.forEach((m) => {
    const p = mvParts(m);
    const li = el(`<li><button type="button" class="mv">
      <span class="mv-what">${esc(p.what)}</span>
      <span class="mv-amt">${esc(money(m.amount, m.currency))}</span>
      <span class="mv-meta"><span>${esc(when(m.event_ts || m.event_date))}</span><span>${esc(p.type)}</span><span>${esc(p.product)}</span><span>${esc(p.channel)}</span>${m.is_international ? `<span class="tag-intl">${esc(t("intl"))}</span>` : ""}</span>
    </button></li>`);
    const btn = li.querySelector("button");
    buttons.push(btn);
    btn.addEventListener("click", async () => {
      if (!canAct()) return;
      buttons.forEach((x) => { x.disabled = true; x.classList.remove("chosen"); });
      btn.classList.add("chosen");
      // The bank's own fields and the movement id, never the merchant text: that comes from data, not from the customer.
      const out = await send(t("pickMsg")(p.type, when(m.event_ts || m.event_date, false), money(m.amount, m.currency), m.transaction_id));
      if (!out) buttons.forEach((x) => { x.disabled = false; x.classList.remove("chosen"); });
    });
    list.appendChild(li);
  });
  state.lastCandidates = buttons;
  add(card);
}

function renderConfirm(b) {
  const f = b.facts || {}, p = b.preview || {}, d = b.decision || {};
  const handoff = d.handoff_required === true;
  const why = HANDOFF_WHY[d.handoff_reason] ? tr(HANDOFF_WHY, d.handoff_reason) : t("whyGeneric");
  const title = handoff ? t("confirmTitleHandoff") : t("confirmTitle");
  const what = f.merchant && f.merchant.untrusted_text ? t("movementAt")(tr(TXN, f.transaction_type), f.merchant.untrusted_text) : tr(TXN, f.transaction_type);
  const card = el(`<section class="card${handoff ? " handoff" : ""}" aria-label="${esc(title)}">
    <header class="card-head"><h3>${esc(title)}</h3><code>${esc(b.evidence || "")}</code></header>
    <div class="card-body">
      <p class="card-lede">${esc(handoff ? t("confirmLedeHandoff")(why) : t("confirmLede"))}</p>
      <dl class="facts">
        <dt>${esc(t("amount"))}</dt><dd class="big">${esc(money(f.amount, f.currency))}</dd>
        <dt>${esc(t("movement"))}</dt><dd>${esc(what)}</dd>
        <dt>${esc(t("date"))}</dt><dd>${esc(when(f.event_ts || f.event_date))}</dd>
        <dt>${esc(t("product"))}</dt><dd>${esc(tr(PRODUCT, f.product_type_en))}${f.number_last4 ? esc(t("endingIn")(f.number_last4)) : ""}</dd>
        <dt>${esc(t("channel"))}</dt><dd>${esc(tr(CHANNEL, f.channel) || "—")}${f.is_international ? `, ${esc(t("intl").toLowerCase())}` : ""}</dd>
        <dt>${esc(t("kind"))}</dt><dd>${esc(tr(DISPUTE, p.dispute_type) || p.subcategory || "—")}</dd>
        <dt>${esc(handoff ? t("priorityHandoff") : t("priority"))}</dt><dd><span class="prio ${esc(p.priority || "")}">${esc(tr(PRIO, p.priority) || "—")}</span> ${p.first_response_hours != null ? `${esc(t("firstResponse").toLowerCase())} ${esc(t("inHours")(p.first_response_hours))}` : ""}</dd>
      </dl>
      <div class="actions"><button type="button" class="btn primary" data-a="yes">${icon("check")}${esc(handoff ? t("confirmYesHandoff") : t("confirmYes"))}</button><button type="button" class="btn" data-a="no">${esc(t("confirmNo"))}</button></div>
    </div></section>`);
  card.querySelectorAll("[data-a]").forEach((btn) => btn.addEventListener("click", async () => {
    if (!canAct()) return;
    const yes = btn.dataset.a === "yes";
    const actions = card.querySelector(".actions");
    const resolved = el(`<p class="resolved${yes ? "" : " no"}">${icon(yes ? "check" : "x")}${esc(yes ? t("confirmedState") : t("deniedState"))}</p>`);
    actions.replaceWith(resolved);
    const out = await send(yes ? (handoff ? t("confirmMsgHandoff") : t("confirmMsg")) : t("denyMsg"));
    if (!out) { resolved.replaceWith(actions); return; }
    if (!yes && state.lastCandidates && state.lastCandidates.length) {
      enableCandidates();
      note(t("pickAgain"), "", "doc");
    }
  }));
  add(card);
}

function renderCase(b) {
  const c = b.case || {};
  const existed = b.already_existed === true;
  const title = existed ? t("caseExisting") : t("caseTitle");
  add(el(`<section class="card result" aria-label="${esc(title)}">
    <header class="card-head"><h3>${icon("check")}${esc(title)}</h3><code>${esc(b.evidence || "")}</code></header>
    <div class="card-body">
      ${existed ? `<p class="card-lede">${esc(t("caseExistingLede"))}</p>` : ""}
      <dl class="facts">
        <dt>${esc(t("number"))}</dt><dd class="case-id">${esc(b.case_id)}</dd>
        <dt>${esc(t("kind"))}</dt><dd>${esc(tr(DISPUTE, c.dispute_type) || c.subcategory || "—")}</dd>
        <dt>${esc(t("amount"))}</dt><dd>${esc(money(c.amount, c.currency))}</dd>
        <dt>${esc(t("status"))}</dt><dd>${esc(tr(STATUS, b.status))}</dd>
        <dt>${esc(t("priority"))}</dt><dd><span class="prio ${esc(b.priority || "")}">${esc(tr(PRIO, b.priority))}</span> ${esc(t("firstResponse").toLowerCase())} ${esc(t("inHours")(b.first_response_hours))}</dd>
      </dl>
      <p class="card-lede">${esc(t("verifiedCase"))}</p>
    </div></section>`));
}

function renderTicket(b) {
  add(el(`<section class="card called-card" aria-label="${esc(t("ticketTitle"))}">
    <header class="card-head"><h3>${icon("handoff")}${esc(t("ticketTitle"))}</h3><code>${esc(b.evidence || "")}</code></header>
    <div class="card-body">
      <dl class="facts">
        <dt>${esc(t("ticket"))}</dt><dd class="case-id">${esc(b.ticket_id)}</dd>
        <dt>${esc(t("queue"))}</dt><dd>${esc(tr(QUEUE, b.queue) || "—")}</dd>
        ${b.priority ? `<dt>${esc(t("priority"))}</dt><dd><span class="prio ${esc(b.priority)}">${esc(tr(PRIO, b.priority))}</span></dd>` : ""}
        ${b.first_response_hours != null ? `<dt>${esc(t("firstResponse"))}</dt><dd>${esc(t("inHours")(b.first_response_hours))}</dd>` : ""}
      </dl>
      <p class="card-lede">${esc(t("verifiedTicket"))}</p>
    </div></section>`));
}

function startErrorText(out) {
  if (out.error === "RATE_LIMITED") {
    const min = Math.max(1, Math.ceil((out.retry_after_s || 60) / 60));
    return t("rateLimited")(min) + (state.demo ? " " + t("rateLimitedDemo") : "");
  }
  if (out.error === "VALIDATION_ERROR") return t("badDoc");
  if (out.error === "OTHER_CUSTOMER") return t("otherCustomer");
  if (out.error === "GONE") return t("err404");
  return t("sendFailed");
}

function renderAuth(expired, prefill = state.prefill) {
  document.querySelectorAll(".card.secure").forEach((c) => c.remove());
  const conv = state.conv;
  const card = el(`<section class="card secure" aria-label="${esc(t("authTitle"))}">
    <header class="card-head"><h3>${icon("lock")}${esc(t("authTitle"))}</h3></header>
    <div class="card-body">
      <p class="card-lede">${esc(expired ? t("authExpired") : t("authIntro"))}</p>
      <form class="secure-form" data-step="doc" novalidate>
        <div class="pair">
          <label class="field">${esc(t("docType"))}<select name="type" id="auth-type"><option value="DNI">DNI</option><option value="CC">CC</option><option value="CE">CE</option><option value="Pasaporte">${esc(t("passport"))}</option></select></label>
          <label class="field">${esc(t("docNumber"))}<input name="number" id="auth-number" inputmode="numeric" autocomplete="off" maxlength="20" required></label>
        </div>
        <div class="actions"><button class="btn primary" type="submit">${esc(t("sendCode"))}</button></div>
        <p class="form-error" role="alert" hidden></p>
      </form>
    </div></section>`);
  const form = card.querySelector("form");
  if (prefill) { form.type.value = prefill.document_type; form.number.value = prefill.document_number; }
  const fail = (box, text, withNewTurn = false, withNewCode = false) => {
    box.textContent = text;
    if (withNewTurn) box.appendChild(newTurnButton());
    if (withNewCode) {
      const again = el(`<button type="button" class="btn small">${esc(t("newCode"))}</button>`);
      again.addEventListener("click", () => renderAuth(false, state.lastDoc));
      box.appendChild(again);
    }
    box.hidden = false;
  };
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = form.querySelector(".form-error");
    err.hidden = true;
    if (form.dataset.step === "doc") {
      const doc = { document_type: form.type.value, document_number: form.number.value.trim() };
      if (!doc.document_number) { fail(err, t("needDoc")); form.number.focus(); return; }
      const btn = form.querySelector("button"); btn.disabled = true;
      let out;
      try { out = await api(`/api/conversations/${conv}/auth/start`, doc); }
      catch (e) { out = { ok: false, error: e.status === 404 ? "GONE" : "FAILED" }; }
      btn.disabled = false;
      if (state.conv !== conv) return;
      if (!out.ok) { fail(err, startErrorText(out), out.error === "OTHER_CUSTOMER" || out.error === "GONE"); return; }
      state.lastDoc = doc;
      state.pendingDoc = docKey(doc.document_type, doc.document_number);
      if (out.now) { state.now = out.now; renderSession(); }
      form.dataset.step = "code";
      form.innerHTML = `<p class="card-lede">${esc(t("codeSent"))}</p>
        <label class="field">${esc(t("code"))}<input name="code" id="auth-code" class="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required></label>
        <div class="actions"><button class="btn primary" type="submit">${esc(t("verify"))}</button></div>
        <p class="form-error" role="alert" hidden></p>`;
      form.code.focus();
      if (state.demo) pollSms();
    } else {
      const btn = form.querySelector("button"), e2 = form.querySelector(".form-error");
      if (!/^\d{6}$/.test(form.code.value.trim())) { fail(e2, t("codeDigits")); return; }
      if (!canAct()) return;
      btn.disabled = true;
      const out = await runTurn(async () => {
        const res = await api(`/api/conversations/${conv}/auth/verify`, { code: form.code.value.trim() }, TURN_TIMEOUT_MS);
        if (state.conv !== conv) return { skip: true };
        if (!res.ok) {
          btn.disabled = false;
          if (res.reason === "invalid_code") fail(e2, t("wrongCode")(res.attempts_remaining ?? 0));
          else fail(e2, res.locked ? t("locked") : t("codeExpired"), false, true);
          return { skip: true };
        }
        card.remove(); showSms(null);
        state.session = res.session;
        state.signedDoc = state.pendingDoc;
        setStage("identity", "done");
        if (!state.stages.movement) setStage("movement", "current");
        if (res.now) state.now = res.now;
        renderSession();
        note(t("verifiedNote"));
        return res;
      });
      if (!out) btn.disabled = false;  // the request failed: the code can be sent again
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
  const conv = state.conv;
  let n = 0;
  state.smsTimer = setInterval(async () => {
    if (state.conv !== conv) { clearInterval(state.smsTimer); return; }
    try {
      const out = await api(`/api/conversations/${conv}/phone`);
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
function classifierText(c) {
  if (c.error) return `no respondió (${c.error})`;
  const bits = [String(c.intent || "—").replace(/_/g, " ")];
  if (typeof c.confidence === "number") bits.push(`confianza ${c.confidence.toLocaleString("es", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  if (c.below_threshold) bits.push("bajo el umbral");
  return bits.join(", ");
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
  if (turn.classifier) steps.appendChild(el(`<li class="step"><span class="step-dot"></span><span class="step-name">Clasificador de intención</span><span class="step-ms">${turn.classifier.latency_ms != null ? `${esc(turn.classifier.latency_ms)} ms` : ""}</span><span class="step-detail">${esc(classifierText(turn.classifier))}</span></li>`));
  turn.tools.forEach((tool) => {
    const detail = [tool.error ? `<span class="bad">${esc(ERR[tool.error] || tool.error)}</span>` : "", esc(policyText(tool.policy)), tool.runtime_fallback ? "lo llamó la app, no el modelo" : ""].filter(Boolean).join("; ");
    steps.appendChild(el(`<li class="step${tool.ok ? "" : " err"}"><span class="step-dot"></span>
      <span class="step-name">${esc(TOOL[tool.tool] || tool.tool)}</span><span class="step-ms">${tool.latency_ms} ms</span>
      <span class="step-detail">${detail ? detail + "<br>" : ""}<code>${esc(tool.tool)}</code> <code>${esc(tool.tool_call_id || "")}</code></span></li>`));
  });
  if (!turn.tools.length && !turn.classifier) steps.appendChild(el(`<li class="step"><span class="step-dot"></span><span class="step-name">Solo conversación</span><span class="step-ms"></span><span class="step-detail">El asistente respondió sin consultar datos.</span></li>`));
  if (turn.fallback) node.appendChild(el(`<p class="turn-flag">${esc(fallbackText(turn))}</p>`));
  if (turn.unverified_ids_in_reply && turn.unverified_ids_in_reply.length) node.appendChild(el(`<p class="turn-flag">La respuesta menciona ${esc(turn.unverified_ids_in_reply.join(", "))} sin respaldo de una herramienta.</p>`));
  const clear = () => node.classList.remove("lit");
  node.addEventListener("mouseenter", clear);
  node.addEventListener("focusin", clear);
  node.tabIndex = -1;
  box.prepend(node);
}
function fallbackText(turn) {
  const handed = turn.tools.some((x) => x.runtime_fallback && x.tool === "handoff_to_human" && x.ok);
  if (turn.fallback === "static_fallback") return "Respuesta fija: no se pudo registrar el ticket y el cliente recibió un mensaje para contactar al banco.";
  if (handed) return "Respuesta de respaldo: el modelo falló y la app pasó el caso a un especialista.";
  if (String(turn.fallback).startsWith("max_model_calls:repeated")) return "Respuesta fija: el modelo repitió la misma llamada y la app cortó el turno, sin crear un ticket.";
  return "Respuesta de respaldo: el modelo falló y la app respondió con un texto fijo, sin crear un ticket nuevo.";
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
  box.innerHTML = "";
  if (!state.personas.length) { box.innerHTML = `<li class="sheet-note">Corre <code>python -m app.build_personas</code> para crear los clientes de prueba.</li>`; return; }
  const country = { MX: "México", CO: "Colombia", AR: "Argentina" };
  state.personas.forEach((p) => {
    const li = el(`<li class="persona"><span class="persona-cc" title="${esc(country[p.country_code] || p.country_code)}">${esc(p.country_code)}</span>
      <span class="persona-name">${esc(p.first_name)}${p.customer_status !== "Active" ? `<span class="persona-flag">${p.customer_status === "Suspended" ? "Suspendida" : esc(p.customer_status)}</span>` : ""}</span>
      <span class="persona-story">${esc((p.story || {}).es || "")}</span>
      <span class="persona-doc">${esc(p.document_type)} ${esc(p.document_number)}</span>
      <button type="button" class="btn">Usar</button></li>`);
    li.querySelector("button").addEventListener("click", async () => {
      closeSheet();
      // Another customer never signs in inside a conversation that already has one: start a new turn first.
      if (state.signedDoc && state.signedDoc !== docKey(p.document_type, p.document_number)) await newConversation();
      state.prefill = p;
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
  let out;
  try { out = await api("/api/demo/clock", { conversation_id: state.conv, minutes: 16 }); }
  catch (e) { showError(e); return; }
  state.now = out.now;
  renderSession();
  if (out.added_minutes > 0) note(`Reloj de este turno adelantado ${out.added_minutes} minutos: ahora son las ${hhmm(out.now)}. Los otros turnos no cambian.`, "warn", "clock");
  else note(`Este turno ya llegó al máximo de ${out.max_offset_minutes} minutos de adelanto.`, "warn", "clock");
}

// ---- specialist console ------------------------------------------------------------------------------------------------------
async function loadConsole() {
  let out;
  try { out = await api("/api/console"); }
  catch { $("#calls").innerHTML = '<li><p class="empty">No se pudo leer la consola. Vuelve a intentarlo con Actualizar.</p></li>'; return; }
  const calls = $("#calls"), cases = $("#cases");
  calls.innerHTML = out.tickets.length ? "" : '<li><p class="empty">Todavía nadie fue llamado a un especialista.</p></li>';
  out.tickets.forEach((r) => {
    const fresh = !state.seen.has(r.ticket_id);
    const li = el(`<li><button type="button" class="call${fresh ? " lit" : ""}">
      <span class="call-turn${r.turn_label ? "" : " old"}">${esc(r.turn_label || "Previo")}</span>
      <span class="call-reason">${esc(REASON[r.reason_code] || r.reason_code)}</span>
      <span class="call-time">${esc(when(r.created_at, true, "es"))}</span>
      <span class="call-meta"><span>${esc(tr(QUEUE, r.queue, "es"))}</span><span>${r.identity_verified ? "Identidad verificada" : "Sin identificar"}</span><span>${r.language === "pt" ? "Portugués" : "Español"}</span>${r.priority ? `<span class="prio ${esc(r.priority)}">${esc(tr(PRIO, r.priority, "es"))}</span>` : ""}</span>
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
    <td class="nowrap"><code>${esc(c.case_id)}</code></td><td class="nowrap">${esc(c.turn_label || "Previo")}</td><td>${esc(tr(DISPUTE, c.dispute_type, "es") || c.subcategory)}</td>
    <td class="num">${esc(money(c.amount, c.currency))}</td><td><span class="prio ${esc(c.priority)}">${esc(tr(PRIO, c.priority, "es"))}</span></td><td>${esc(when(c.first_response_due_at, true, "es"))}</td>
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
  const evidence = sv.evidence || [];
  const ev = evidence.map((e) => {
    const pd = e.policy_decision || {};
    const ok = e.outcome === "ok";
    const bits = [ok ? "Correcto" : (ERR[e.error_code] || e.error_code || e.outcome), pd.handoff_reason ? `motivo: ${(REASON[pd.handoff_reason] || pd.handoff_reason).toLowerCase()}` : "", pd.next_action ? `siguiente: ${NEXT[pd.next_action] || pd.next_action}` : ""].filter(Boolean).join("; ");
    return `<li><span class="step-dot" style="background:var(--${ok ? "green" : "red"})"></span><span><strong>${esc(TOOL[e.tool] || e.tool)}</strong>, ${esc(bits)}<br><code>${esc(e.tool)} ${esc(e.tool_call_id || "")}</code></span></li>`;
  });
  const draft = sv.draft ? { ...(sv.draft.case_fields || {}), ...sv.draft } : null;
  if (draft) ev.push(`<li><span class="step-dot"></span><span>Borrador de reclamo adjunto${draft.transaction_id ? ` <code>${esc(draft.transaction_id)}</code>` : ""}${draft.amount != null ? `, ${esc(money(draft.amount, draft.currency))}` : ""}</span></li>`);
  if ((sv.dropped_evidence || []).length) ev.push(`<li><span class="step-dot" style="background:var(--red)"></span><span>${sv.dropped_evidence.length} evidencia(s) descartada(s) porque el servicio no pudo comprobarlas</span></li>`);
  const warnings = [];
  if (!evidence.length) warnings.push("El servicio no recibió evidencia comprobable para esta transferencia: revisa la conversación antes de actuar.");
  if (r.reason_check && r.reason_check !== "consistent") warnings.push(`Motivo: ${(REASON_CHECK[r.reason_check] || r.reason_check).toLowerCase()}.`);
  $("#file").innerHTML = `<div class="file-head">
      <span class="file-turn${r.turn_label ? "" : " old"}">${esc(r.turn_label || "Previo")}</span>
      <div><h2>${esc(REASON[r.reason_code] || r.reason_code)}</h2><p>Ticket <code>${esc(r.ticket_id)}</code>, cola ${esc(tr(QUEUE, r.queue, "es").toLowerCase())}</p></div>
    </div>
    <dl class="file-facts">
      <dt>Cliente</dt><dd>${r.identity_verified ? `Identidad verificada, referencia <code>${esc(r.customer_ref)}</code>` : "Sin identificar"}</dd>
      <dt>Idioma</dt><dd>${r.language === "pt" ? "Portugués" : "Español"}</dd>
      ${r.priority ? `<dt>Prioridad</dt><dd><span class="prio ${esc(r.priority)}">${esc(tr(PRIO, r.priority, "es"))}</span>${r.first_response_hours != null ? `, primera respuesta en ${esc(r.first_response_hours)} h` : ""}</dd>` : ""}
      <dt>Motivo</dt><dd>${esc(REASON_CHECK[r.reason_check] || r.reason_check || "—")}</dd>
      <dt>Recibido</dt><dd>${esc(when(r.created_at, true, "es"))}</dd>
    </dl>
    <section class="file-group verified" aria-labelledby="file-verified">
      <h3 id="file-verified">${icon("check")}Comprobado por el servicio</h3>
      ${warnings.map((w) => `<p class="file-warn">${esc(w)}</p>`).join("")}
      ${ev.length ? `<ul class="evidence">${ev.join("")}</ul>` : '<p class="none">Nada registrado.</p>'}
      <details class="raw"><summary>Registro completo del servicio</summary><pre>${esc(JSON.stringify(sv, null, 2))}</pre></details>
    </section>
    <section class="file-group claimed" aria-labelledby="file-claimed">
      <h3 id="file-claimed">Según el asistente (sin verificar)</h3>
      <p class="file-note">Lo escribió el modelo para ti. El servicio no lo comprobó: contrástalo con lo de arriba antes de actuar.</p>
      <h4>Lo que necesita el cliente</h4><p>${esc(r.request_summary || pkg.request_summary || "—")}</p>
      <h4>Hechos que reporta</h4>${items(pkg.verified_facts)}
      <h4>Lo que hizo</h4>${items(pkg.actions_taken)}
      <h4>Para revisar</h4>${items(pkg.open_questions)}
    </section>`;
}
function setCount(n) { const c = $("#console-count"); c.hidden = !n; c.textContent = n; }
async function refreshConsoleCount() {
  if (!state.console) return;
  try { const out = await api("/api/console/count"); setCount(out.ticket_ids.filter((id) => !state.seen.has(id)).length); } catch { /* informational */ }
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
  let cfg;
  try { cfg = await api("/api/config"); }
  catch (e) { showError(e); return; }
  state.personas = cfg.personas || [];
  state.demo = cfg.demo_controls !== false;
  state.console = cfg.console !== false;
  $("#nav-console").hidden = !state.console;
  state.limits = cfg.limits || {};
  $("#model-name").textContent = (cfg.model || "—").replace("databricks-", "");
  $("#btn-personas").hidden = !state.demo;
  $("#btn-clock").hidden = !state.demo;
  $("#classifier-status").textContent = cfg.classifier
    ? `Clasificador de intención: ${cfg.classifier}. Su resultado es una pista para el asistente, no una decisión.`
    : "Clasificador de intención: no cargado (se entrena con python -m src.classifier.train).";
  const input = $("#input");
  if (state.limits.max_text) input.maxLength = state.limits.max_text;
  renderPersonas();
  await newConversation();
  $("#composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = input.value;
    if (!v.trim()) return;
    if (!canAct()) return;  // busy: the text stays in the box
    input.value = ""; input.style.height = "";
    const out = await send(v);
    if (!out && !input.value) input.value = v.trim();  // failed: give the text back so it can be sent again
  });
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
  setInterval(refreshClock, 30000);  // the demo clock follows wall time, so the session meter moves on its own
  refreshConsoleCount();
}
boot();
