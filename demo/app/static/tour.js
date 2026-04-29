/* Belastingdienst KennisAssistent — Guided Tour
 *
 * Self-contained module. Exposes window.startTour().
 * Walks the visitor across all 7 workspaces and overlays English copy
 * mapping each stop to a criterion from the assessment.
 *
 * No external libs. Reuses setView() from app.js to navigate.
 */
(function () {
  "use strict";

  const STORAGE_KEY = "tour_completed_v1";
  const SPOTLIGHT_PAD = 10;
  const TOOLTIP_GAP = 18;

  const TOUR_STEPS = [
    {
      view: null,
      target: "body",
      eyebrow: "TOUR · BELASTINGDIENST KENNISASSISTENT",
      title: "Welcome — 90 seconds across the whole stack",
      body: "This walk-through visits every workspace and shows which assessment module each one answers. Press <strong>Next</strong> to advance, <strong>Esc</strong> to exit.",
    },
    {
      view: "chat",
      target: "#input",
      eyebrow: "MODULE 3 · AGENTIC RAG",
      title: "Where the end-user lives",
      body: "The assessment asks for an agentic, self-healing RAG — not a linear demo. This chat is wired end-to-end on a real CRAG state machine. Every turn streams a trace you can replay in the CRAG-pipeline workspace.",
    },
    {
      view: "documents",
      target: "#documents-list",
      eyebrow: "MODULE 1 · INGESTION + MODULE 4 · RBAC",
      title: "The corpus, audited per tier",
      body: "29 documents, 174 chunks across 4 security tiers. Each doc shows a tier badge — that label is what the pre-retrieval RBAC filter consults at query time.",
    },
    {
      view: "ingest",
      target: "#tree-container",
      eyebrow: "MODULE 1 · CHUNKING + METADATA",
      title: "Hierarchical chunking, not recursive splits",
      body: "The assessment is explicit: <em>\"recursive text splitters destroy the hierarchical context of legal documents\"</em>. Every chunk here carries <code>parent_chunk_id</code> and <code>hierarchy_path</code> — citations are verifiable down to the exact Lid.",
    },
    {
      view: "ingest",
      target: "#quant-grid",
      eyebrow: "MODULE 1 · VECTOR DB SCALE",
      title: "OOM math, not optimism",
      body: "The assessment asks for HNSW configs and quantization to prevent OOM at 20M chunks. This widget projects current corpus across precisions: int8 fits in ~14 GB where fp32 needs 56 GB.",
    },
    {
      view: "retrieval",
      target: "#retrieval-query",
      eyebrow: "MODULE 2 · RETRIEVAL",
      title: "Hybrid search, side-by-side",
      body: "The assessment requires combining sparse and dense retrieval. The four rivers below show BM25 ranks, kNN ranks, the RRF (k=60) fusion, and an optional LLM rerank — fused on <strong>rank</strong>, not score.",
    },
    {
      view: "crag",
      target: "#crag-state-diagram",
      eyebrow: "MODULE 3 · CORRECTIVE RAG",
      title: "Self-healing pipeline",
      body: "The assessment's <strong>zero-hallucination tolerance</strong> is a hard constraint — fiscal advice must be 100% factually accurate. The 9-state machine grades retrieved chunks <em>before</em> generation; IRRELEVANT or AMBIGUOUS routes to refuse, not to a hallucination.",
    },
    {
      view: "security",
      target: "#sec-tier-chips",
      eyebrow: "MODULE 4 · DATABASE-LEVEL SECURITY",
      title: "Pre-retrieval RBAC — leak-proof by math",
      body: "The assessment asks <em>at which stage</em> filtering must occur. Pre-scoring: a classified chunk cannot influence TF-IDF, kNN neighborhoods, or the cache. Switch the role badge — pills flip live.",
    },
    {
      view: "eval",
      target: "#eval-metrics",
      eyebrow: "MODULE 4 · OBSERVABILITY + EVAL",
      title: "CI/CD eval gate, not vibe-checks",
      body: "Faithfulness · Context Recall · Hallucination — real Ragas + DeepEval numbers, not a stub. Ship/Hold pills flip on pre-set thresholds; production would block PR merges below them.",
    },
    {
      view: null,
      target: "body",
      eyebrow: "END · YOUR TURN",
      title: "Tour complete — what to try next",
      body: "Try a query in <strong>Gesprek</strong>, switch to <strong>FIOD-rechercheur</strong> for an RBAC demo, or trip the breaker via the chaos endpoint. The whole stack runs offline once warmed up.",
    },
  ];

  const state = { idx: 0, running: false, resizeRaf: 0 };
  let onKeyDown = null;
  let onResize = null;
  let activePulseEl = null;

  // ─── public API ───
  function startTour() {
    if (state.running) return;
    state.running = true;
    state.idx = 0;
    onKeyDown = (e) => {
      if (e.key === "Escape") skipTour();
      else if (e.key === "ArrowRight" || e.key === "Enter") nextStep();
      else if (e.key === "ArrowLeft") prevStep();
    };
    document.addEventListener("keydown", onKeyDown);
    onResize = debounce(() => repaintCurrent(), 100);
    window.addEventListener("resize", onResize);
    renderStep(TOUR_STEPS[0]);
  }

  function nextStep() {
    if (!state.running) return;
    if (state.idx >= TOUR_STEPS.length - 1) {
      endTour();
      return;
    }
    state.idx++;
    renderStep(TOUR_STEPS[state.idx]);
  }

  function prevStep() {
    if (!state.running || state.idx === 0) return;
    state.idx--;
    renderStep(TOUR_STEPS[state.idx]);
  }

  function skipTour() {
    markTourSeen();
    teardown();
  }

  function endTour() {
    markTourSeen();
    teardown();
  }

  // ─── core renderer ───
  async function renderStep(step) {
    // 1. Switch workspace if needed (uses existing setView from app.js)
    if (step.view && typeof window.setView === "function") {
      try { window.setView(step.view); } catch (e) { /* noop */ }
    }

    // 2. Wait for the target to exist + be measurable
    let target = null;
    let rect = null;
    if (step.target && step.target !== "body") {
      target = await waitForElement(step.target, 2000);
      if (target) {
        scrollAndCenter(target);
        // wait one frame for the smooth-scroll to start so the spotlight aligns
        await sleep(120);
        rect = target.getBoundingClientRect();
      }
    }

    // 3. Paint spotlight (or full overlay if no target)
    paintOverlay(rect);

    // 4. Paint tooltip
    paintTooltip(step, rect);

    // 5. Pulse the target
    setPulse(target);
  }

  function repaintCurrent() {
    if (!state.running) return;
    const step = TOUR_STEPS[state.idx];
    if (!step) return;
    let rect = null;
    if (step.target && step.target !== "body") {
      const el = document.querySelector(step.target);
      if (el) rect = el.getBoundingClientRect();
    }
    paintOverlay(rect);
    paintTooltip(step, rect);
  }

  // ─── overlay (spotlight cutout) ───
  function paintOverlay(rect) {
    let overlay = document.getElementById("tour-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "tour-overlay";
      // clicking the overlay should NOT advance — user must click Next.
      overlay.addEventListener("click", (e) => e.stopPropagation());
      document.body.appendChild(overlay);
    }
    if (!rect) {
      // overlay-only mode (welcome / closing step)
      overlay.style.clipPath = "none";
      return;
    }
    const x1 = Math.max(0, rect.left - SPOTLIGHT_PAD);
    const y1 = Math.max(0, rect.top - SPOTLIGHT_PAD);
    const x2 = Math.min(window.innerWidth, rect.right + SPOTLIGHT_PAD);
    const y2 = Math.min(window.innerHeight, rect.bottom + SPOTLIGHT_PAD);
    // even-odd polygon: outer rectangle minus inner rectangle
    overlay.style.clipPath =
      "polygon(" +
      "0% 0%, 100% 0%, 100% 100%, 0% 100%, 0% 0%, " +
      x1 + "px " + y1 + "px, " +
      x1 + "px " + y2 + "px, " +
      x2 + "px " + y2 + "px, " +
      x2 + "px " + y1 + "px, " +
      x1 + "px " + y1 + "px" +
      ")";
  }

  // ─── tooltip card ───
  function paintTooltip(step, rect) {
    let tip = document.getElementById("tour-tooltip");
    const isFirstPaint = !tip;
    if (!tip) {
      tip = document.createElement("div");
      tip.id = "tour-tooltip";
      document.body.appendChild(tip);
    }

    const total = TOUR_STEPS.length;
    const idx = state.idx;
    const progressPct = Math.round(((idx + 1) / total) * 100);
    const isLast = idx === total - 1;
    const isFirst = idx === 0;

    tip.innerHTML =
      '<div class="tour-eyebrow">' + escapeHtml(step.eyebrow) + '</div>' +
      '<h3>' + escapeHtml(step.title) + '</h3>' +
      '<p>' + step.body + '</p>' +
      '<div class="tour-progress"><div class="tour-progress-bar" style="width:' + progressPct + '%"></div></div>' +
      '<div class="tour-controls">' +
        '<span class="tour-step-counter">Step ' + (idx + 1) + ' / ' + total + '</span>' +
        '<div class="tour-actions">' +
          (isFirst ? '' :
            '<button type="button" class="tour-btn tour-btn-ghost" data-tour-action="prev">Back</button>') +
          '<button type="button" class="tour-btn tour-btn-ghost" data-tour-action="skip">' +
          (isLast ? 'Close' : 'Skip') + '</button>' +
          '<button type="button" class="tour-btn tour-btn-primary" data-tour-action="next">' +
          (isLast ? 'Done' : 'Next →') + '</button>' +
        '</div>' +
      '</div>';

    // Wire buttons — fresh innerHTML means listeners must be re-bound every paint.
    tip.querySelectorAll("[data-tour-action]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const a = btn.getAttribute("data-tour-action");
        if (a === "next") nextStep();
        else if (a === "prev") prevStep();
        else if (a === "skip") skipTour();
      });
    });

    // Re-show animation on first paint of a step (not on resize repaints)
    if (isFirstPaint || tip.dataset.lastIdx !== String(idx)) {
      tip.style.animation = "none";
      // force reflow so animation can restart
      void tip.offsetWidth;
      tip.style.animation = "";
      tip.dataset.lastIdx = String(idx);
    }

    // Position it
    positionTooltip(tip, rect);
  }

  function positionTooltip(tip, rect) {
    const w = tip.offsetWidth || 440;
    const h = tip.offsetHeight || 220;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const margin = 16;

    if (!rect) {
      // center the tooltip on viewport
      tip.style.left = Math.max(margin, (vw - w) / 2) + "px";
      tip.style.top = Math.max(margin, (vh - h) / 2) + "px";
      return;
    }

    let x = rect.left + rect.width / 2 - w / 2;
    let y;

    // prefer above
    if (rect.top - h - TOOLTIP_GAP > margin) {
      y = rect.top - h - TOOLTIP_GAP;
    } else if (rect.bottom + TOOLTIP_GAP + h < vh - margin) {
      y = rect.bottom + TOOLTIP_GAP;
    } else {
      // target is too tall — center vertically
      y = Math.max(margin, (vh - h) / 2);
    }

    x = clamp(x, margin, vw - w - margin);
    y = clamp(y, margin, vh - h - margin);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  // ─── pulse on the highlighted element ───
  function setPulse(el) {
    if (activePulseEl && activePulseEl !== el) {
      activePulseEl.classList.remove("tour-pulse");
    }
    activePulseEl = el || null;
    if (el) el.classList.add("tour-pulse");
  }

  // ─── teardown ───
  function teardown() {
    state.running = false;
    if (onKeyDown) document.removeEventListener("keydown", onKeyDown);
    if (onResize) window.removeEventListener("resize", onResize);
    onKeyDown = null;
    onResize = null;
    if (activePulseEl) {
      activePulseEl.classList.remove("tour-pulse");
      activePulseEl = null;
    }
    const overlay = document.getElementById("tour-overlay");
    if (overlay) overlay.remove();
    const tip = document.getElementById("tour-tooltip");
    if (tip) tip.remove();
  }

  // ─── helpers ───
  function waitForElement(selector, timeoutMs) {
    return new Promise((resolve) => {
      const start = performance.now();
      function tick() {
        const el = document.querySelector(selector);
        // require visible (offsetParent !== null) so we don't anchor on a hidden workspace
        if (el && (el.offsetParent !== null || el === document.body)) {
          resolve(el);
          return;
        }
        if (performance.now() - start > timeoutMs) {
          console.warn("[tour] target not found:", selector);
          resolve(null);
          return;
        }
        requestAnimationFrame(tick);
      }
      tick();
    });
  }

  function scrollAndCenter(el) {
    try {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    } catch (e) {
      el.scrollIntoView();
    }
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function debounce(fn, ms) {
    let t = 0;
    return function () {
      const args = arguments;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(null, args), ms);
    };
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function markTourSeen() {
    try { localStorage.setItem(STORAGE_KEY, "1"); } catch (e) { /* noop */ }
  }

  function wasTourSeen() {
    try { return localStorage.getItem(STORAGE_KEY) === "1"; } catch (e) { return false; }
  }

  // expose
  window.startTour = startTour;
  window.tourWasSeen = wasTourSeen;
})();
