// Belastingdienst KennisAssistent — S11 polish pass.
// No build step. Single file.

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  sessionId: `s-${Math.random().toString(36).slice(2, 10)}`,
  tier: "PUBLIC",
  view: null,
  chunks: new Map(),           // chunk_id -> record
  recentTurns: [],             // chat turns w/ trace, for CRAG workspace
  lastDocuments: [],
};

const VIEW_LABELS = {
  chat: "Gesprek", ingest: "Ingestie", retrieval: "Retrieval", crag: "CRAG-pipeline",
  security: "Toegang", eval: "Kwaliteit", documents: "Documenten",
};

// ═══════════════════════════════════════════════════════════════
//  UNIVERSAL PROMPT TRAY (5 prompts exercise all 4 modules)
// ═══════════════════════════════════════════════════════════════
const DEMO_PROMPTS = [
  { query: "Wat is de arbeidskorting in 2024?" },
  { query: "ECLI:NL:HR:2021:1523" },
  { query: "Hoe werkt de hypotheekrenteaftrek en wat zijn de recente wijzigingen?" },
  { query: "Wat zijn de FIOD opsporingsmethoden?" },
  { query: "Wanneer mag ik geen huishoudelijke uitgaven aftrekken?" },
];

const TIER_LABELS = {
  PUBLIC: "Publiek",
  INTERNAL: "Juridisch medewerker",
  RESTRICTED: "Inspecteur",
  CLASSIFIED_FIOD: "FIOD-rechercheur",
};
const TIER_ACCESS = {
  PUBLIC:          ["PUBLIC"],
  INTERNAL:        ["PUBLIC", "INTERNAL"],
  RESTRICTED:      ["PUBLIC", "INTERNAL", "RESTRICTED"],
  CLASSIFIED_FIOD: ["PUBLIC", "INTERNAL", "RESTRICTED", "CLASSIFIED_FIOD"],
};

const STAGE_ORDER = ["api_reachable", "loading_embedder", "pinging_ollama", "opensearch_setup", "connecting_redis", "ready"];

// ═══════════════════════════════════════════════════════════════
//  SHARED STATE HELPERS (empty / loading / error)
// ═══════════════════════════════════════════════════════════════
function renderEmpty(host, { icon = "✨", title, hint, cta, onCta } = {}) {
  host.innerHTML = "";
  const box = document.createElement("div");
  box.className = "state-box";
  box.innerHTML = `
    <div class="state-icon" aria-hidden="true">${esc(icon)}</div>
    <div class="state-title">${esc(title || "Nog niets te zien")}</div>
    <div class="state-hint">${esc(hint || "")}</div>`;
  if (cta) {
    const btn = document.createElement("button");
    btn.className = "state-cta";
    btn.textContent = cta;
    btn.addEventListener("click", () => onCta?.());
    box.appendChild(btn);
  }
  host.appendChild(box);
}
function renderLoading(host, { rows = 3, text } = {}) {
  host.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.setAttribute("role", "status");
  wrap.setAttribute("aria-live", "polite");
  if (text) {
    const t = document.createElement("div");
    t.className = "text-xs text-slate-400 mb-2";
    t.textContent = text;
    wrap.appendChild(t);
  }
  for (let i = 0; i < rows; i++) {
    const r = document.createElement("div");
    r.className = "skeleton skel-row";
    r.style.width = `${80 - i * 10}%`;
    wrap.appendChild(r);
  }
  host.appendChild(wrap);
}
function renderError(host, { err, onRetry } = {}) {
  host.innerHTML = "";
  const box = document.createElement("div");
  box.className = "state-box err";
  box.innerHTML = `
    <div class="state-icon" aria-hidden="true">⚠</div>
    <div class="state-title">Iets ging mis</div>
    <div class="state-hint">${esc(String(err?.message || err || "Onbekende fout"))}</div>`;
  if (onRetry) {
    const btn = document.createElement("button");
    btn.className = "state-cta";
    btn.textContent = "Opnieuw proberen";
    btn.addEventListener("click", () => onRetry());
    box.appendChild(btn);
  }
  host.appendChild(box);
}

// ═══════════════════════════════════════════════════════════════
//  WARMUP SPLASH
// ═══════════════════════════════════════════════════════════════
function paintChecklist(currentStage, complete = false) {
  const idx = STAGE_ORDER.indexOf(currentStage);
  STAGE_ORDER.forEach((s, i) => {
    const li = $(`#splash-checklist li[data-stage="${s}"]`);
    if (!li) return;
    const cb = li.querySelector(".cb");
    li.classList.remove("active", "done");
    if (complete) { li.classList.add("done"); cb.textContent = "✓"; return; }
    if (idx < 0) { cb.textContent = "○"; return; }
    if (i < idx)       { li.classList.add("done");   cb.textContent = "✓"; }
    else if (i === idx){ li.classList.add("active"); cb.textContent = ""; }
    else               { cb.textContent = "○"; }
  });
}

function showSplash(splash, app) { splash.hidden = false; app.hidden = true; }
function hideSplash(splash, app) { splash.hidden = true;  app.hidden = false; }

async function waitForWarmup() {
  const splash = $("#warmup-splash");
  const app    = $("#app");
  const detail = $("#warmup-detail");
  const elapsedEl = $("#splash-elapsed");
  const modelEl   = $("#splash-model");
  const skipBtn   = $("#splash-skip");
  showSplash(splash, app);
  paintChecklist("api_reachable");

  const t0 = Date.now();
  const tick = setInterval(() => { elapsedEl.textContent = Math.round((Date.now() - t0) / 1000) + "s"; }, 250);
  setTimeout(() => { skipBtn.hidden = false; }, 4000); // show sooner than before
  skipBtn.addEventListener("click", () => {
    clearInterval(tick);
    hideSplash(splash, app);
    if (!state._appBooted) { state._appBooted = true; onAppReady(); }
  });

  let consecutiveErrors = 0;
  for (let i = 0; i < 300; i++) {
    try {
      const r = await fetch("/health", { cache: "no-store" });
      if (r.ok) {
        consecutiveErrors = 0;
        const j = await r.json();
        detail.textContent = readableStage(j.warmup_stage);
        paintChecklist(j.warmup_stage, j.warmup_complete);
        if (modelEl.textContent === "—" && i % 3 === 0) {
          fetch("/health/detailed", { cache: "no-store" })
            .then(r => r.ok ? r.json() : null)
            .then(d => { if (d?.config?.llm_model) modelEl.textContent = d.config.llm_model; })
            .catch(() => {});
        }
        if (j.warmup_complete) {
          clearInterval(tick);
          hideSplash(splash, app);
          if (!state._appBooted) { state._appBooted = true; onAppReady(); }
          return;
        }
      } else {
        consecutiveErrors++;
      }
    } catch (e) {
      consecutiveErrors++;
      detail.textContent = "Wachten op API…";
    }
    if (consecutiveErrors >= 3) {
      detail.textContent = "API niet bereikbaar — check `docker compose logs -f api`.";
      skipBtn.hidden = false;
    }
    await sleep(1000);
  }
  detail.textContent = "Warmup duurt te lang — klik 'App nu openen'.";
  skipBtn.hidden = false;
}

function readableStage(stage) {
  return ({
    api_reachable:   "API bereikbaar",
    starting:        "Diensten opstarten",
    loading_embedder:"Embedding-model laden (e5-small)",
    pinging_ollama:  "Ollama verbinding verifiëren",
    opensearch_setup:"Zoekindex voorbereiden",
    connecting_redis:"Cache verbinden",
    ready:           "Pipeline gereed",
  })[stage] || "Opstarten…";
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc   = s => s == null ? "" : String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Minimal Markdown → HTML renderer for assistant answers.
// Supports: H2/H3, bullets, numbered lists, GitHub-style tables, **bold**, `code`,
// citation refs `[N]` → <sup class="cite-ref" data-cite-idx="N">. No raw HTML, no
// images, no code blocks — input is escaped first, then enriched.
function renderMarkdown(text) {
  if (!text) return "";
  let s = String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // Tables: 2+ pipe-rows, second must be the separator (---|---|---).
  s = s.replace(/(?:^\|[^\n]+\|[ \t]*\n){2,}(?:^\|[^\n]+\|[ \t]*\n?)*/gm, (block) => {
    const lines = block.trim().split("\n");
    const rows = lines.map(r =>
      r.replace(/^\|[ \t]*|[ \t]*\|$/g, "").split(/[ \t]*\|[ \t]*/)
    );
    if (rows.length < 2 || !rows[1].every(c => /^:?-{3,}:?$/.test(c.trim()))) return block;
    const head = rows[0], body = rows.slice(2);
    let out = `<table class="md-table"><thead><tr>`;
    head.forEach(h => out += `<th>${h}</th>`);
    out += `</tr></thead><tbody>`;
    body.forEach(r => {
      out += "<tr>";
      r.forEach(c => out += `<td>${c}</td>`);
      out += "</tr>";
    });
    return out + "</tbody></table>";
  });

  // Headings (H2/H3 only — model is constrained to those).
  s = s.replace(/^###\s+(.+)$/gm, "<h4 class=\"md-h\">$1</h4>");
  s = s.replace(/^##\s+(.+)$/gm,  "<h3 class=\"md-h\">$1</h3>");

  // Bullet lists (consecutive `- ` or `* ` lines).
  s = s.replace(/(?:^[-*][ \t]+[^\n]+\n?)+/gm, (m) => {
    const items = m.trim().split("\n").map(l => l.replace(/^[-*][ \t]+/, ""));
    return "<ul class=\"md-list\">" + items.map(i => `<li>${i}</li>`).join("") + "</ul>";
  });
  // Numbered lists.
  s = s.replace(/(?:^\d+\.[ \t]+[^\n]+\n?)+/gm, (m) => {
    const items = m.trim().split("\n").map(l => l.replace(/^\d+\.[ \t]+/, ""));
    return "<ol class=\"md-list\">" + items.map(i => `<li>${i}</li>`).join("") + "</ol>";
  });

  // Inline: bold, code, citation refs.
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\[(\d+)\]/g, '<sup class="cite-ref" data-cite-idx="$1">$1</sup>');

  // Paragraph wrap: split on blank lines; leave block elements untouched.
  s = s.split(/\n{2,}/).map(p => {
    p = p.trim();
    if (!p) return "";
    if (/^<(h[1-6]|ul|ol|table|pre|blockquote)/i.test(p)) return p;
    return `<p>${p.replace(/\n/g, "<br>")}</p>`;
  }).join("\n");

  return s;
}

// Delegated click: a citation `[N]` in a rendered answer scrolls to and triggers the
// matching pill in that bubble's citation row.
document.addEventListener("click", (e) => {
  const ref = e.target.closest?.(".cite-ref");
  if (!ref) return;
  const bubble = ref.closest(".msg.assistant");
  if (!bubble) return;
  const idx = parseInt(ref.dataset.citeIdx, 10) - 1;
  const order = bubble._refOrder || [];
  const cid = order[idx];
  if (!cid) return;
  const pill = bubble.querySelector(`.citation-pill[data-cid="${CSS.escape(cid)}"]`);
  if (pill) {
    pill.scrollIntoView({ behavior: "smooth", block: "nearest" });
    pill.classList.add("flash");
    setTimeout(() => pill.classList.remove("flash"), 900);
    pill.click();
  }
});

// ═══════════════════════════════════════════════════════════════
//  NAVIGATION + TIER BANNER
// ═══════════════════════════════════════════════════════════════
function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  return Object.prototype.hasOwnProperty.call(VIEW_LABELS, h) ? h : null;
}

function setView(view, { silent = false, fromKeyboard = false } = {}) {
  if (state.view === view && state.view !== null) return;
  if (!silent) {
    const depth = (history.state?.depth ?? 0) + 1;
    history.pushState({ view, depth }, "", "#" + view);
  }
  state.view = view;
  $$(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $$(".workspace").forEach(w => w.classList.toggle("hidden", w.dataset.workspace !== view));
  refreshNavControls();
  paintTierBanners();
  if (view === "documents")   loadDocuments();
  if (view === "ingest")      { loadTreeDocs(); renderQuantWidget(); }
  if (view === "crag")        renderCragState();
  if (view === "security")    { renderTierChips(); loadCacheEntries(); renderAuditTable(); }
  if (view === "eval")        loadEval();
  if (fromKeyboard && view !== "chat") {
    setTimeout(() => {
      const target = $(`section[data-workspace="${view}"] input:not([type=hidden]), section[data-workspace="${view}"] button:not([disabled])`);
      target?.focus({ preventScroll: true });
    }, 0);
  }
}

function setTier(tier) {
  state.tier = tier;
  $$(".role-btn").forEach(r => r.classList.toggle("active", r.dataset.tier === tier));
  paintTierBanners();
  renderSuggestedPrompts();
  if (state.view === "security") { renderTierChips(); loadCacheEntries($("#cache-probe")?.value?.trim() || null); }
}

function paintTierBanners() { /* no-op — tier visible via active role-btn */ }
function ensureTierBanners() { /* no-op */ }

function chatHasMessages() {
  return !!document.querySelector("#messages .msg");
}

function refreshNavControls() {
  const back = $("#nav-back"), home = $("#nav-home"), label = $("#nav-view-label");
  if (label) label.textContent = VIEW_LABELS[state.view] || state.view;
  if (home)  home.disabled = state.view === "chat" && !chatHasMessages();
  if (!back) return;
  const canPopView = (history.state?.depth ?? 0) > 0;
  const canClearChat = state.view === "chat" && chatHasMessages();
  back.disabled = !(canPopView || canClearChat);
  back.textContent = (!canPopView && canClearChat) ? "← Nieuw gesprek" : "← Terug";
}

function clearChat() {
  const list = $("#messages"); if (list) list.innerHTML = "";
  const empty = $("#empty-state"); if (empty) empty.style.display = "";
  state.sessionId = `s-${Math.random().toString(36).slice(2, 10)}`;
  refreshNavControls();
}

function wireNav() {
  $$(".nav-btn").forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
  $$(".role-btn").forEach(b => b.addEventListener("click", () => setTier(b.dataset.tier)));
  $("#nav-home")?.addEventListener("click", () => { setView("chat"); clearChat(); });
  $("#nav-back")?.addEventListener("click", () => {
    if ((history.state?.depth ?? 0) > 0) { history.back(); return; }
    if (state.view === "chat" && chatHasMessages()) clearChat();
  });
  window.addEventListener("popstate", () => {
    const v = parseHash() || "chat";
    setView(v, { silent: true });
  });
  setTier("PUBLIC");
}

// ═══════════════════════════════════════════════════════════════
//  CHAT
// ═══════════════════════════════════════════════════════════════
function renderSuggestedPrompts() {
  const c = $("#suggested-prompts");
  c.innerHTML = "";
  c.setAttribute("role", "list");
  c.setAttribute("aria-label", "Voorbeeldvragen");
  for (const p of DEMO_PROMPTS) {
    const btn = document.createElement("button");
    btn.className = "prompt-card";
    btn.setAttribute("role", "listitem");
    btn.setAttribute("aria-label", `Voorbeeld: ${p.query}`);
    btn.innerHTML = `<span class="pc-label"><div>${esc(p.query)}</div></span>`;
    btn.addEventListener("click", () => {
      $("#input").value = p.query;
      $("#composer").dispatchEvent(new Event("submit", { cancelable: true }));
    });
    c.appendChild(btn);
  }
}

function wireChat() {
  const composer = $("#composer");
  const input    = $("#input");
  const send     = $("#send");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(160, input.scrollHeight) + "px";
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); composer.requestSubmit(); }
  });
  composer.addEventListener("submit", async e => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    input.style.height = "auto";
    $("#empty-state").style.display = "none";
    send.disabled = true;
    addUserMsg(q);
    const asst = addAsstPlaceholder();
    try { await streamChat(q, asst); }
    catch (err) { toast("Er ging iets mis: " + err.message, "err"); console.error(err); }
    finally { send.disabled = false; input.focus(); }
  });
}

function addUserMsg(text) {
  const list = $("#messages");
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="msg-avatar">JIJ</div><div class="msg-body"><div class="msg-role">Jij</div><div class="msg-content"></div></div>`;
  el.querySelector(".msg-content").textContent = text;
  list.appendChild(el);
  scrollChat();
  refreshNavControls();
}

function addAsstPlaceholder() {
  const list = $("#messages");
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `
    <div class="msg-avatar">BD</div>
    <div class="msg-body">
      <div class="msg-role">KennisAssistent</div>
      <div class="ttft-badge" hidden></div>
      <div class="progress-strip" role="status" aria-live="polite">
        <span class="ps-spinner" aria-hidden="true"></span>
        <span class="ps-label">Voorbereiden…</span>
        <span class="ps-elapsed">0s</span>
      </div>
      <div class="progress-steps"></div>
      <div class="msg-content" hidden><span class="cursor"></span></div>
      <div class="parent-badge-slot"></div>
      <div class="citations-label" hidden>Bronnen</div>
      <div class="citations"></div>
      <button class="trace-toggle" hidden aria-expanded="false">▸ Pipeline details tonen</button>
      <div class="trace-panel"></div>
      <button class="open-in-crag text-xs text-bd-navy hover:text-bd-orange mt-2 underline" hidden>▸ Open trace in CRAG-workspace</button>
    </div>`;
  list.appendChild(el);
  scrollChat();
  const toggle = el.querySelector(".trace-toggle");
  const panel  = el.querySelector(".trace-panel");
  toggle.addEventListener("click", () => {
    const open = panel.classList.toggle("open");
    toggle.textContent = open ? "▾ Pipeline details verbergen" : "▸ Pipeline details tonen";
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  const openCrag = el.querySelector(".open-in-crag");
  openCrag.addEventListener("click", () => { state._cragFocusTurn = asst.turnId; setView("crag"); });

  const asst = {
    root: el, content: el.querySelector(".msg-content"),
    citationsLabel: el.querySelector(".citations-label"),
    citations:   el.querySelector(".citations"),
    parentSlot:  el.querySelector(".parent-badge-slot"),
    trace: panel, traceToggle: toggle, openCrag,
    ttftBadge: el.querySelector(".ttft-badge"),
    // progress UI
    progressStrip: el.querySelector(".progress-strip"),
    progressLabel: el.querySelector(".ps-label"),
    progressElapsed: el.querySelector(".ps-elapsed"),
    progressSteps: el.querySelector(".progress-steps"),
    progressStartedAt: Date.now(),
    progressTick: null,
    progressVisibleSteps: [],
    turnId: Math.random().toString(36).slice(2, 10),
    citedIds: new Set(), relevantIds: new Set(), retrievedIds: new Set(),
    parentIds: new Set(), parentContext: new Map(),
    traceEvents: [], gradingResult: null,
    tokenCount: 0, errorShown: false,
  };
  // tick elapsed timer while waiting
  asst.progressTick = setInterval(() => {
    const sec = Math.round((Date.now() - asst.progressStartedAt) / 1000);
    asst.progressElapsed.textContent = sec + "s";
    // Show cold-model hint after 8s
    if (sec === 8 && !asst._hintShown) {
      asst._hintShown = true;
      const hint = document.createElement("div");
      hint.className = "ps-hint";
      hint.textContent = "Eerste query is traag op CPU (~1-2 min); daarna <200 ms via semantic cache.";
      asst.progressStrip.after(hint);
    }
  }, 500);
  return asst;
}

// Human-readable labels for each CRAG node (used in progress strip).
const NODE_LABELS = {
  cache_lookup:      { icon: "💾", label: "Cache doorzoeken"         },
  classify_query:    { icon: "🧭", label: "Vraag classificeren"      },
  memory_resolve:    { icon: "🧠", label: "Conversatie-geheugen"     },
  decompose:         { icon: "🪓", label: "Vraag splitsen"            },
  hyde:              { icon: "🎭", label: "HyDE hypothese-passage"   },
  retrieve:          { icon: "📚", label: "Retrieval (BM25 + kNN)"   },
  grade_context:     { icon: "⚖️", label: "Chunks beoordelen (grader)"},
  rewrite_and_retry: { icon: "✍️", label: "Query herschrijven"        },
  parent_expansion:  { icon: "🌳", label: "Hiërarchische context"    },
  generate:          { icon: "💬", label: "Antwoord genereren"        },
  validate_output:   { icon: "✅", label: "Citaties valideren"         },
  respond:           { icon: "🎉", label: "Klaar"                      },
  refuse:            { icon: "⛔", label: "Weigeren (refuse-pad)"       },
};

function updateProgressStrip(asst, node, result) {
  const info = NODE_LABELS[node] || { icon: "⚙️", label: node };
  asst.progressLabel.innerHTML = `${info.icon} <strong>${info.label}</strong>${result ? ` <span class="text-slate-400">→ ${esc(result)}</span>` : ""}`;
  // Append a visible step bullet so user sees history
  if (!asst.progressVisibleSteps.includes(node)) {
    asst.progressVisibleSteps.push(node);
    const bullet = document.createElement("span");
    bullet.className = "ps-step";
    bullet.innerHTML = `<span class="ps-check">✓</span> ${info.icon} ${esc(info.label)}`;
    asst.progressSteps.appendChild(bullet);
  }
}

function finishProgress(asst) {
  if (asst.progressTick) { clearInterval(asst.progressTick); asst.progressTick = null; }
  if (asst.progressStrip) asst.progressStrip.classList.add("done");
}

function appendToken(asst, token) {
  if (token) asst.tokenCount++;
  // First token: reveal the content box and finish the progress strip
  if (asst.content.hidden) {
    asst.content.hidden = false;
    finishProgress(asst);
    updateProgressStrip(asst, "generate", "streaming");
    // hide strip entirely once tokens start (keep details button)
    asst.progressStrip.style.display = "none";
    if (asst.progressSteps.firstChild) asst.progressSteps.style.display = "none";
    const hint = asst.root.querySelector(".ps-hint");
    if (hint) hint.remove();
  }
  const cur = asst.content.querySelector(".cursor");
  if (cur) cur.remove();
  asst.content.appendChild(document.createTextNode(token));
  const c = document.createElement("span"); c.className = "cursor";
  asst.content.appendChild(c);
  scrollChat();
}
function showInlineError(asst, msg) {
  if (asst.content.hidden) asst.content.hidden = false;
  const cur = asst.content.querySelector(".cursor");
  if (cur) cur.remove();
  const err = document.createElement("div");
  err.className = "inline-error";
  err.textContent = msg;
  asst.content.appendChild(err);
}

// Server emits `text_replace` after streaming completes with the citation-compacted
// answer. We swap the streamed plain text for a markdown-rendered version.
function replaceWithMarkdown(asst, data) {
  const text = typeof data === "string" ? data : (data?.text || "");
  const refOrder = (data && Array.isArray(data.ref_order)) ? data.ref_order : [];
  asst.root._refOrder = refOrder;
  asst.refOrder = refOrder;
  if (asst.content.hidden) asst.content.hidden = false;
  asst.content.innerHTML = renderMarkdown(text);
  asst.content.classList.add("md-rendered");
  scrollChat();
}

function finalizeAsst(asst) {
  const cur = asst.content.querySelector(".cursor");
  if (cur) cur.remove();
  if (asst.tokenCount === 0 && !asst.errorShown) {
    showInlineError(asst, "Geen antwoord ontvangen — controleer serverlogs of probeer opnieuw.");
  }
  if (asst.content.hidden) asst.content.hidden = false; // e.g. refuse path with no tokens
  finishProgress(asst);
  // Collapse the progress strip/steps after completion
  if (asst.progressStrip) asst.progressStrip.style.display = "none";
  if (asst.progressSteps) asst.progressSteps.style.display = "none";
  const hint = asst.root.querySelector(".ps-hint");
  if (hint) hint.remove();
  asst.openCrag.hidden = false;
  state.recentTurns.unshift({ id: asst.turnId, query: asst.query, trace: asst.traceEvents, gradingResult: asst.gradingResult });
  state.recentTurns = state.recentTurns.slice(0, 10);
}
function addTrace(asst, evt) {
  asst.traceToggle.hidden = false;
  asst.traceEvents.push(evt);
  if (evt.node === "grade_context") asst.gradingResult = evt.result;
  // Drive the visible progress strip
  updateProgressStrip(asst, evt.node, evt.result);
  const line = document.createElement("div");
  line.className = "trace-line";
  const dur = evt.duration_ms ? ` <span class="trace-result">${Math.round(evt.duration_ms)}ms</span>` : "";
  line.innerHTML = `<span class="trace-node">${esc(evt.node)}</span> → ${esc(evt.result || "")}${dur}${evt.detail ? ` <span class="text-slate-400">— ${esc(evt.detail)}</span>` : ""}`;
  asst.trace.appendChild(line);
}
function addCitation(asst, cit) {
  asst.citationsLabel.hidden = false;
  // Prepend `[N]` so the pill row mirrors the inline refs in the answer.
  const order = asst.root._refOrder || [];
  const idx = order.indexOf(cit.chunk_id);
  const num = idx >= 0 ? idx + 1 : asst.citations.children.length + 1;
  const el = document.createElement("button");
  el.type = "button";
  el.className = "citation-pill";
  el.dataset.cid = cit.chunk_id;
  el.innerHTML = `<span class="cite-num">[${num}]</span> ${esc(cit.hierarchy_path || cit.chunk_id)}`;
  el.title = cit.chunk_id;
  el.setAttribute("aria-label", `Open metadata voor ${cit.chunk_id}`);
  el.addEventListener("click", () => openMetaModal(cit.chunk_id, el));
  asst.citations.appendChild(el);
}
function renderParentBadge(asst) {
  if (!asst.parentIds.size) return;
  const slot = asst.parentSlot;
  slot.innerHTML = "";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "parent-badge";
  btn.setAttribute("aria-expanded", "false");
  btn.textContent = `Hiërarchische context: +${asst.parentIds.size} bovenliggende leden`;
  const details = document.createElement("div");
  details.style.display = "none";
  details.className = "mt-2 p-3 rounded bg-bd-ink text-xs";
  for (const pid of asst.parentIds) {
    const t = asst.parentContext.get(pid) || state.chunks.get(pid)?.chunk_text || "(geen inhoud)";
    const b = document.createElement("div");
    b.className = "mb-2";
    b.innerHTML = `<div class="font-mono text-[10px] text-slate-500 mb-1">${esc(pid)}</div><div class="text-slate-300">${esc(t.slice(0, 280))}${t.length > 280 ? "…" : ""}</div>`;
    details.appendChild(b);
  }
  btn.addEventListener("click", () => {
    const open = details.style.display === "none";
    details.style.display = open ? "block" : "none";
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  slot.appendChild(btn); slot.appendChild(details);
}

function renderTtftBadge(asst, data) {
  if (!asst.ttftBadge) return;
  const ms = Number(data?.ms ?? 0);
  const source = data?.source || "live";
  const cls = ms <= 500 ? "good" : ms <= 1500 ? "warn" : "bad";
  const sourceLabel = source === "cache" ? "via cache" : source === "refuse" ? "via refuse" : "live generatie";
  const check = ms <= 1500 ? "✓" : "⚠";
  asst.ttftBadge.className = `ttft-badge ${cls}`;
  asst.ttftBadge.innerHTML = `${check} TTFT <strong>${ms.toFixed(0)} ms</strong> · drempel 1500 ms · ${esc(sourceLabel)}`;
  asst.ttftBadge.hidden = false;
}

function scrollChat() { const s = $("#chat-scroll"); s.scrollTop = s.scrollHeight; }

async function streamChat(query, asst) {
  asst.query = query;
  const resp = await fetch("/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ query, security_tier: state.tier, session_id: state.sessionId }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
      let ev = "message", line = "";
      for (const l of block.split("\n")) {
        if (l.startsWith("event:")) ev = l.slice(6).trim();
        else if (l.startsWith("data:")) line += l.slice(5).trim();
      }
      if (!line) continue;
      let data; try { data = JSON.parse(line); } catch { data = line; }
      handleChatEvent(ev, data, asst);
    }
  }
  finalizeAsst(asst);
}

function handleChatEvent(ev, data, asst) {
  switch (ev) {
    case "trace":
      // M8: refuse-frame — visually flag the bubble before the tokens stream in
      if (data?.node === "refuse") asst.root.classList.add("msg-refuse");
      addTrace(asst, data);
      break;
    case "ttft":     renderTtftBadge(asst, data); break;
    case "token":    appendToken(asst, typeof data === "string" ? data : ""); break;
    case "text_replace": replaceWithMarkdown(asst, data); break;
    case "citation":
      addCitation(asst, data);
      asst.citedIds.add(data.chunk_id);
      paintTreeNode(data.chunk_id, "cited");
      break;
    case "chunk":
      if (data.status === "retrieved") { asst.retrievedIds.add(data.chunk_id); paintTreeNode(data.chunk_id, "retrieved"); }
      else if (data.status === "relevant") { asst.relevantIds.add(data.chunk_id); paintTreeNode(data.chunk_id, "relevant"); }
      else if (data.status === "cited")    { asst.citedIds.add(data.chunk_id);    paintTreeNode(data.chunk_id, "cited"); }
      else if (data.status === "parent_expanded") {
        asst.parentIds.add(data.chunk_id);
        if (data.chunk_text) asst.parentContext.set(data.chunk_id, data.chunk_text);
        paintTreeNode(data.chunk_id, "parent-expanded");
        renderParentBadge(asst);
      }
      break;
    case "done":    finalizeAsst(asst); break;
    case "error":   asst.errorShown = true; showInlineError(asst, data?.detail || "Fout bij generatie"); toast(data?.detail || "Fout bij generatie", "err"); break;
  }
}

// ═══════════════════════════════════════════════════════════════
//  TOASTS + META MODAL (with focus trap)
// ═══════════════════════════════════════════════════════════════
function toast(msg, kind = "") {
  const host = $("#toast-container");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.innerHTML = `<span class="flex-1">${esc(msg)}</span>`;
  const x = document.createElement("button");
  x.className = "toast-close"; x.textContent = "✕";
  x.setAttribute("aria-label", "Melding sluiten");
  x.addEventListener("click", () => el.remove());
  el.appendChild(x);
  host.appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

let _lastFocus = null;

async function openMetaModal(chunkId, triggerEl) {
  _lastFocus = triggerEl || document.activeElement;
  let record = state.chunks.get(chunkId);
  if (!record || !record.chunk_text) {
    try {
      const r = await fetch(`/v1/chunks/${encodeURIComponent(chunkId)}`);
      if (r.ok) record = await r.json();
    } catch {}
  }
  if (!record) { toast("Metadata niet gevonden.", "err"); return; }

  const hl = new Set(["chunk_id", "parent_chunk_id", "hierarchy_path", "doc_id"]);
  const jsonText = JSON.stringify(record, null, 2);
  const rendered = jsonText.split("\n").map(line => {
    const m = line.match(/^(\s*)"([^"]+)":(.*)$/);
    if (m && hl.has(m[2])) return `${m[1]}<span class="hl">"${m[2]}"</span>:${esc(m[3])}`;
    return esc(line);
  }).join("\n");

  const pre = $("#meta-json");
  pre.innerHTML = rendered;
  pre.classList.add("meta-json");
  pre.dataset.raw = jsonText;

  const crumb = $("#meta-breadcrumb");
  crumb.innerHTML = (record.hierarchy_path || "").split(" > ").map(p => `<span class="crumb">${esc(p)}</span>`).join('<span class="sep">›</span>');
  crumb.classList.add("meta-breadcrumb");

  const modal = $("#meta-modal");
  modal.classList.remove("hidden");
  modal.classList.add("flex");
  // focus trap
  const close = modal.querySelector("[data-meta-close]");
  close.focus();
}
function closeMetaModal() {
  const modal = $("#meta-modal");
  modal.classList.add("hidden");
  modal.classList.remove("flex");
  if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
}
function wireMetaModal() {
  $$("[data-meta-close]").forEach(el => el.addEventListener("click", closeMetaModal));
  $("#meta-copy").addEventListener("click", () => {
    const json = $("#meta-json").dataset.raw || "";
    navigator.clipboard.writeText(json).then(() => toast("JSON gekopieerd.", ""));
  });
}

// ═══════════════════════════════════════════════════════════════
//  KEYBOARD PALETTE
// ═══════════════════════════════════════════════════════════════
function wireKeyboard() {
  const palette = $("#kb-palette");
  const metaModal = $("#meta-modal");
  const openPalette = () => { palette.classList.remove("hidden"); palette.classList.add("flex"); palette.querySelector("[data-kb-close]").focus(); };
  const closePalette = () => { palette.classList.add("hidden"); palette.classList.remove("flex"); };
  $$("[data-kb-close]").forEach(el => el.addEventListener("click", closePalette));

  document.addEventListener("keydown", e => {
    // Modal close
    if (e.key === "Escape") {
      if (!metaModal.classList.contains("hidden")) { closeMetaModal(); return; }
      if (!palette.classList.contains("hidden"))  { closePalette(); return; }
    }
    // Skip shortcuts if user is typing in an input/textarea
    const inField = e.target.matches("input,textarea,[contenteditable]");
    if (inField && e.key !== "Escape") return;
    if (e.key === "?")         { e.preventDefault(); openPalette(); }
    else if (e.key === "/")    { e.preventDefault(); setView("chat"); $("#input")?.focus(); }
    else if (e.key === "1")    setView("chat",       { fromKeyboard: true });
    else if (e.key === "2")    setView("documents",  { fromKeyboard: true });
    else if (e.key === "3")    setView("ingest",     { fromKeyboard: true });
    else if (e.key === "4")    setView("retrieval",  { fromKeyboard: true });
    else if (e.key === "5")    setView("crag",       { fromKeyboard: true });
    else if (e.key === "6")    setView("security",   { fromKeyboard: true });
    else if (e.key === "7")    setView("eval",       { fromKeyboard: true });
  });
}

// ═══════════════════════════════════════════════════════════════
//  INGESTIE (M1)
// ═══════════════════════════════════════════════════════════════
async function renderQuantWidget() {
  const host = $("#quant-grid");
  if (!host) return;
  try {
    const r = await fetch("/v1/admin/index_stats", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    const fmt = b => {
      b = Number(b) || 0;
      if (b < 1024)         return b.toFixed(0) + " B";
      if (b < 1024 ** 2)    return (b / 1024).toFixed(1) + " KB";
      if (b < 1024 ** 3)    return (b / 1024 ** 2).toFixed(1) + " MB";
      return (b / 1024 ** 3).toFixed(2) + " GB";
    };
    const labels = {
      fp32: "fp32 (default)",
      fp16: "fp16",
      int8: "int8",
      pq8:  "PQ8 (compressed)",
    };
    host.innerHTML = ["fp32", "fp16", "int8", "pq8"].map(prec => {
      const isCurrent = prec === d.current_precision;
      const cls = isCurrent
        ? "border-bd-orange bg-bd-orange/10"
        : "border-bd-border bg-bd-surface-2";
      return `
        <div class="border ${cls} rounded p-3">
          <div class="text-[10px] text-slate-400 uppercase tracking-wide">${esc(labels[prec])}${isCurrent ? " · actief" : ""}</div>
          <div class="font-mono text-lg text-bd-orange">${fmt(d.memory_bytes?.[prec])}</div>
          <div class="text-[10px] text-slate-500 mt-1">huidig corpus (${d.chunks ?? 0} chunks)</div>
          <div class="text-[10px] text-slate-500 mt-1 pt-1 border-t border-bd-border/50">
            @ 20M: <span class="font-mono text-slate-300">${fmt(d.production_20m_bytes?.[prec])}</span>
          </div>
        </div>`;
    }).join("");
  } catch (err) {
    host.innerHTML = `<div class="text-xs text-slate-500">Geen index-stats beschikbaar (${esc(err.message || String(err))})</div>`;
  }
}

async function loadTreeDocs() {
  const select = $("#tree-doc-select");
  if (select.options.length > 1) {
    // already loaded — ensure tree is rendered
    if (!state.treeDocId && state.lastDocuments.length) {
      select.value = state.lastDocuments[0].doc_id;
      loadTreeForDoc(state.lastDocuments[0].doc_id);
    }
    return;
  }
  renderLoading($("#tree-container"), { rows: 4, text: "Documenten laden…" });
  try {
    const r = await fetch("/v1/documents");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    state.lastDocuments = data.documents || [];
    for (const d of state.lastDocuments) {
      const opt = document.createElement("option");
      opt.value = d.doc_id;
      opt.textContent = `${d.title || d.doc_id} (${d.chunk_count} chunks)`;
      select.appendChild(opt);
    }
    if (state.lastDocuments.length && !state.treeDocId) {
      select.value = state.lastDocuments[0].doc_id;
      loadTreeForDoc(state.lastDocuments[0].doc_id);
    } else if (!state.lastDocuments.length) {
      renderEmpty($("#tree-container"), {
        icon: "📁",
        title: "Geen documenten in corpus",
        hint: "Upload een PDF hierboven om de hiërarchische structuur opgebouwd te zien.",
      });
    }
  } catch (err) {
    renderError($("#tree-container"), { err, onRetry: loadTreeDocs });
  }
}

async function loadTreeForDoc(docId) {
  state.treeDocId = docId;
  const container = $("#tree-container");
  renderLoading(container, { rows: 6, text: "Chunks ophalen…" });
  try {
    const r = await fetch(`/v1/documents/${encodeURIComponent(docId)}/chunks`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    for (const c of data.chunks || []) state.chunks.set(c.chunk_id, c);
    renderTree(data.chunks || []);
  } catch (err) {
    renderError(container, { err, onRetry: () => loadTreeForDoc(docId) });
  }
}

function renderTree(chunks) {
  const container = $("#tree-container");
  container.innerHTML = "";
  container.setAttribute("role", "tree");
  const sorted = [...chunks].sort((a, b) => (a.chunk_sequence ?? 0) - (b.chunk_sequence ?? 0));
  for (const c of sorted) {
    const node = document.createElement("div");
    node.className = "tree-node";
    node.dataset.chunkId = c.chunk_id;
    node.setAttribute("role", "treeitem");
    node.setAttribute("tabindex", "0");
    const parts = (c.hierarchy_path || "").split(" > ");
    const depth = Math.min(parts.length - 1, 5); // cap depth for responsive safety
    node.style.paddingLeft = `${depth * 14 + 8}px`;
    node.setAttribute("aria-level", String(parts.length));
    const label = parts.length > 6
      ? `…/${parts.slice(-2).join(" › ")}`
      : parts[parts.length - 1] || c.chunk_id;
    node.innerHTML = `<span>${esc(label)}</span>  <button class="chip chip-meta ml-2" data-cid="${esc(c.chunk_id)}" aria-label="Toon metadata">{ }</button>`;
    node.title = c.hierarchy_path || c.chunk_id;
    node.querySelector(".chip-meta").addEventListener("click", e => { e.stopPropagation(); openMetaModal(c.chunk_id, node); });
    node.addEventListener("click", () => openMetaModal(c.chunk_id, node));
    node.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openMetaModal(c.chunk_id, node); } });
    container.appendChild(node);
  }
}

function paintTreeNode(chunkId, status) {
  const node = document.querySelector(`.tree-node[data-chunk-id="${CSS.escape(chunkId)}"]`);
  if (!node) return;
  node.classList.add(status);
  if (status === "cited" && !node.querySelector(".cited-emoji")) {
    const e = document.createElement("span"); e.className = "cited-emoji"; e.textContent = " 🎯"; node.appendChild(e);
  }
  if (status === "parent-expanded" && !node.querySelector(".parent-label")) {
    const lbl = document.createElement("span");
    lbl.className = "chip ml-2 parent-label";
    lbl.style.background = "rgba(225,112,0,.2)"; lbl.style.color = "var(--bd-orange-2)"; lbl.style.borderColor = "var(--bd-orange)";
    lbl.textContent = "added as parent context";
    node.appendChild(lbl);
  }
}

function wireIngest() {
  const fileInput = $("#ingest-upload-file");
  $("#ingest-upload-btn").addEventListener("click", () => fileInput.click());
  $("#documents-upload-btn")?.addEventListener("click", () => fileInput.click());
  $("#chat-upload-btn")?.addEventListener("click", () => $("#chat-upload-file").click());
  $("#chat-upload-file")?.addEventListener("change", e => { const f = e.target.files?.[0]; if (f) startIngest(f); e.target.value = ""; });
  fileInput.addEventListener("change", async e => {
    const f = e.target.files?.[0]; if (f) await startIngest(f); e.target.value = "";
  });
  const drop = $("#ingest-drop");
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("border-bd-orange"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("border-bd-orange"));
  drop.addEventListener("drop", async e => {
    e.preventDefault(); drop.classList.remove("border-bd-orange");
    const f = e.dataTransfer?.files?.[0]; if (f) await startIngest(f);
  });
  $("#tree-doc-select").addEventListener("change", e => { if (e.target.value) loadTreeForDoc(e.target.value); });
  // floater -> go to ingest view
  $("#ingest-floater")?.addEventListener("click", () => setView("ingest"));
}

async function startIngest(file) {
  if (state.view !== "ingest") setView("ingest");
  const list = $("#ingest-chunks");
  const chatList = $("#chat-ingest-chunks");
  list.innerHTML = ""; if (chatList) chatList.innerHTML = "";
  const cuts = $("#ingest-cuts"); cuts.innerHTML = ""; cuts.classList.add("hidden");
  $("#ingest-status").textContent = `Upload van "${file.name}"...`;
  $("#chat-ingest-status") && ($("#chat-ingest-status").textContent = `Upload "${file.name}"...`);

  const fd = new FormData();
  fd.append("file", file);
  fd.append("title", file.name.replace(/\.[^.]+$/, ""));
  fd.append("security_classification", state.tier);

  let resp;
  try { resp = await fetch("/v1/ingest", { method: "POST", body: fd }); }
  catch (e) { toast("Upload mislukt.", "err"); return; }
  if (!resp.ok || !resp.body) { toast(`Upload fout HTTP ${resp.status}`, "err"); return; }

  const reader = resp.body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  const cardByChunk = new Map();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
      let ev = "message", line = "";
      for (const l of block.split("\n")) {
        if (l.startsWith("event:")) ev = l.slice(6).trim();
        else if (l.startsWith("data:")) line += l.slice(5).trim();
      }
      if (!line) continue;
      let data; try { data = JSON.parse(line); } catch { data = line; }
      handleIngestEvent(ev, data, cardByChunk, chatList);
    }
  }
}

function handleIngestEvent(ev, data, cardByChunk, chatList) {
  const status = $("#ingest-status");
  const chatStatus = $("#chat-ingest-status");
  const cuts = $("#ingest-cuts");
  switch (ev) {
    case "parsed":
      status.textContent = `📄 Geparsed (${(data.chars || 0).toLocaleString("nl")} tekens)`; break;
    case "chunker_choice": {
      const label = data.path === "semantic" ? "🔍 geen markers — AI bepaalt semantische grenzen"
                  : data.path === "structural" ? "🔍 structurele markers gevonden"
                  : "🔍 document als één chunk";
      status.innerHTML = `<strong>${label}</strong> <span class="text-slate-500">(${esc(data.reason)})</span>`;
      if (chatStatus) chatStatus.textContent = label;
      break;
    }
    case "semantic_cut":
      cuts.classList.remove("hidden");
      if (!cuts.querySelector("h4")) {
        const h = document.createElement("h4"); h.className = "text-[10px] font-bold text-bd-orange uppercase tracking-wider mb-1";
        h.textContent = "AI-voorgestelde breukpunten"; cuts.appendChild(h);
      }
      const ln = document.createElement("div");
      ln.className = "text-slate-400"; ln.textContent = `@${data.offset} — ${data.reason}`;
      cuts.appendChild(ln);
      break;
    case "chunk_started": {
      const card = document.createElement("div"); card.className = "chunk-card"; card.dataset.chunkId = data.chunk_id;
      card.setAttribute("tabindex", "0");
      card.setAttribute("aria-label", `Chunk ${data.chunk_id}`);
      card.innerHTML = `
        <div class="chunk-id">${colorChunkId(data.chunk_id)}</div>
        <div class="chunk-preview">${esc(data.text_preview || "")}${(data.text_preview || "").length >= 160 ? "…" : ""}</div>
        <div class="chunk-pills"><button type="button" class="chip chip-meta" aria-label="Toon metadata JSON">{ }</button></div>`;
      card.querySelector(".chip-meta").addEventListener("click", e => { e.stopPropagation(); openMetaModal(data.chunk_id, card); });
      card.addEventListener("click", () => openMetaModal(data.chunk_id, card));
      card.addEventListener("keydown", e => { if (e.key === "Enter") openMetaModal(data.chunk_id, card); });
      $("#ingest-chunks").appendChild(card);
      if (chatList) {
        const c2 = card.cloneNode(true);
        c2.querySelector(".chip-meta").addEventListener("click", e => { e.stopPropagation(); openMetaModal(data.chunk_id, c2); });
        chatList.appendChild(c2);
      }
      cardByChunk.set(data.chunk_id, card);
      state.chunks.set(data.chunk_id, {
        chunk_id: data.chunk_id, parent_chunk_id: data.parent_chunk_id,
        hierarchy_path: data.hierarchy_path, chunk_text: data.text_preview,
      });
      break;
    }
    case "chunk_enriched": {
      const card = cardByChunk.get(data.chunk_id); if (!card) break;
      const pills = card.querySelector(".chunk-pills");
      if (data.topic) pills.insertAdjacentHTML("afterbegin", `<span class="chip chip-topic">topic: ${esc(data.topic)}</span>`);
      for (const e of (data.entities || [])) pills.insertAdjacentHTML("beforeend", `<span class="chip chip-entity">${esc(e)}</span>`);
      const prev = state.chunks.get(data.chunk_id) || {};
      state.chunks.set(data.chunk_id, { ...prev, topic: data.topic, entities: data.entities, summary: data.summary });
      break;
    }
    case "chunk_embedded": {
      const card = cardByChunk.get(data.chunk_id); if (!card) break;
      card.querySelector(".chunk-pills").insertAdjacentHTML("beforeend", `<span class="chip chip-embed">🧮 ${data.dim}-dim</span>`);
      break;
    }
    case "chunk_indexed": {
      const card = cardByChunk.get(data.chunk_id); if (!card) break;
      card.querySelector(".chunk-pills").insertAdjacentHTML("beforeend", `<span class="chip chip-ok">✓ geïndexeerd</span>`);
      break;
    }
    case "complete":
      status.innerHTML = `<span class="text-green-400">✅ Klaar — ${data.chunks} chunks in ${(data.total_ms / 1000).toFixed(1)}s</span>`;
      if (chatStatus) chatStatus.innerHTML = `<span class="text-green-400">✅ ${data.chunks} chunks</span>`;
      toast(`Document ingested — ${data.chunks} chunks.`, "");
      break;
    case "error":
      toast(`Ingestiefout: ${data.detail}`, "err");
      status.textContent = `Fout: ${data.detail}`;
      break;
  }
}

function colorChunkId(id) {
  return id.split("::").map((seg, i) => {
    if (i === 0) return `<span class="doc">${esc(seg)}</span>`;
    if (seg.startsWith("art")) return `<span class="art">${esc(seg)}</span>`;
    if (seg.startsWith("par")) return `<span class="par">${esc(seg)}</span>`;
    if (seg.startsWith("sub")) return `<span class="sub">${esc(seg)}</span>`;
    if (seg.startsWith("chunk")) return `<span class="seq">${esc(seg)}</span>`;
    return esc(seg);
  }).join('<span class="text-slate-600">::</span>');
}

// ═══════════════════════════════════════════════════════════════
//  RETRIEVAL (M2)
// ═══════════════════════════════════════════════════════════════
function wireRetrieval() {
  $("#retrieval-form").addEventListener("submit", async e => {
    e.preventDefault();
    const q = $("#retrieval-query").value.trim();
    if (!q) return;
    await runRetrievalTrace(q);
  });
  // auto-resubmit when rerank toggle flips, if a query is already entered
  $("#retrieval-rerank").addEventListener("change", () => {
    const q = $("#retrieval-query").value.trim();
    if (q && state._retrievalRanOnce) runRetrievalTrace(q);
  });
}

async function runRetrievalTrace(query) {
  state._retrievalRanOnce = true;
  const withRerank = $("#retrieval-rerank").checked;
  $("#retrieval-timings").textContent = "⏳ Tracing…";
  renderLoading($("#river-bm25"),  { rows: 3 });
  renderLoading($("#river-knn"),   { rows: 3 });
  renderLoading($("#river-fused"), { rows: 3 });
  if (withRerank) renderLoading($("#river-reranked"), { rows: 3 });
  try {
    const r = await fetch("/v1/retrieval/trace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, security_tier: state.tier, with_rerank: withRerank }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    renderRetrievalTrace(d);
  } catch (e) {
    $("#retrieval-timings").textContent = "❌ " + e.message;
    renderError($("#river-bm25"), { err: e });
    renderError($("#river-knn"),  { err: e });
    renderError($("#river-fused"),{ err: e });
  }
}

function renderRankRows(container, items, cls = "", scoreField = "score") {
  container.innerHTML = "";
  if (!items.length) { container.innerHTML = `<div class="text-xs text-slate-400">geen hits</div>`; return; }
  for (const it of items) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `rank-row ${cls}`;
    row.setAttribute("aria-label", `Rang ${it.rank}, chunk ${it.chunk_id}`);
    row.innerHTML = `
      <div class="rank-badge">${it.rank}</div>
      <div class="rank-chunk-id" title="${esc(it.chunk_id)}">${esc(it.chunk_id)}</div>
      <div class="rank-score">${it[scoreField] ?? "—"}</div>
      <button class="chip chip-meta" data-cid="${esc(it.chunk_id)}" aria-label="Toon metadata">{ }</button>`;
    row.querySelector(".chip-meta").addEventListener("click", e => { e.stopPropagation(); openMetaModal(it.chunk_id, row); });
    row.addEventListener("click", () => openMetaModal(it.chunk_id, row));
    container.appendChild(row);
  }
}

function renderRetrievalTrace(d) {
  const counts = `BM25: ${d.bm25.length} · kNN: ${d.knn.length} · RRF: ${d.fused.length}${d.reranked?.length ? ` · Rerank: ${d.reranked.length}` : ""}`;
  $("#retrieval-timings").innerHTML = `${counts} · embed ${d.timings_ms.embed}ms · search ${d.timings_ms.search}ms · total <strong class="text-bd-orange">${d.timings_ms.total}ms</strong>`;

  // M9: Top-K config pills — show the live retrieval parameters.
  const c = d.config || {};
  const pills = $("#retrieval-config-pills");
  if (pills) {
    pills.innerHTML = [
      ["BM25 top-k",  c.top_k_bm25],
      ["kNN top-k",   c.top_k_knn],
      ["RRF k",       c.rrf_k],
      ["Rerank top-k",c.top_k_rerank],
    ].filter(([,v]) => v != null).map(([k,v]) =>
      `<span class="pill"><span class="text-slate-500">${esc(k)}</span> <strong class="text-bd-orange">${esc(String(v))}</strong></span>`
    ).join("");
  }

  renderRankRows($("#river-bm25"), d.bm25, "", "score");
  renderRankRows($("#river-knn"),  d.knn, "orange", "score");
  renderRankRows($("#river-fused"), d.fused, "", "rrf_score");
  if (d.reranked?.length) renderRankRows($("#river-reranked"), d.reranked, "green", "rerank_score");
  else $("#river-reranked").innerHTML = `<div class="text-xs text-slate-400">Rerank niet gevraagd.</div>`;
}

// ═══════════════════════════════════════════════════════════════
//  CRAG (M3)
// ═══════════════════════════════════════════════════════════════
function renderCragState() {
  const select = $("#crag-trace-select");
  select.innerHTML = `<option value="">— Kies een recente turn —</option>`;
  state.recentTurns.forEach(t => {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `${t.query.slice(0, 55)} — ${t.gradingResult || "?"}`;
    select.appendChild(opt);
  });

  const picked = state._cragFocusTurn || state.recentTurns[0]?.id;
  if (picked) {
    select.value = picked;
    paintCragForTurn(picked);
  } else {
    $("#crag-state-diagram").innerHTML = "";
    renderEmpty($("#crag-state-diagram"), {
      icon: "🔄", title: "Nog geen turns", hint: "Stel een vraag in Gesprek om een trace te zien.",
      cta: "Naar Gesprek →", onCta: () => setView("chat"),
    });
    $("#crag-grader").innerHTML = "";
  }
  select.onchange = e => { if (e.target.value) paintCragForTurn(e.target.value); };
}

function paintCragForTurn(turnId) {
  const turn = state.recentTurns.find(t => t.id === turnId);
  if (!turn) return;
  const visited = new Set(turn.trace.map(e => e.node));
  const refused = visited.has("refuse");
  const diagram = $("#crag-state-diagram");
  diagram.innerHTML = "";
  const flow = [
    ["cache_lookup", "classify_query", "decompose", "hyde", "retrieve", "grade_context"],
    ["rewrite_and_retry", "parent_expansion", "generate", "validate_output"],
    ["respond", "refuse"],
  ];
  for (const row of flow) {
    const rowEl = document.createElement("div");
    rowEl.className = "mb-2";
    row.forEach((s, i) => {
      const node = document.createElement("span");
      node.className = "state-node";
      node.textContent = s;
      if (visited.has(s)) node.classList.add("visited");
      if (s === "refuse" && refused) node.classList.add("refuse");
      if (s === "respond" && visited.has("respond")) node.classList.add("success");
      rowEl.appendChild(node);
      if (i < row.length - 1) { const a = document.createElement("span"); a.className = "state-arrow"; a.textContent = "→"; rowEl.appendChild(a); }
    });
    diagram.appendChild(rowEl);
  }
  const gradeEv = turn.trace.find(e => e.node === "grade_context");
  const graderEl = $("#crag-grader");
  if (gradeEv) {
    graderEl.innerHTML = `
      <div class="rank-row ${gradeEv.result === "RELEVANT" ? "green" : gradeEv.result === "IRRELEVANT" ? "" : "orange"}">
        <div class="rank-badge">${gradeEv.result === "RELEVANT" ? "✓" : gradeEv.result === "IRRELEVANT" ? "✗" : "?"}</div>
        <div class="rank-chunk-id">overall verdict</div>
        <div class="rank-score">${esc(gradeEv.result)}</div>
      </div>
      <div class="text-xs text-slate-400 mt-2">${esc(gradeEv.detail || "")}</div>`;
  } else graderEl.innerHTML = `<div class="text-xs text-slate-400">Geen grading-event in deze turn.</div>`;
}

// ═══════════════════════════════════════════════════════════════
//  SECURITY (M4)
// ═══════════════════════════════════════════════════════════════
function renderTierChips() {
  const host = $("#sec-tier-chips");
  host.innerHTML = "";
  for (const t of ["PUBLIC", "INTERNAL", "RESTRICTED", "CLASSIFIED_FIOD"]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = t;
    btn.className = "text-xs px-3 py-1.5 rounded border " + (t === state.tier ? "bg-bd-orange border-bd-orange text-white" : "bg-bd-surface-2 border-bd-border text-slate-300 hover:border-bd-navy");
    btn.addEventListener("click", () => setTier(t));
    host.appendChild(btn);
  }
  const ALL_TIERS = ["PUBLIC", "INTERNAL", "RESTRICTED", "CLASSIFIED_FIOD"];
  const accessible = TIER_ACCESS[state.tier] || ["PUBLIC"];
  const blocked = ALL_TIERS.filter(t => !accessible.includes(t));
  const summary = $("#sec-summary");
  if (summary) {
    summary.innerHTML = `
      <div>
        <div class="text-xs text-slate-400 mb-1.5">Zichtbaar voor jou</div>
        <div class="flex flex-wrap gap-1.5">${accessible.map(t => `<span class="pill pill-tier">${esc(TIER_LABELS[t] || t)}</span>`).join("")}</div>
      </div>
      ${blocked.length ? `
        <div>
          <div class="text-xs text-slate-400 mb-1.5">Niet toegankelijk</div>
          <div class="flex flex-wrap gap-1.5">${blocked.map(t => `<span class="pill pill-blocked">🔒 ${esc(TIER_LABELS[t] || t)}</span>`).join("")}</div>
        </div>` : ""}
      <ul class="text-xs text-slate-400 space-y-1 pt-2 border-t border-bd-border">
        <li>✓ Alleen toegankelijke tiers worden doorzocht — geclassificeerde documenten beïnvloeden de ranking niet.</li>
        <li>✓ Verlopen documenten (vervaldatum in het verleden) worden uitgesloten.</li>
      </ul>`;
  }
}

async function loadCacheEntries(probe = null) {
  const host = $("#cache-entries");
  renderLoading(host, { rows: 3, text: "Cache ophalen…" });
  const url = new URL("/v1/cache/entries", location.origin);
  if (probe) url.searchParams.set("query", probe);
  url.searchParams.set("tier", state.tier);
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    $("#cache-threshold").textContent = d.threshold;
    host.innerHTML = "";
    const real = d.entries || [];
    if (!real.length) {
      renderEmpty(host, { icon: "💾", title: "Geen entries op dit tier.", hint: "Stel een vraag om de cache te vullen." });
      return;
    }
    for (const e of real) host.appendChild(renderCacheRow(e, d.threshold));
  } catch (err) {
    renderError(host, { err, onRetry: () => loadCacheEntries(probe) });
  }
}

function renderCacheRow(e, threshold) {
  const row = document.createElement("div");
  row.className = `cache-row ${e.would_hit ? "hit" : ""} ${e.accessible_to_user ? "" : "blocked"} ${e.stub ? "stub" : ""}`;
  const simHtml = e.similarity_to_probe == null
    ? `<span class="sim-badge miss">—</span>`
    : `<span class="sim-badge ${e.similarity_to_probe >= threshold ? "hit" : "miss"}">${e.similarity_to_probe.toFixed ? e.similarity_to_probe.toFixed(4) : e.similarity_to_probe}</span>`;
  row.innerHTML = `
    <span class="pill pill-tier">${esc(e.tier)}</span>
    <span class="text-[10px] text-slate-400 font-mono">${e.accessible_to_user ? "🔓" : "🔒 blocked"}</span>
    <span class="truncate">${esc(e.query || "(no query stored)")}</span>
    ${simHtml}
    <span class="text-[10px] text-slate-500">${e.citation_count || 0} cit</span>`;
  return row;
}

function wireSecurity() {
  $("#cache-probe").addEventListener("input", () => {
    clearTimeout(state._probeDebounce);
    state._probeDebounce = setTimeout(() => loadCacheEntries($("#cache-probe").value.trim() || null), 400);
  });
  $("#cache-refresh").addEventListener("click", () => loadCacheEntries($("#cache-probe").value.trim() || null));
  $("#audit-refresh")?.addEventListener("click", () => renderAuditTable());
}

async function renderAuditTable() {
  const host = $("#audit-table");
  if (!host) return;
  try {
    const r = await fetch("/v1/audit/recent?limit=50", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    if (!d.entries?.length) {
      host.innerHTML = `<div class="text-slate-500 text-[11px]">Nog geen queries gelogd vandaag.</div>`;
      return;
    }
    const gradeColor = g => {
      if (g === "RELEVANT") return "text-bd-green";
      if (g === "IRRELEVANT") return "text-bd-red";
      if (g === "CACHE_HIT") return "text-bd-navy-2";
      if (g === "AMBIGUOUS") return "text-bd-amber";
      return "text-slate-400";
    };
    host.innerHTML = `
      <div class="grid grid-cols-12 gap-2 text-[10px] uppercase tracking-wider text-slate-500 pb-1 border-b border-bd-border">
        <div class="col-span-2">Tijd</div>
        <div class="col-span-2">Tier</div>
        <div class="col-span-5">Query</div>
        <div class="col-span-2">Grade</div>
        <div class="col-span-1 text-right">TTFT</div>
      </div>
      ${d.entries.map(e => `
        <div class="grid grid-cols-12 gap-2 font-mono py-1 border-b border-bd-border/40">
          <div class="col-span-2 text-slate-500">${new Date((e.ts||0)*1000).toLocaleTimeString()}</div>
          <div class="col-span-2"><span class="pill pill-tier">${esc(e.tier || "")}</span></div>
          <div class="col-span-5 truncate text-slate-300" title="${esc(e.query || "")}">${esc(e.query || "")}</div>
          <div class="col-span-2 ${gradeColor(e.grade)}">${esc(e.grade || "")}</div>
          <div class="col-span-1 text-right text-slate-500">${e.ttft_ms != null ? Math.round(e.ttft_ms) + "ms" : "—"}</div>
        </div>`).join("")}`;
  } catch (err) {
    host.innerHTML = `<div class="text-slate-500 text-[11px]">Audit niet beschikbaar (${esc(err.message || String(err))})</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
//  EVAL (M4b)
// ═══════════════════════════════════════════════════════════════
async function loadEval() {
  let data = null;
  try {
    const r = await fetch("/v1/eval/latest", { cache: "no-store" });
    if (r.ok) data = await r.json();
  } catch {}

  const metrics = data ? buildLiveMetrics(data) : buildEmptyMetrics();
  $("#eval-metrics").innerHTML = metrics.map(m => `
    <div class="metric-card">
      <div class="metric-label">${esc(m.label)}</div>
      <div class="metric-value ${m.cls}">${esc(m.value)}</div>
      <div class="text-[11px] text-slate-400">${esc(m.hint)}</div>
    </div>`).join("");

  renderGate(data);

  if (data) {
    const ts = new Date(data.ts * 1000).toLocaleString();
    const dur = Math.round(data.total_duration_s || 0);
    const judge = data.ragas?.judge_model || data.deepeval?.judge_model || "—";
    $("#eval-runner").innerHTML = `
      <div class="text-xs text-slate-400">
        Laatste run: <strong>${esc(ts)}</strong> · ${data.golden_count} queries · ${dur}s · judge <code class="text-bd-orange">${esc(judge)}</code>
      </div>`;
  } else {
    $("#eval-runner").innerHTML = `<div class="text-xs text-slate-400">
      Geen run beschikbaar. Klik "Run" om de golden set door Ragas + DeepEval te sturen. Duurt 1-3 minuten op CPU.
    </div>`;
  }
}

function buildLiveMetrics(d) {
  const r = d.ragas || {};
  const e = d.deepeval || {};
  // Higher-is-better metrics (Ragas): green when ≥ threshold
  const hib = (v, t) => v == null ? "warn" : v >= t ? "good" : v >= t * 0.85 ? "warn" : "bad";
  // Lower-is-better metrics (DeepEval): green when ≤ threshold
  const lib = (v, t) => v == null ? "warn" : v <= t ? "good" : v <= t * 1.5 ? "warn" : "bad";
  const fmt = v => v == null ? "—" : Number(v).toFixed(2);
  return [
    {label: "Faithfulness",       value: fmt(r.faithfulness),       cls: hib(r.faithfulness, 0.90),       hint: "Ragas · claim is gegrond in context"},
    {label: "Context Recall",     value: fmt(r.context_recall),     cls: hib(r.context_recall, 0.85),     hint: "Ragas · golden chunks retrieved"},
    {label: "Answer Relevancy",   value: fmt(r.answer_relevancy),   cls: hib(r.answer_relevancy, 0.85),   hint: "Ragas · semantiek vs vraag"},
    {label: "Hallucination",      value: fmt(e.hallucination),      cls: lib(e.hallucination, 0.10),      hint: "DeepEval · fabricatie-rate"},
    {label: "Bias",               value: fmt(e.bias),               cls: lib(e.bias, 0.10),               hint: "DeepEval · demografische drift"},
    {label: "Toxicity",           value: fmt(e.toxicity),           cls: lib(e.toxicity, 0.05),           hint: "DeepEval · PII / schade"},
  ];
}

function buildEmptyMetrics() {
  return ["Faithfulness", "Context Recall", "Answer Relevancy", "Hallucination", "Bias", "Toxicity"]
    .map(label => ({ label, value: "—", cls: "warn", hint: "Nog geen run" }));
}

async function runGoldenSet() {
  const host = $("#eval-runner");
  renderLoading(host, { rows: 5, text: "Ragas + DeepEval draaien over de golden set… dit kan 1-3 minuten duren op CPU." });
  try {
    const r = await fetch("/v1/eval/run", { method: "POST" });
    if (r.status === 409) throw new Error("Er draait al een eval-run; wacht tot die klaar is.");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await r.json();           // we read the body so the connection closes cleanly
    await loadEval();         // refreshes metrics-grid + gate from cached result
  } catch (err) {
    renderError(host, { err, onRetry: runGoldenSet });
  }
}

function renderGate(d) {
  const r = d?.ragas || {};
  const e = d?.deepeval || {};
  const stages = [
    { name: "Retrieval Quality",  req: "Context Recall ≥ 0.85",  pass: (r.context_recall   ?? 0) >= 0.85 },
    { name: "Generation Quality", req: "Faithfulness ≥ 0.90",    pass: (r.faithfulness     ?? 0) >= 0.90 },
    { name: "Answer Quality",     req: "Answer Relevancy ≥ 0.85",pass: (r.answer_relevancy ?? 0) >= 0.85 },
    { name: "Safety",             req: "Hallucination ≤ 0.10",   pass: (e.hallucination    ?? 1) <= 0.10 },
  ];
  const haveData = !!d;
  const ship = haveData && stages.every(s => s.pass);
  $("#eval-gate").innerHTML = `
    <div class="flex items-center justify-between pb-2 border-b border-bd-border mb-2">
      <span class="text-xs text-slate-400">Gate verdict</span>
      <span class="text-sm font-bold ${haveData ? (ship ? "text-green-400" : "text-red-400") : "text-slate-400"}">
        ${haveData ? (ship ? "✓ SHIP" : "✗ HOLD") : "— geen run —"}
      </span>
    </div>
    ${stages.map(s => `
      <div class="flex items-center justify-between">
        <span>${esc(s.name)} <span class="text-slate-500 text-[10px]">(${esc(s.req)})</span></span>
        <span class="${haveData ? (s.pass ? "text-green-400" : "text-red-400") : "text-slate-500"} font-bold">${haveData ? (s.pass ? "✓" : "✗") : "·"}</span>
      </div>`).join("")}
    <p class="text-[10px] text-slate-500 pt-2 border-t border-bd-border mt-2">
      In productie draait dit op CI bij elke PR. Falen → deploy blocked.
    </p>`;
}

function wireEval() { $("#eval-run-btn").addEventListener("click", runGoldenSet); }

// ═══════════════════════════════════════════════════════════════
//  DOCUMENTS
// ═══════════════════════════════════════════════════════════════
async function loadDocuments() {
  const list = $("#documents-list");
  renderLoading(list, { rows: 3, text: "Documenten ophalen…" });
  try {
    const r = await fetch("/v1/documents");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    state.lastDocuments = d.documents || [];
    list.innerHTML = "";
    if (!state.lastDocuments.length) {
      renderEmpty(list, { icon: "📚", title: "Geen documenten", hint: "Upload er een via de knop rechtsboven." });
      return;
    }
    for (const doc of state.lastDocuments) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "doc-card text-left";
      card.innerHTML = `
        <div class="doc-title">${esc(doc.title || doc.doc_id)}</div>
        <div class="doc-pills">
          <span class="pill pill-tier">${esc(doc.security_classification || "PUBLIC")}</span>
          <span class="pill">${doc.chunk_count} chunks</span>
          ${doc.doc_type ? `<span class="pill">${esc(doc.doc_type)}</span>` : ""}
        </div>`;
      card.addEventListener("click", () => { setView("ingest"); setTimeout(() => loadTreeForDoc(doc.doc_id), 50); });
      list.appendChild(card);
    }
  } catch (err) { renderError(list, { err, onRetry: loadDocuments }); }
}

// ═══════════════════════════════════════════════════════════════
//  BOOT
// ═══════════════════════════════════════════════════════════════
function onAppReady() {
  wireNav();
  wireChat();
  wireIngest();
  wireRetrieval();
  wireSecurity();
  wireEval();
  wireMetaModal();
  wireKeyboard();
  renderSuggestedPrompts();
  ensureTierBanners();
  const initial = parseHash() || "chat";
  history.replaceState({ view: initial, depth: 0 }, "", "#" + initial);
  setView(initial, { silent: true });
}

waitForWarmup();
