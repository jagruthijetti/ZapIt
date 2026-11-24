let hostWs = null;
let reservedPin = null;
let generatedQuestions = [];
let selectedBots = new Set();
let genTimerInterval = null;

const ALL_HOST_VIEWS = [
  "view-home",
  "view-create",
  "view-history",
  "view-studio",
  "view-lobby",
  "view-question",
  "view-result",
  "view-podium",
];

async function safeFetchJson(url, options = {}) {
  const res = await fetch(url, options);
  const rawText = await res.text();
  let data = {};
  try {
    data = rawText ? JSON.parse(rawText) : {};
  } catch (_) {
    throw new Error(rawText || `Server error (HTTP ${res.status})`);
  }
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (HTTP ${res.status})`);
  }
  return data;
}

function showGenError(message) {
  const box = document.getElementById("gen-error-box");
  if (!box) return;
  if (!message) {
    box.classList.add("hidden");
    box.innerText = "";
  } else {
    box.classList.remove("hidden");
    box.innerText = message;
  }
}

function updateHeaderNavHighlight(viewId) {
  const createBtn = document.getElementById("nav-btn-create");
  const historyBtn = document.getElementById("nav-btn-history");
  if (createBtn) {
    createBtn.classList.toggle("border-violet-500", viewId === "view-create");
    createBtn.classList.toggle("bg-violet-500/20", viewId === "view-create");
  }
  if (historyBtn) {
    historyBtn.classList.toggle("border-cyan-500", viewId === "view-history");
    historyBtn.classList.toggle("bg-cyan-500/20", viewId === "view-history");
  }
}

function showView(viewId) {
  ALL_HOST_VIEWS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.classList.add("hidden");
  });
  updateHeaderNavHighlight(viewId);

  const target = document.getElementById(viewId);
  if (target) {
    target.classList.remove("hidden");
    if (window.gsap) {
      gsap.fromTo(target, { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.35, ease: "power3.out" });
    }
  }
}

function openCreateView() {
  showGenError("");
  showView("view-create");
}

function openHistoryView() {
  showView("view-history");
  loadHostHistory();
}

function toggleBot(persona, btn) {
  if (selectedBots.has(persona)) {
    selectedBots.delete(persona);
    btn.classList.remove("border-violet-500", "bg-violet-500/20", "text-violet-200");
    btn.classList.add("border-zinc-800", "bg-zinc-950", "text-zinc-400");
  } else {
    selectedBots.add(persona);
    btn.classList.remove("border-zinc-800", "bg-zinc-950", "text-zinc-400");
    btn.classList.add("border-violet-500", "bg-violet-500/20", "text-violet-200");
  }
}

async function generateQuiz() {
  showGenError("");

  if (location.protocol === "file:") {
    showGenError("Please open http://localhost:8000/host in your browser instead of opening the HTML file directly.");
    return;
  }

  const titleEl = document.getElementById("quiz-title");
  const sourceEl = document.getElementById("quiz-source");
  const countEl = document.getElementById("quiz-count");
  const diffEl = document.getElementById("quiz-difficulty");
  const provEl = document.getElementById("quiz-provider");
  const hostEl = document.getElementById("host-name");

  const title = (titleEl?.value || "").trim();
  const sourceText = (sourceEl?.value || "").trim();
  const questionCount = parseInt(countEl?.value || "5", 10) || 5;
  const difficulty = diffEl?.value || "Medium";
  const provider = provEl?.value || "gemini";
  const hostName = (hostEl?.value || "Instructor").trim() || "Instructor";

  if (title.length < 2) {
    showGenError("Please enter a Quiz Title (at least 2 characters).");
    return;
  }
  if (sourceText.length < 20) {
    showGenError("Please paste at least 20 characters of source study material.");
    return;
  }

  const genBtn = document.getElementById("btn-generate-quiz");
  const statusCard = document.getElementById("gen-status-card");
  if (genBtn) {
    genBtn.disabled = true;
    genBtn.innerText = "Generating Quiz...";
  }
  if (statusCard) statusCard.classList.remove("hidden");

  try {
    // 1. Reserve 6-digit PIN immediately
    const reserveData = await safeFetchJson("/api/rooms/reserve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, question_count: questionCount }),
    });
    reservedPin = reserveData.game_pin;
    const totalEst = reserveData.estimated_seconds || 15;

    const pinBadge = document.getElementById("reserved-pin-display");
    if (pinBadge) pinBadge.innerText = reservedPin;

    let elapsed = 0;
    if (genTimerInterval) clearInterval(genTimerInterval);
    genTimerInterval = setInterval(() => {
      elapsed += 1;
      const rem = Math.max(1, totalEst - elapsed);
      const pct = Math.min(95, Math.round((elapsed / totalEst) * 100));
      const bar = document.getElementById("gen-progress-bar");
      const txt = document.getElementById("gen-countdown-text");
      if (bar) bar.style.width = `${pct}%`;
      if (txt) {
        txt.innerText =
          elapsed < totalEst
            ? `~${rem}s remaining (${elapsed}s elapsed)`
            : `Still generating on AI engine (${elapsed}s elapsed)...`;
      }
    }, 1000);

    // 2. Generate MCQs
    const data = await safeFetchJson("/api/quizzes/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        source_text: sourceText,
        question_count: questionCount,
        difficulty,
        provider,
        host_name: hostName,
      }),
    });

    clearInterval(genTimerInterval);
    generatedQuestions = data.questions || [];
    const modelBadge = document.getElementById("studio-model-badge");
    if (modelBadge) modelBadge.innerText = `Generated by ${data.model_label}`;

    renderStudioQuestions();
    showView("view-studio");
  } catch (err) {
    if (genTimerInterval) clearInterval(genTimerInterval);
    showGenError("Generation Error: " + err.message);
  } finally {
    if (genBtn) {
      genBtn.disabled = false;
      genBtn.innerText = "Generate Quiz & Reserve PIN ⚡";
    }
    if (statusCard) statusCard.classList.add("hidden");
  }
}

function renderStudioQuestions() {
  const letters = ["A", "B", "C", "D"];
  const listEl = document.getElementById("studio-questions-list");
  if (!listEl) return;

  listEl.innerHTML = generatedQuestions
    .map(
      (q, idx) => `
      <div class="p-4 rounded-2xl bg-zinc-950/85 border border-zinc-800 space-y-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-violet-400">MCQ #${idx + 1}</span>
          <button type="button" onclick="removeStudioQuestion(${idx})" class="text-xs text-rose-400 hover:underline">Remove</button>
        </div>
        <input type="text" value="${escapeHtml(q.question)}" onchange="generatedQuestions[${idx}].question = this.value"
          class="w-full px-3 py-2 rounded-xl bg-zinc-900 border border-zinc-700 text-sm font-medium focus:outline-none focus:border-violet-500" />
        <div class="grid sm:grid-cols-2 gap-2">
          ${(q.options || [])
            .map(
              (opt, oIdx) => `
              <div class="flex items-center gap-2 bg-zinc-900 px-3 py-2 rounded-xl border ${
                oIdx === q.correct_option_index ? "border-emerald-500/60" : "border-zinc-800"
              }">
                <input type="radio" name="correct-${idx}" ${oIdx === q.correct_option_index ? "checked" : ""}
                  onchange="generatedQuestions[${idx}].correct_option_index = ${oIdx}; renderStudioQuestions()"
                  class="accent-emerald-500" title="Mark as correct option" />
                <span class="text-xs font-bold text-zinc-400">${letters[oIdx]}.</span>
                <input type="text" value="${escapeHtml(opt)}" onchange="generatedQuestions[${idx}].options[${oIdx}] = this.value"
                  class="w-full bg-transparent text-xs text-zinc-200 focus:outline-none" />
              </div>
            `
            )
            .join("")}
        </div>
      </div>
    `
    )
    .join("");
}

function addStudioQuestion() {
  const nextNum = generatedQuestions.length + 1;
  generatedQuestions.push({
    question_id: nextNum,
    question: `Custom Question #${nextNum}`,
    options: ["Choice A", "Choice B", "Choice C", "Choice D"],
    correct_option_index: 0,
    reference_answer: "A) Choice A",
    option_explanations: [
      "Correct! Choice A is the right answer.",
      "Incorrect — Choice B is not right.",
      "Incorrect — Choice C is not right.",
      "Incorrect — Choice D is not right.",
    ],
    max_marks: 2,
    time_limit: 20,
  });
  renderStudioQuestions();
}

function removeStudioQuestion(idx) {
  generatedQuestions.splice(idx, 1);
  generatedQuestions.forEach((q, i) => (q.question_id = i + 1));
  renderStudioQuestions();
}

async function publishQuiz() {
  if (!generatedQuestions.length) return;

  const letters = ["A", "B", "C", "D"];
  generatedQuestions.forEach((q, i) => {
    q.question_id = i + 1;
    const cIdx = q.correct_option_index ?? 0;
    const rightOpt = q.options[cIdx] || "Option A";
    q.reference_answer = `${letters[cIdx]}) ${rightOpt}`;
    q.option_explanations = q.options.map((opt, oIdx) =>
      oIdx === cIdx
        ? `Correct! '${rightOpt}' is the right answer.`
        : `Incorrect — '${opt}' is not right. The correct answer is ${letters[cIdx]}: ${rightOpt}.`
    );
  });

  const title = document.getElementById("quiz-title").value.trim();
  const sourceText = document.getElementById("quiz-source").value.trim();
  const difficulty = document.getElementById("quiz-difficulty").value;
  const provider = document.getElementById("quiz-provider").value;
  const hostName = (document.getElementById("host-name")?.value || "Instructor").trim() || "Instructor";

  const pubBtn = document.getElementById("btn-publish-quiz");
  if (pubBtn) {
    pubBtn.disabled = true;
    pubBtn.innerText = "Opening Lobby...";
  }

  try {
    const data = await safeFetchJson("/api/quizzes/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        source_text: sourceText,
        difficulty,
        provider,
        host_name: hostName,
        questions: generatedQuestions,
        bots: Array.from(selectedBots),
        reserved_pin: reservedPin,
      }),
    });

    const activePin = data.game_pin;
    document.getElementById("lobby-pin").innerText = activePin;
    document.getElementById("lobby-quiz-title").innerText = title;
    document.getElementById("lobby-join-url").innerText = `${location.origin}/`;

    connectHostWebSocket(activePin);
    showView("view-lobby");
  } catch (err) {
    alert("Publish Error: " + err.message);
  } finally {
    if (pubBtn) {
      pubBtn.disabled = false;
      pubBtn.innerText = "Open Lobby for Participants →";
    }
  }
}

function connectHostWebSocket(pin) {
  if (hostWs) hostWs.close();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  hostWs = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(pin)}/host`);

  hostWs.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === "LOBBY_UPDATE") {
      const players = msg.players || [];
      document.getElementById("lobby-player-count").innerText = `${players.length} Joined`;
      document.getElementById("lobby-players-list").innerHTML = players.length
        ? players
            .map(
              (p) => `
              <div class="px-3.5 py-2 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center gap-2 text-xs">
                <span class="text-base">${p.avatar}</span>
                <span class="font-semibold text-zinc-100">${escapeHtml(p.name)}</span>
                <span class="text-zinc-500">(${escapeHtml(p.student_id)})</span>
              </div>
            `
            )
            .join("")
        : `<p class="text-xs text-zinc-500">Waiting for participants to join with PIN ${pin}...</p>`;
    }

    if (msg.type === "QUESTION_START") {
      const letters = ["A", "B", "C", "D"];
      showView("view-question");
      document.getElementById("q-progress").innerText = `Question ${msg.question_number} of ${msg.total_questions}`;
      document.getElementById("q-marks").innerText = `${msg.max_marks} Marks`;
      document.getElementById("q-text").innerText = msg.question;
      document.getElementById("host-options-preview").innerHTML = (msg.options || [])
        .map(
          (opt, i) => `
          <div class="p-3.5 rounded-xl bg-zinc-900/90 border border-zinc-700/80 flex items-center gap-3 text-sm">
            <span class="w-7 h-7 rounded-lg bg-violet-600/30 border border-violet-500/40 text-violet-300 font-display font-bold flex items-center justify-center shrink-0">${letters[i]}</span>
            <span>${escapeHtml(opt)}</span>
          </div>
        `
        )
        .join("");
      document.getElementById("host-answer-counter").innerText = `0 / ${msg.total_participants}`;
      document.getElementById("host-answer-bar").style.width = "0%";
    }

    if (msg.type === "TIMER_TICK") {
      document.getElementById("host-timer-text").innerText = `${msg.remaining}s`;
    }

    if (msg.type === "ANSWER_COUNT") {
      document.getElementById("host-answer-counter").innerText = `${msg.answered} / ${msg.total}`;
      const pct = msg.total > 0 ? Math.round((msg.answered / msg.total) * 100) : 0;
      document.getElementById("host-answer-bar").style.width = `${pct}%`;
    }

    if (msg.type === "AUTO_NEXT_TICK") {
      document.getElementById("host-auto-next-label").innerText =
        `⏭️ Auto-advancing to next question in ${msg.remaining}s...`;
    }

    if (msg.type === "ROUND_RESULT") {
      showView("view-result");
      document.getElementById("host-res-ref").innerText = msg.reference_answer;
      document.getElementById("host-auto-next-label").innerText = msg.is_last_question
        ? `🏆 Showing final podium in ${msg.auto_next_seconds}s...`
        : `⏭️ Auto-advancing to next question in ${msg.auto_next_seconds}s...`;

      const banterEl = document.getElementById("host-bot-banter");
      if (msg.bot_banter) {
        banterEl.innerText = msg.bot_banter;
        banterEl.classList.remove("hidden");
      } else {
        banterEl.classList.add("hidden");
      }

      document.getElementById("host-round-leaderboard").innerHTML = (msg.leaderboard || [])
        .map(
          (p) => `
          <div class="flex items-center justify-between px-4 py-2.5 rounded-xl bg-zinc-900 border border-zinc-800 text-sm">
            <div class="flex items-center gap-2.5">
              <span class="font-display font-bold text-zinc-400 w-6">#${p.rank}</span>
              <span>${p.avatar}</span>
              <span class="font-semibold">${escapeHtml(p.name)}</span>
            </div>
            <span class="font-display font-bold text-cyan-400">${p.score} pts</span>
          </div>
        `
        )
        .join("");

      document.getElementById("host-breakdown-list").innerHTML = (msg.breakdown || [])
        .map(
          (row) => `
          <div class="flex items-center justify-between px-3.5 py-2 rounded-xl bg-zinc-950 border ${
            row.is_correct ? "border-emerald-500/30" : "border-rose-500/30"
          } text-xs">
            <span>${row.avatar} <strong>${escapeHtml(row.name)}</strong>: ${escapeHtml(row.submitted_answer)}</span>
            <span class="${row.is_correct ? "text-emerald-400" : "text-rose-400"} font-bold">
              ${row.is_correct ? "✅ Correct" : "❌ Wrong"}
            </span>
          </div>
        `
        )
        .join("");
    }

    if (msg.type === "PODIUM") {
      showView("view-podium");
      document.getElementById("host-podium-list").innerHTML = (msg.leaderboard || [])
        .map(
          (p, idx) => `
          <div class="flex items-center justify-between px-4 py-3 rounded-2xl ${
            idx === 0 ? "bg-violet-600/25 border border-violet-500/50" : "bg-zinc-900 border border-zinc-800"
          }">
            <div class="flex items-center gap-3">
              <span class="font-display font-bold text-zinc-400 w-7">#${p.rank}</span>
              <span class="text-xl">${p.avatar}</span>
              <div>
                <div class="font-bold text-sm">${escapeHtml(p.name)}</div>
                <div class="text-[11px] text-zinc-400">${escapeHtml(p.student_id)}</div>
              </div>
            </div>
            <span class="font-display font-bold text-cyan-400">${p.score} pts</span>
          </div>
        `
        )
        .join("");
      if (window.confetti) confetti({ particleCount: 90, spread: 75, origin: { y: 0.6 } });
    }
  };
}

function sendHostAction(action) {
  if (hostWs && hostWs.readyState === WebSocket.OPEN) {
    hostWs.send(JSON.stringify({ action }));
  }
}

async function loadSystemStatus() {
  try {
    const status = await safeFetchJson("/api/system/status");
    const badge = document.getElementById("db-status-badge");
    if (badge) badge.innerText = `DB: ${status.database}`;
  } catch (_) {}
}

async function loadHostHistory() {
  const container = document.getElementById("host-history-list");
  if (!container) return;
  container.innerHTML = `<p class="text-xs text-zinc-400 animate-pulse">Loading past matches...</p>`;
  try {
    const data = await safeFetchJson("/api/host/history");
    const sessions = data.sessions || [];
    container.innerHTML = sessions.length
      ? sessions
          .map(
            (s) => `
            <div class="p-3.5 rounded-2xl bg-zinc-950 border border-zinc-800 flex items-center justify-between gap-3 text-xs">
              <div>
                <div class="font-semibold text-sm text-zinc-100">${escapeHtml(s.title || "ZapIt Quiz")} <span class="text-cyan-400 font-mono">(PIN: ${s.game_pin})</span></div>
                <div class="text-zinc-500 mt-0.5">${s.status} • ${escapeHtml(s.model_label || "")}</div>
              </div>
              <button type="button" onclick="viewMatchReport('${s.game_pin}')" class="px-3.5 py-2 rounded-xl bg-zinc-800 hover:bg-zinc-700 text-cyan-300 font-semibold shrink-0">
                View Report
              </button>
            </div>
          `
          )
          .join("")
      : `<p class="text-xs text-zinc-500">No past sessions recorded yet.</p>`;
  } catch (err) {
    container.innerHTML = `<p class="text-xs text-rose-400">Could not load history: ${escapeHtml(err.message)}</p>`;
  }
}

async function viewMatchReport(pin) {
  try {
    const report = await safeFetchJson(`/api/host/reports/${encodeURIComponent(pin)}`);
    const modal = document.getElementById("report-modal");
    const body = document.getElementById("report-modal-body");
    body.innerHTML = `
      <div class="text-xs text-zinc-400 mb-3">Storage: ${report.storage} • Status: ${report.room.status}</div>
      <div class="space-y-2 max-h-80 overflow-y-auto">
        ${(report.evaluations || [])
          .map(
            (ev) => `
            <div class="p-3 rounded-xl bg-zinc-900 border border-zinc-800 text-xs">
              <strong>Q${ev.question_id} — ${escapeHtml(ev.player_name)} (${escapeHtml(ev.student_id)}):</strong>
              ${escapeHtml(ev.submitted_answer)} • <span class="text-cyan-400">${ev.round_score} pts</span>
            </div>
          `
          )
          .join("")}
      </div>
    `;
    modal.classList.remove("hidden");
  } catch (err) {
    alert("Report Error: " + err.message);
  }
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

loadSystemStatus();