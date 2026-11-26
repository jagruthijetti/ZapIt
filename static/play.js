let ws = null;
let activeProvider = "ollama";
let myPlayerId = null;
let soundEnabled = true;
let lockedOptionIdx = null;
let currentView = "view-join";
let syncRequested = false;
let activePin = "";
let lobbyPollTimer = null;

const MCQ_STYLES = [
  { letter: "A", border: "border-rose-500/40 hover:border-rose-400", bg: "bg-rose-500/10", badge: "bg-rose-500 text-white" },
  { letter: "B", border: "border-cyan-500/40 hover:border-cyan-400", bg: "bg-cyan-500/10", badge: "bg-cyan-500 text-zinc-950" },
  { letter: "C", border: "border-amber-500/40 hover:border-amber-400", bg: "bg-amber-500/10", badge: "bg-amber-400 text-zinc-950" },
  { letter: "D", border: "border-violet-500/40 hover:border-violet-400", bg: "bg-violet-500/10", badge: "bg-violet-500 text-white" },
];

let audioCtx = null;

function playTone(freq, duration = 0.12) {
  if (!soundEnabled) return;
  try {
    if (!audioCtx) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (AudioContextClass) audioCtx = new AudioContextClass();
    }
    if (!audioCtx) return;
    if (audioCtx.state === "suspended") audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + duration);
  } catch (_) {}
}

function toggleSound() {
  soundEnabled = !soundEnabled;
  document.getElementById("sound-toggle").innerText = soundEnabled ? "🔊 Sound On" : "🔇 Muted";
}

function showView(viewId) {
  currentView = viewId;
  if (viewId !== "view-lobby" && lobbyPollTimer) {
    clearInterval(lobbyPollTimer);
    lobbyPollTimer = null;
  }

  ["view-join", "view-lobby", "view-question", "view-result", "view-podium"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.classList.add("hidden");
  });

  const target = document.getElementById(viewId);
  if (target) {
    target.classList.remove("hidden");
    if (window.gsap) {
      gsap.fromTo(target, { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.35, ease: "power3.out" });
    }
  }
}

function showJoinError(message) {
  const box = document.getElementById("join-error-box");
  if (!box) return;
  if (!message) {
    box.classList.add("hidden");
    box.innerText = "";
  } else {
    box.classList.remove("hidden");
    box.innerText = message;
  }
}

function renderQuestionView(msg) {
  lockedOptionIdx = null;
  syncRequested = false;
  showView("view-question");
  document.getElementById("q-progress").innerText = `Question ${msg.question_number} of ${msg.total_questions}`;
  document.getElementById("q-marks").innerText = `${msg.max_marks} Marks`;
  document.getElementById("q-text").innerText = msg.question;
  document.getElementById("answer-locked-banner").classList.add("hidden");

  if (typeof msg.time_limit === "number") {
    document.getElementById("timer-text").innerText = msg.time_limit;
    const ring = document.getElementById("timer-ring");
    if (ring) {
      ring.style.strokeDashoffset = 0;
      ring.style.stroke = "#06b6d4";
    }
  }

  const grid = document.getElementById("mcq-options-grid");
  grid.innerHTML = (msg.options || [])
    .map((optText, idx) => {
      const st = MCQ_STYLES[idx] || MCQ_STYLES[0];
      return `
        <button id="mcq-btn-${idx}" type="button" onclick="selectMcqOption(${idx})"
          class="p-4 rounded-2xl ${st.bg} border ${st.border} text-left flex items-start gap-3 transition transform hover:-translate-y-0.5">
          <span class="w-8 h-8 rounded-xl ${st.badge} font-display font-bold flex items-center justify-center shrink-0 text-sm">
            ${st.letter}
          </span>
          <span class="text-sm sm:text-base font-medium text-zinc-100 leading-snug pt-0.5">${escapeHtml(optText)}</span>
        </button>
      `;
    })
    .join("");

  playTone(587.33, 0.18);
}

async function joinRoom() {
  showJoinError("");

  if (location.protocol === "file:") {
    showJoinError("Please open http://localhost:8000 in your browser instead of opening the HTML file directly.");
    return;
  }

  const pin = document.getElementById("join-pin").value.trim();
  const name = document.getElementById("join-name").value.trim();
  const sid = document.getElementById("join-sid").value.trim();

  if (pin.length !== 6) {
    showJoinError("Please enter a valid 6-digit Quiz PIN.");
    return;
  }
  if (!name || !sid) {
    showJoinError("Please enter both your Name and Student ID.");
    return;
  }

  const btn = document.getElementById("btn-join-zapit");
  btn.disabled = true;
  btn.innerText = "Checking PIN...";

  try {
    const checkRes = await fetch(`/api/rooms/${encodeURIComponent(pin)}/check`);
    if (!checkRes.ok) {
      const errData = await checkRes.json().catch(() => ({}));
      showJoinError(errData.detail || `Unable to join PIN ${pin} (HTTP ${checkRes.status}).`);
      btn.disabled = false;
      btn.innerText = "Join ZapIt ⚡";
      return;
    }
  } catch (_) {
    showJoinError("Cannot reach the ZapIt server. Verify the server is running and you are on the same network.");
    btn.disabled = false;
    btn.innerText = "Join ZapIt ⚡";
    return;
  }

  btn.innerText = "Entering Arena...";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(
    `${proto}://${location.host}/ws/${encodeURIComponent(pin)}/player?name=${encodeURIComponent(name)}&student_id=${encodeURIComponent(sid)}`
  );
  ws = socket;
  activePin = pin;

  if (lobbyPollTimer) clearInterval(lobbyPollTimer);
  lobbyPollTimer = setInterval(async () => {
    if (currentView !== "view-lobby" || !activePin) return;
    try {
      const res = await fetch(`/api/rooms/${encodeURIComponent(activePin)}/state`);
      if (!res.ok) return;
      const st = await res.json();
      if (st.status === "QUESTION" && currentView === "view-lobby") {
        renderQuestionView(st);
      }
    } catch (_) {}
  }, 2000);

  socket.onopen = () => {
    btn.disabled = false;
    btn.innerText = "Join ZapIt ⚡";
    document.getElementById("my-display-name").innerText = `${name} (${sid})`;
    showView("view-lobby");
    playTone(523.25, 0.15);
  };

  socket.onerror = () => {
    btn.disabled = false;
    btn.innerText = "Join ZapIt ⚡";
    showJoinError("WebSocket connection failed. Ensure 'websockets' is installed and refresh.");
  };

  socket.onclose = (e) => {
    btn.disabled = false;
    btn.innerText = "Join ZapIt ⚡";
    if (lobbyPollTimer) {
      clearInterval(lobbyPollTimer);
      lobbyPollTimer = null;
    }
    if (e.code === 4003) {
      showJoinError("Invalid Quiz PIN or this match has already ended.");
      showView("view-join");
    } else if (e.code === 4006) {
      showJoinError("This quiz room has reached maximum capacity.");
      showView("view-join");
    } else if (e.code !== 1000) {
      showJoinError(`Disconnected from quiz room (code ${e.code}). Re-enter the PIN to rejoin.`);
      showView("view-join");
    }
  };

  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === "JOINED") {
      myPlayerId = msg.player_id;
      activeProvider = msg.provider;
      document.getElementById("my-avatar-badge").innerText = msg.avatar;
      document.getElementById("lobby-title").innerText = `${msg.title} • PIN ${pin}`;
      document.getElementById("player-engine-badge").innerText = msg.model_label;
    }

    if (msg.type === "QUESTION_START") {
      renderQuestionView(msg);
    }

    if (msg.type === "TIMER_TICK") {
      if (currentView === "view-lobby" && !syncRequested && socket.readyState === WebSocket.OPEN) {
        syncRequested = true;
        socket.send(JSON.stringify({ action: "SYNC_STATE" }));
        return;
      }

      document.getElementById("timer-text").innerText = msg.remaining;
      const offset = 175.9 * (1 - msg.remaining / msg.total);
      const ring = document.getElementById("timer-ring");
      ring.style.strokeDashoffset = offset;
      ring.style.stroke = msg.remaining <= 5 ? "#ef4444" : msg.remaining <= 10 ? "#f59e0b" : "#06b6d4";
      if (msg.remaining <= 5 && msg.remaining > 0) playTone(440, 0.06);
    }

    if (msg.type === "AUTO_NEXT_TICK") {
      document.getElementById("auto-next-label").innerText =
        `⏭️ Next question starting automatically in ${msg.remaining}s...`;
    }

    if (msg.type === "ROUND_RESULT") {
      showView("view-result");
      document.getElementById("res-ref-answer").innerText = msg.reference_answer;
      document.getElementById("auto-next-label").innerText = msg.is_last_question
        ? `🏆 Quiz complete! Unlocking final standings in ${msg.auto_next_seconds}s...`
        : `⏭️ Next question starting automatically in ${msg.auto_next_seconds}s...`;

      if (msg.your_feedback) {
        const fb = msg.your_feedback;
        const badge = document.getElementById("res-badge");
        badge.innerText = fb.is_correct ? "✅ Correct Answer!" : "❌ Incorrect Answer";
        badge.className = fb.is_correct
          ? "px-3.5 py-1.5 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-300"
          : "px-3.5 py-1.5 rounded-full text-xs font-bold bg-rose-500/20 text-rose-300";

        document.getElementById("res-tutoring").innerText = fb.micro_tutoring;

        if (fb.is_correct) {
          playTone(784, 0.25);
          if (window.confetti) confetti({ particleCount: 45, spread: 60, origin: { y: 0.7 } });
        }
      }
    }

    if (msg.type === "PODIUM") {
      showView("view-podium");
      document.getElementById("my-final-summary").innerText =
        `Your Final Rank: #${msg.your_rank} • Total Score: ${msg.your_score} pts`;
      renderLeaderboard("podium-list", msg.leaderboard);
      renderMistakesReview(msg.your_review || []);
      playTone(880, 0.35);
      if (window.confetti) confetti({ particleCount: 100, spread: 80, origin: { y: 0.6 } });
    }

    if (msg.type === "REMEDIATION_READY") {
      renderRemediationDeck(msg.deck, msg.model_used);
    }

    if (msg.type === "REMEDIATION_ERROR") {
      document.getElementById("remediation-loading").innerText = "Could not build remediation deck: " + msg.detail;
    }
  };
}

function selectMcqOption(idx) {
  if (lockedOptionIdx !== null || !ws || ws.readyState !== WebSocket.OPEN) return;
  lockedOptionIdx = idx;
  ws.send(JSON.stringify({ action: "SUBMIT_ANSWER", option_index: idx }));

  for (let i = 0; i < 4; i++) {
    const btn = document.getElementById(`mcq-btn-${i}`);
    if (!btn) continue;
    btn.disabled = true;
    if (i === idx) {
      btn.classList.add("ring-2", "ring-cyan-400", "scale-[1.02]");
    } else {
      btn.classList.add("opacity-40");
    }
  }
  document.getElementById("answer-locked-banner").classList.remove("hidden");
  playTone(659.25, 0.14);
}

function renderLeaderboard(containerId, list) {
  const el = document.getElementById(containerId);
  el.innerHTML = list
    .map(
      (p, idx) => `
      <div class="flex items-center justify-between px-4 py-2.5 rounded-2xl ${
        p.player_id === myPlayerId
          ? "bg-cyan-500/20 border border-cyan-400/60"
          : idx === 0
          ? "bg-violet-600/20 border border-violet-500/50"
          : "bg-zinc-900/80 border border-zinc-800"
      }">
        <div class="flex items-center gap-2.5">
          <span class="font-display font-bold w-7 text-zinc-400 text-xs">#${p.rank}</span>
          <span class="text-lg">${p.avatar}</span>
          <span class="font-semibold text-sm">${escapeHtml(p.name)} ${p.player_id === myPlayerId ? "(You)" : ""}</span>
        </div>
        <span class="font-display font-bold text-cyan-400 text-sm">${p.score} pts</span>
      </div>
    `
    )
    .join("");
}

function renderMistakesReview(reviewList) {
  const wrongCount = reviewList.filter((item) => !item.is_correct).length;
  document.getElementById("mistakes-count-badge").innerText =
    wrongCount === 0 ? "Perfect Score — 0 Mistakes!" : `${wrongCount} Mistake${wrongCount > 1 ? "s" : ""} to Review`;

  const container = document.getElementById("my-mistakes-list");
  container.innerHTML = reviewList
    .map(
      (item) => `
      <div class="p-4 rounded-2xl bg-zinc-950/90 border ${
        item.is_correct ? "border-emerald-500/30" : "border-rose-500/50"
      } space-y-2">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-bold text-zinc-400">Question #${item.question_number}</span>
          <span class="text-xs px-2.5 py-0.5 rounded-full font-bold ${
            item.is_correct ? "bg-emerald-500/20 text-emerald-300" : "bg-rose-500/20 text-rose-300"
          }">
            ${item.is_correct ? "✅ Correct" : "❌ Mistake"}
          </span>
        </div>
        <p class="font-semibold text-sm text-zinc-100">${escapeHtml(item.question)}</p>
        <div class="text-xs space-y-1 pt-1">
          <div class="${item.is_correct ? "text-emerald-300" : "text-rose-300"}">
            <strong>Your Choice:</strong> ${escapeHtml(item.submitted_answer)}
          </div>
          ${
            !item.is_correct
              ? `<div class="text-emerald-300"><strong>Correct Answer:</strong> ${escapeHtml(item.reference_answer)}</div>`
              : ""
          }
          <div class="text-zinc-300 pt-1"><strong>Explanation:</strong> ${escapeHtml(item.micro_tutoring)}</div>
        </div>
      </div>
    `
    )
    .join("");
}

function renderRemediationDeck(deck, modelUsed) {
  document.getElementById("remediation-loading").classList.add("hidden");
  document.getElementById("remediation-content").classList.remove("hidden");
  document.getElementById("remediation-model-tag").innerText = `Built by ${modelUsed}`;
  document.getElementById("remediation-summary").innerText = deck.weak_concepts_summary;

  document.getElementById("flashcards-grid").innerHTML = (deck.flashcards || [])
    .map(
      (card) => `
      <div class="flashcard-scene" onclick="this.classList.toggle('is-flipped'); playTone(520, 0.08)">
        <div class="flashcard-inner">
          <div class="flashcard-face flashcard-front">
            <span class="text-[11px] uppercase tracking-wider text-violet-400 font-bold">${escapeHtml(card.concept_title)}</span>
            <p class="font-semibold text-sm text-zinc-100 leading-snug">${escapeHtml(card.front_prompt)}</p>
            <span class="text-[11px] text-zinc-400">Tap to flip ↻</span>
          </div>
          <div class="flashcard-face flashcard-back">
            <p class="text-xs text-zinc-100 leading-relaxed">${escapeHtml(card.back_explanation)}</p>
            <div class="p-2 rounded-lg bg-cyan-500/10 border border-cyan-500/30 text-[11px] text-cyan-200">
              💡 Hook: ${escapeHtml(card.memory_hook)}
            </div>
          </div>
        </div>
      </div>
    `
    )
    .join("");

  document.getElementById("redemption-list").innerHTML = (deck.redemption_questions || [])
    .map(
      (rq, idx) => `
      <div class="p-4 rounded-2xl bg-zinc-950 border border-zinc-800 space-y-2.5">
        <div class="text-sm font-semibold text-zinc-100">${idx + 1}. ${escapeHtml(rq.question)}</div>
        <div class="text-xs text-zinc-400">Hint: ${escapeHtml(rq.hint)}</div>
        <div class="flex gap-2">
          <input id="red-ans-${idx}" type="text" placeholder="Type your redemption answer..."
            class="flex-1 px-3 py-2 rounded-xl bg-zinc-900 border border-zinc-700 text-xs focus:outline-none focus:border-cyan-400" />
          <button type="button" onclick="checkRedemption(${idx}, '${encodeURIComponent(rq.question)}', '${encodeURIComponent(rq.reference_answer)}')"
            class="px-4 py-2 rounded-xl bg-violet-600 hover:bg-violet-500 text-xs font-semibold">
            Verify with AI
          </button>
        </div>
        <div id="red-feedback-${idx}" class="hidden text-xs p-2.5 rounded-xl bg-zinc-900 border border-zinc-700"></div>
      </div>
    `
    )
    .join("");
}

async function checkRedemption(idx, encQ, encRef) {
  const student_answer = document.getElementById(`red-ans-${idx}`).value.trim();
  if (!student_answer) return;
  const fbBox = document.getElementById(`red-feedback-${idx}`);
  fbBox.classList.remove("hidden");
  fbBox.innerText = "⏳ AI Judge verifying...";

  const res = await fetch("/api/redemption/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: activeProvider,
      question: decodeURIComponent(encQ),
      reference_answer: decodeURIComponent(encRef),
      student_answer,
    }),
  });
  const data = await res.json();
  const ev = data.evaluation;
  fbBox.innerHTML = `<strong class="${ev.awarded_marks >= 2 ? "text-emerald-400" : "text-amber-300"}">${ev.awarded_marks}/2 Marks:</strong> ${escapeHtml(ev.micro_tutoring)}`;
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

if (window.gsap) {
  gsap.from("#view-join", { opacity: 0, y: 20, duration: 0.45, ease: "power3.out" });
}
(function prefillPinFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const urlPin = (params.get("pin") || "").replace(/\D/g, "").slice(0, 6);
  if (urlPin) {
    const pinInput = document.getElementById("join-pin");
    const nameInput = document.getElementById("join-name");
    if (pinInput) {
      pinInput.value = urlPin;
      pinInput.classList.add("border-cyan-400", "ring-2", "ring-cyan-500/30");
    }
    if (nameInput) {
      nameInput.focus();
    }
  }
})();