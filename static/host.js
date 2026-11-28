let hostWs = null;
let hostStateWatchdog = null;
let currentActivePin = "";
let reservedPin = null;
let generatedQuestions = [];
let selectedBots = new Set();
let genTimerInterval = null;
let currentJoinUrl = "";
let authMode = "login"; // "login" | "register"
let hostToken = localStorage.getItem("zapit_host_token") || "";
let currentHost = null;

const TIMER_OPTIONS = [10, 15, 20, 30, 45, 60, 90];

const ALL_HOST_VIEWS = [
  "view-auth",
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
  const headers = { ...(options.headers || {}) };
  if (hostToken) {
    headers["Authorization"] = `Bearer ${hostToken}`;
  }
  const res = await fetch(url, { ...options, headers });
  const rawText = await res.text();
  let data = {};
  try {
    data = rawText ? JSON.parse(rawText) : {};
  } catch (_) {
    throw new Error(rawText || `Server error (HTTP ${res.status})`);
  }
  if (res.status === 401 && !url.startsWith("/api/auth/")) {
    logoutHost();
    throw new Error(data.detail || "Session expired. Please sign in again.");
  }
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (HTTP ${res.status})`);
  }
  return data;
}

// ----------------- HOST AUTHENTICATION UI -----------------

function switchAuthTab(mode) {
  authMode = mode;
  const loginTab = document.getElementById("tab-auth-login");
  const regTab = document.getElementById("tab-auth-register");
  const nameGroup = document.getElementById("auth-name-group");
  const submitBtn = document.getElementById("btn-auth-submit");
  const errBox = document.getElementById("auth-error-box");

  if (errBox) errBox.classList.add("hidden");

  if (mode === "register") {
    loginTab?.classList.remove("bg-violet-600", "text-white");
    loginTab?.classList.add("text-zinc-400");
    regTab?.classList.add("bg-violet-600", "text-white");
    regTab?.classList.remove("text-zinc-400");
    nameGroup?.classList.remove("hidden");
    if (submitBtn) submitBtn.innerText = "Create Account & Enter Studio →";
  } else {
    regTab?.classList.remove("bg-violet-600", "text-white");
    regTab?.classList.add("text-zinc-400");
    loginTab?.classList.add("bg-violet-600", "text-white");
    loginTab?.classList.remove("text-zinc-400");
    nameGroup?.classList.add("hidden");
    if (submitBtn) submitBtn.innerText = "Sign In to Host Studio →";
  }
}

async function submitHostAuth(event) {
  if (event) event.preventDefault();
  const errBox = document.getElementById("auth-error-box");
  const submitBtn = document.getElementById("btn-auth-submit");
  if (errBox) errBox.classList.add("hidden");

  const email = (document.getElementById("auth-email")?.value || "").trim();
  const password = document.getElementById("auth-password")?.value || "";
  const name = (document.getElementById("auth-name")?.value || "").trim();

  if (!email || !password) {
    if (errBox) {
      errBox.innerText = "Please enter both email address and password.";
      errBox.classList.remove("hidden");
    }
    return;
  }

  if (authMode === "register" && name.length < 2) {
    if (errBox) {
      errBox.innerText = "Please enter your name (at least 2 characters).";
      errBox.classList.remove("hidden");
    }
    return;
  }

  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerText = authMode === "register" ? "Creating Account..." : "Signing In...";
  }

  try {
    const endpoint = authMode === "register" ? "/api/auth/register" : "/api/auth/login";
    const body = authMode === "register" ? { name, email, password } : { email, password };

    const data = await safeFetchJson(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    hostToken = data.token;
    localStorage.setItem("zapit_host_token", hostToken);
    onHostAuthenticated(data.host);
  } catch (err) {
    if (errBox) {
      errBox.innerText = err.message;
      errBox.classList.remove("hidden");
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerText =
        authMode === "register" ? "Create Account & Enter Studio →" : "Sign In to Host Studio →";
    }
  }
}

function onHostAuthenticated(host) {
  currentHost = host;
  const authNav = document.getElementById("host-auth-nav");
  const userBadge = document.getElementById("host-user-badge");
  const welcomeTag = document.getElementById("home-welcome-tag");
  const hostNameInput = document.getElementById("host-name");

  if (authNav) authNav.classList.remove("hidden");
  if (userBadge) userBadge.innerText = `👩‍🏫 ${host.name} (${host.email})`;
  if (welcomeTag) welcomeTag.innerText = `Welcome, ${host.name}`;
  if (hostNameInput && host.name) hostNameInput.value = host.name;

  showView("view-home");
}

function logoutHost() {
  hostToken = "";
  currentHost = null;
  localStorage.removeItem("zapit_host_token");
  if (hostStateWatchdog) {
    clearInterval(hostStateWatchdog);
    hostStateWatchdog = null;
  }
  if (hostWs) {
    hostWs.close();
    hostWs = null;
  }
  const authNav = document.getElementById("host-auth-nav");
  if (authNav) authNav.classList.add("hidden");
  showView("view-auth");
}

async function restoreHostSession() {
  if (!hostToken) {
    showView("view-auth");
    return;
  }
  try {
    const data = await safeFetchJson("/api/auth/me");
    onHostAuthenticated(data.host);
  } catch (_) {
    logoutHost();
  }
}

function navigateHostHome() {
  if (hostStateWatchdog) {
    clearInterval(hostStateWatchdog);
    hostStateWatchdog = null;
  }
  if (!currentHost) {
    showView("view-auth");
  } else {
    showView("view-home");
  }
}

// ----------------- NAVIGATION & VIEW HELPERS -----------------

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
  if (!currentHost) {
    showView("view-auth");
    return;
  }
  showGenError("");
  showView("view-create");
}

function openHistoryView() {
  if (!currentHost) {
    showView("view-auth");
    return;
  }
  showView("view-history");
  loadHostHistory();
}

function updateWordCount() {
  const txt = (document.getElementById("quiz-source")?.value || "").trim();
  const words = txt ? txt.split(/\s+/).length : 0;
  const badge = document.getElementById("word-count-badge");
  if (badge) badge.innerText = `${words} words`;
}

async function handleNotesFileUpload(event) {
  const file = event.target.files?.[0];
  if (!file) return;

  showGenError("");
  const nameLabel = document.getElementById("uploaded-file-name");
  const metaLabel = document.getElementById("uploaded-file-meta");
  const sourceBox = document.getElementById("quiz-source");
  const titleInput = document.getElementById("quiz-title");

  if (nameLabel) nameLabel.innerText = `⏳ Extracting text from ${file.name}...`;
  if (metaLabel) metaLabel.innerText = "Reading slides, paragraphs, and tables...";

  try {
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch("/api/documents/extract", {
      method: "POST",
      headers: hostToken ? { Authorization: `Bearer ${hostToken}` } : {},
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || `Upload failed (HTTP ${res.status})`);
    }

    if (sourceBox) {
      sourceBox.value = data.extracted_text;
      updateWordCount();
    }
    if (titleInput && !titleInput.value.trim()) {
      titleInput.value = file.name.replace(/\.[^/.]+$/, "").replace(/[_-]+/g, " ");
    }

    if (nameLabel) nameLabel.innerText = `✅ Loaded: ${data.filename}`;
    if (metaLabel) {
      metaLabel.innerText = `${data.char_count.toLocaleString()} characters extracted — questions will strictly follow these notes`;
    }
  } catch (err) {
    if (nameLabel) nameLabel.innerText = "Upload Class Notes (.pptx, .docx, .pdf, .txt)";
    if (metaLabel) metaLabel.innerText = "AI will generate questions strictly from your uploaded slides/notes";
    showGenError("Document Upload Error: " + err.message);
  } finally {
    event.target.value = "";
  }
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

  const titleEl = document.getElementById("quiz-title");
  const sourceEl = document.getElementById("quiz-source");
  const countEl = document.getElementById("quiz-count");
  const diffEl = document.getElementById("quiz-difficulty");
  const provEl = document.getElementById("quiz-provider");
  const hostEl = document.getElementById("host-name");
  const timerEl = document.getElementById("quiz-default-timer");

  const title = (titleEl?.value || "").trim();
  const sourceText = (sourceEl?.value || "").trim();
  const questionCount = parseInt(countEl?.value || "5", 10) || 5;
  const difficulty = diffEl?.value || "Medium";
  const provider = provEl?.value || "gemini";
  const hostName = (hostEl?.value || currentHost?.name || "Instructor").trim() || "Instructor";
  const defaultTimeLimit = parseInt(timerEl?.value || "20", 10) || 20;

  if (title.length < 2) {
    showGenError("Please enter a Quiz Title (at least 2 characters).");
    return;
  }
  if (sourceText.length < 20) {
    showGenError("Please paste at least 20 characters of class notes or upload a document.");
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
        default_time_limit: defaultTimeLimit,
      }),
    });

    clearInterval(genTimerInterval);
    generatedQuestions = (data.questions || []).map((q) => ({
      ...q,
      time_limit: parseInt(q.time_limit || defaultTimeLimit, 10) || defaultTimeLimit,
    }));

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
    .map((q, idx) => {
      const currentLimit = parseInt(q.time_limit || "20", 10);
      const timerSelectHtml = TIMER_OPTIONS.map(
        (sec) => `<option value="${sec}" ${sec === currentLimit ? "selected" : ""}>${sec}s</option>`
      ).join("");

      return `
      <div class="p-4 rounded-2xl bg-zinc-950/85 border border-zinc-800 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="text-xs font-bold text-violet-400">MCQ #${idx + 1}</span>
          <div class="flex items-center gap-3">
            <label class="flex items-center gap-1.5 text-xs text-cyan-300 bg-zinc-900 px-2.5 py-1 rounded-lg border border-zinc-700">
              <span>⏱️ Timer:</span>
              <select onchange="generatedQuestions[${idx}].time_limit = parseInt(this.value, 10)"
                class="bg-transparent text-cyan-300 font-bold focus:outline-none cursor-pointer">
                ${timerSelectHtml}
              </select>
            </label>
            <button type="button" onclick="removeStudioQuestion(${idx})" class="text-xs text-rose-400 hover:underline">Remove</button>
          </div>
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
    `;
    })
    .join("");
}

function addStudioQuestion() {
  const nextNum = generatedQuestions.length + 1;
  const defaultLimit = parseInt(document.getElementById("quiz-default-timer")?.value || "20", 10) || 20;
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
    time_limit: defaultLimit,
  });
  renderStudioQuestions();
}

function removeStudioQuestion(idx) {
  generatedQuestions.splice(idx, 1);
  generatedQuestions.forEach((q, i) => (q.question_id = i + 1));
  renderStudioQuestions();
}

function renderLobbyQrCode(pin) {
  currentJoinUrl = `${location.origin}/?pin=${encodeURIComponent(pin)}`;
  const qrBox = document.getElementById("lobby-qr-box");
  if (!qrBox) return;
  qrBox.innerHTML = "";

  if (window.QRCode) {
    new QRCode(qrBox, {
      text: currentJoinUrl,
      width: 132,
      height: 132,
      colorDark: "#09090b",
      colorLight: "#ffffff",
      correctLevel: QRCode.CorrectLevel.M,
    });
  } else {
    qrBox.innerHTML = `<span class="text-xs text-zinc-800 font-mono p-2">${pin}</span>`;
  }
}

async function copyLobbyJoinLink() {
  if (!currentJoinUrl) return;
  const btn = document.getElementById("btn-copy-join-link");
  try {
    await navigator.clipboard.writeText(currentJoinUrl);
    if (btn) {
      btn.innerText = "✅ Link Copied!";
      setTimeout(() => (btn.innerText = "📋 Copy Direct Join Link"), 2000);
    }
  } catch (_) {
    prompt("Copy this direct join link:", currentJoinUrl);
  }
}

async function publishQuiz() {
  if (!generatedQuestions.length) return;

  const letters = ["A", "B", "C", "D"];
  generatedQuestions.forEach((q, i) => {
    q.question_id = i + 1;
    q.time_limit = Math.max(10, Math.min(90, parseInt(q.time_limit || "20", 10) || 20));
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
  const hostName = (document.getElementById("host-name")?.value || currentHost?.name || "Instructor").trim() || "Instructor";

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
    currentActivePin = activePin;
    document.getElementById("lobby-pin").innerText = activePin;
    document.getElementById("lobby-quiz-title").innerText = title;
    document.getElementById("lobby-join-url").innerText = `${location.origin}/`;
    renderLobbyQrCode(activePin);

    connectHostWebSocket(activePin);
    startHostStateWatchdog();
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

function renderHostPodium(board, totalQ) {
  showView("view-podium");
  const rows = Array.isArray(board) ? board : [];
  const qCount = totalQ || generatedQuestions.length || 0;
  const winnerBanner = document.getElementById("host-winner-banner");
  const podiumList = document.getElementById("host-podium-list");

  if (winnerBanner) {
    if (rows.length > 0) {
      const top = rows[0];
      winnerBanner.innerText = `🥇 Winner: ${top.avatar || "🏆"} ${top.name} — ${top.score || 0} pts`;
      winnerBanner.classList.remove("hidden");
    } else {
      winnerBanner.classList.add("hidden");
    }
  }

  if (podiumList) {
    const medals = ["🥇", "🥈", "🥉"];
    podiumList.innerHTML = rows.length
      ? rows
          .map((p, idx) => {
            const medal = medals[idx] || `#${p.rank}`;
            const rowStyle =
              idx === 0
                ? "bg-amber-500/15 border border-amber-400/60 shadow-lg shadow-amber-500/10"
                : idx === 1
                ? "bg-violet-600/20 border border-violet-500/40"
                : idx === 2
                ? "bg-cyan-500/15 border border-cyan-500/35"
                : "bg-zinc-900 border border-zinc-800";

            const correctBadge =
              qCount > 0 ? ` • ${p.correct_count || 0}/${qCount} Correct` : "";

            return `
              <div class="flex items-center justify-between px-4 py-3.5 rounded-2xl ${rowStyle}">
                <div class="flex items-center gap-3">
                  <span class="font-display font-bold text-lg w-8 text-center">${medal}</span>
                  <span class="text-2xl">${p.avatar || "🦊"}</span>
                  <div>
                    <div class="font-bold text-sm text-white">${escapeHtml(p.name)}</div>
                    <div class="text-[11px] text-zinc-400">${escapeHtml(p.student_id || "")}${correctBadge}</div>
                  </div>
                </div>
                <div class="text-right">
                  <div class="font-display font-bold text-base text-cyan-300">${p.score || 0} pts</div>
                  <div class="text-[10px] uppercase tracking-wider text-zinc-400">Rank #${p.rank}</div>
                </div>
              </div>
            `;
          })
          .join("")
      : `<p class="text-xs text-zinc-400 text-center py-4">No participant scores recorded for this match.</p>`;
  }

  if (window.confetti) confetti({ particleCount: 95, spread: 75, origin: { y: 0.6 } });
}

function startHostStateWatchdog() {
  if (hostStateWatchdog) clearInterval(hostStateWatchdog);
  hostStateWatchdog = setInterval(async () => {
    if (!currentActivePin) return;
    const podiumVisible = !document.getElementById("view-podium")?.classList.contains("hidden");
    if (podiumVisible) {
      clearInterval(hostStateWatchdog);
      return;
    }
    try {
      const state = await safeFetchJson(`/api/rooms/${encodeURIComponent(currentActivePin)}/state`);
      if (state.status === "COMPLETED" && Array.isArray(state.leaderboard)) {
        renderHostPodium(state.leaderboard, state.total_questions);
        clearInterval(hostStateWatchdog);
      }
    } catch (_) {}
  }, 2000);
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
      document.getElementById("host-timer-text").innerText = `${msg.time_limit}s`;
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
      if (hostStateWatchdog) clearInterval(hostStateWatchdog);
      renderHostPodium(msg.leaderboard || [], msg.total_questions || generatedQuestions.length);
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
      : `<p class="text-xs text-zinc-500">No past sessions recorded under your account yet.</p>`;
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

window.switchAuthTab = switchAuthTab;
window.submitHostAuth = submitHostAuth;
window.logoutHost = logoutHost;
window.navigateHostHome = navigateHostHome;
window.openCreateView = openCreateView;
window.openHistoryView = openHistoryView;
window.showView = showView;
window.removeStudioQuestion = removeStudioQuestion;
window.renderStudioQuestions = renderStudioQuestions;
window.viewMatchReport = viewMatchReport;

function bindHostEventListeners() {
  document.getElementById("brand-home-btn")?.addEventListener("click", navigateHostHome);
  document.getElementById("tab-auth-login")?.addEventListener("click", () => switchAuthTab("login"));
  document.getElementById("tab-auth-register")?.addEventListener("click", () => switchAuthTab("register"));
  document.getElementById("host-auth-form")?.addEventListener("submit", submitHostAuth);

  document.getElementById("nav-btn-create")?.addEventListener("click", openCreateView);
  document.getElementById("nav-btn-history")?.addEventListener("click", openHistoryView);
  document.getElementById("nav-btn-logout")?.addEventListener("click", logoutHost);

  document.getElementById("hub-btn-create")?.addEventListener("click", openCreateView);
  document.getElementById("hub-btn-history")?.addEventListener("click", openHistoryView);

  document.getElementById("create-back-btn")?.addEventListener("click", () => showView("view-home"));
  document.getElementById("quiz-source")?.addEventListener("input", updateWordCount);
  document.getElementById("notes-file-input")?.addEventListener("change", handleNotesFileUpload);
  document.getElementById("btn-generate-quiz")?.addEventListener("click", generateQuiz);

  document.querySelectorAll(".bot-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => toggleBot(btn.dataset.bot, btn));
  });

  document.getElementById("studio-back-btn")?.addEventListener("click", () => showView("view-create"));
  document.getElementById("studio-add-q-btn")?.addEventListener("click", addStudioQuestion);
  document.getElementById("btn-publish-quiz")?.addEventListener("click", publishQuiz);

  document.getElementById("btn-copy-join-link")?.addEventListener("click", copyLobbyJoinLink);
  document.getElementById("lobby-start-btn")?.addEventListener("click", () => sendHostAction("START_MATCH"));
  document.getElementById("q-end-early-btn")?.addEventListener("click", () => sendHostAction("END_ROUND_EARLY"));
  document.getElementById("btn-next-q")?.addEventListener("click", () => sendHostAction("NEXT_QUESTION"));

  document.getElementById("history-refresh-btn")?.addEventListener("click", loadHostHistory);
  document.getElementById("history-back-btn")?.addEventListener("click", () => showView("view-home"));
  document.getElementById("podium-history-btn")?.addEventListener("click", openHistoryView);
  document.getElementById("podium-create-btn")?.addEventListener("click", openCreateView);

  document.getElementById("close-report-modal-btn")?.addEventListener("click", () => {
    document.getElementById("report-modal")?.classList.add("hidden");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindHostEventListeners();
  loadSystemStatus();
  restoreHostSession();
});