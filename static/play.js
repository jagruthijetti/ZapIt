let playerWs = null;
let myPlayerId = "";
let myName = "";
let myAvatar = "🐼";
let currentPin = "";
let currentQuestionId = null;
let selectedOptionIndex = -1;
let answerLocked = false;
let currentOptions = [];
let stateSyncInterval = null;
let practiceQuizData = [];

const ALL_PLAYER_VIEWS = [
  "pview-join",
  "pview-lobby",
  "pview-question",
  "pview-result",
  "pview-podium",
];

function showPlayerView(viewId) {
  ALL_PLAYER_VIEWS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.classList.add("hidden");
  });
  const target = document.getElementById(viewId);
  if (target) {
    target.classList.remove("hidden");
    if (window.gsap) {
      gsap.fromTo(target, { opacity: 0, y: 14 }, { opacity: 1, y: 0, duration: 0.3, ease: "power3.out" });
    }
  }
}

function showJoinError(msg) {
  const box = document.getElementById("join-error-box");
  if (!box) return;
  if (!msg) {
    box.classList.add("hidden");
    box.innerText = "";
  } else {
    box.classList.remove("hidden");
    box.innerText = msg;
  }
}

function updateHudBadge() {
  const badge = document.getElementById("player-hud-badge");
  if (!badge) return;
  badge.classList.remove("hidden");
  badge.innerText = `${myAvatar} ${myName}`;
}

function fireSuccessConfetti() {
  if (!window.confetti) return;
  confetti({
    particleCount: 85,
    spread: 70,
    origin: { y: 0.6 },
  });
}

function startStateSyncWatchdog() {
  if (stateSyncInterval) clearInterval(stateSyncInterval);
  stateSyncInterval = setInterval(async () => {
    if (!currentPin || !myPlayerId) return;
    const podiumVisible = !document.getElementById("pview-podium")?.classList.contains("hidden");
    if (podiumVisible) {
      clearInterval(stateSyncInterval);
      return;
    }
    try {
      const res = await fetch(
        `/api/rooms/${encodeURIComponent(currentPin)}/state?player_id=${encodeURIComponent(myPlayerId)}`
      );
      if (!res.ok) return;
      const state = await res.json();
      if (state.status === "COMPLETED" && Array.isArray(state.leaderboard)) {
        const me = state.leaderboard.find((p) => p.player_id === myPlayerId);
        renderFinalPodium({
          leaderboard: state.leaderboard,
          your_summary: {
            rank: me ? me.rank : 1,
            total_players: state.leaderboard.length,
            score: me ? me.score : 0,
            correct_count: me ? me.correct_count : 0,
            total_questions: state.total_questions || 0,
          },
        });
      }
    } catch (_) {}
  }, 3000);
}

async function handleJoinSubmit(event) {
  if (event) event.preventDefault();
  showJoinError("");

  const pin = (document.getElementById("join-pin")?.value || "").trim();
  const name = (document.getElementById("join-name")?.value || "").trim();
  const sid = (document.getElementById("join-sid")?.value || "").trim();

  if (pin.length !== 6) {
    showJoinError("Please enter a valid 6-digit Game PIN.");
    return;
  }
  if (!name || !sid) {
    showJoinError("Please enter both your Name and Student ID.");
    return;
  }

  const btn = document.getElementById("btn-join-room");
  if (btn) {
    btn.disabled = true;
    btn.innerText = "Connecting...";
  }

  try {
    const checkRes = await fetch(`/api/rooms/${encodeURIComponent(pin)}/check`);
    const checkData = await checkRes.json();
    if (!checkRes.ok) {
      throw new Error(checkData.detail || "Unable to find room.");
    }
    if (checkData.status === "PREPARING") {
      throw new Error("Host is still generating questions for this PIN. Try again in 3 seconds!");
    }

    currentPin = pin;
    myName = name;
    connectPlayerWebSocket(pin, name, sid);
  } catch (err) {
    showJoinError(err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = "Enter Arena 🚀";
    }
  }
}

function connectPlayerWebSocket(pin, name, sid) {
  if (playerWs) {
    playerWs.close();
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${proto}://${location.host}/ws/${encodeURIComponent(pin)}/player?name=${encodeURIComponent(name)}&student_id=${encodeURIComponent(sid)}`;
  playerWs = new WebSocket(wsUrl);

  playerWs.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === "ERROR") {
      showJoinError(msg.detail || "Connection error.");
      showPlayerView("pview-join");
      return;
    }

    if (msg.type === "JOINED_OK") {
      myPlayerId = msg.player_id;
      myName = msg.name;
      myAvatar = msg.avatar || "🐼";
      updateHudBadge();
      startStateSyncWatchdog();

      document.getElementById("plobby-pin").innerText = pin;
      document.getElementById("plobby-avatar").innerText = myAvatar;
      document.getElementById("plobby-welcome").innerText = `Welcome, ${myName}!`;
      document.getElementById("plobby-title").innerText = `Quiz: ${msg.title} — Waiting for Host to start...`;

      if (msg.status === "LOBBY") {
        showPlayerView("pview-lobby");
      }
    }

    if (msg.type === "LOBBY_UPDATE") {
      const players = msg.players || [];
      const me = players.find((p) => p.player_id === myPlayerId);
      if (me && me.avatar) {
        myAvatar = me.avatar;
        const avatarEl = document.getElementById("plobby-avatar");
        if (avatarEl) avatarEl.innerText = myAvatar;
        updateHudBadge();
      }

      const countEl = document.getElementById("plobby-count");
      const listEl = document.getElementById("plobby-players");
      if (countEl) countEl.innerText = `${players.length} Joined`;
      if (listEl) {
        listEl.innerHTML = players
          .map(
            (p) => `
            <span class="px-3 py-1.5 rounded-xl bg-zinc-900 border ${
              p.player_id === myPlayerId ? "border-cyan-500/50 text-cyan-200" : "border-zinc-800 text-zinc-200"
            } text-xs font-medium">
              ${escapeHtml(p.avatar || "🐼")} ${escapeHtml(p.name)}${p.player_id === myPlayerId ? " (You)" : ""}
            </span>
          `
          )
          .join("");
      }
    }

    if (msg.type === "QUESTION_START") {
      currentQuestionId = msg.question_id;
      selectedOptionIndex = -1;
      answerLocked = false;
      currentOptions = msg.options || [];

      showPlayerView("pview-question");
      document.getElementById("pq-progress").innerText = `Question ${msg.question_number} of ${msg.total_questions}`;
      document.getElementById("pq-timer").innerText = `${msg.time_limit}s`;
      document.getElementById("pq-marks").innerText = `${msg.max_marks} Marks`;
      document.getElementById("pq-text").innerText = msg.question;
      document.getElementById("pq-locked-banner").classList.add("hidden");

      renderPlayerOptions(currentOptions, -1, false);
    }

    if (msg.type === "TIMER_TICK") {
      const timerEl = document.getElementById("pq-timer");
      if (timerEl) timerEl.innerText = `${msg.remaining}s`;
    }

    if (msg.type === "ANSWER_LOCKED") {
      answerLocked = true;
      const letters = ["A", "B", "C", "D"];
      const chosenText =
        selectedOptionIndex >= 0 && selectedOptionIndex < currentOptions.length
          ? `${letters[selectedOptionIndex]}) ${currentOptions[selectedOptionIndex]}`
          : "Your answer";
      document.getElementById("pq-locked-banner").classList.remove("hidden");
      document.getElementById("pq-locked-choice").innerText = `Selected: ${chosenText} — waiting for round results...`;
      renderPlayerOptions(currentOptions, selectedOptionIndex, true);
    }

    if (msg.type === "AUTO_NEXT_TICK") {
      const cdEl = document.getElementById("pres-next-countdown");
      if (cdEl) cdEl.innerText = `Next question in ${msg.remaining}s...`;
    }

    if (msg.type === "ROUND_RESULT") {
      showPlayerView("pview-result");
      const yr = msg.your_result || {};
      const isCorrect = Boolean(yr.is_correct);
      const rank = yr.rank || 1;
      const totalPlayers = yr.total_players || 1;

      updateHudBadge();

      const banner = document.getElementById("pres-banner");
      const icon = document.getElementById("pres-icon");
      const headline = document.getElementById("pres-headline");
      const ptsLine = document.getElementById("pres-points-line");

      if (isCorrect) {
        banner.className = "p-5 rounded-2xl border border-emerald-500/50 bg-emerald-500/15 text-center space-y-1.5";
        icon.innerText = "🎉";
        headline.innerText = "Correct Answer!";
        headline.className = "font-display font-bold text-2xl text-emerald-300";
        ptsLine.innerText = `Current Position: #${rank} of ${totalPlayers}`;
        fireSuccessConfetti();
      } else {
        banner.className = "p-5 rounded-2xl border border-rose-500/50 bg-rose-500/15 text-center space-y-1.5";
        icon.innerText = "❌";
        headline.innerText = yr.selected_option_index === -1 ? "Time's Up!" : "Incorrect Answer";
        headline.className = "font-display font-bold text-2xl text-rose-300";
        ptsLine.innerText = `Current Position: #${rank} of ${totalPlayers}`;
      }

      document.getElementById("pres-your-choice").innerText = yr.submitted_answer || "No option selected";
      document.getElementById("pres-correct-choice").innerText = msg.reference_answer || "";
      document.getElementById("pres-explanation").innerText = yr.micro_tutoring || "";
      document.getElementById("pres-next-countdown").innerText = msg.is_last_question
        ? `Final standings in ${msg.auto_next_seconds}s...`
        : `Next question in ${msg.auto_next_seconds}s...`;

      document.getElementById("pres-leaderboard").innerHTML = (msg.leaderboard || [])
        .map(
          (p) => `
          <div class="flex items-center justify-between px-4 py-2.5 rounded-xl ${
            p.player_id === myPlayerId ? "bg-violet-600/25 border border-violet-500/50" : "bg-zinc-900 border border-zinc-800"
          } text-sm">
            <div class="flex items-center gap-2.5">
              <span class="font-display font-bold text-zinc-400 w-6">#${p.rank}</span>
              <span>${p.avatar}</span>
              <span class="font-semibold">${escapeHtml(p.name)} ${p.player_id === myPlayerId ? "(You)" : ""}</span>
            </div>
            <span class="text-xs text-zinc-400 font-semibold">#${p.rank}</span>
          </div>
        `
        )
        .join("");
    }

    if (msg.type === "PODIUM") {
      renderFinalPodium(msg);
    }

    if (msg.type === "STUDY_KIT_READY") {
      renderStudyKit(msg.remediation_deck);
    }
  };
}

function renderFinalPodium(msg) {
  showPlayerView("pview-podium");
  const summary = msg.your_summary || {};
  updateHudBadge();

  document.getElementById("ppodium-personal-card").innerText =
    `You placed #${summary.rank || 1} of ${summary.total_players || 1} • ${summary.score || 0} pts (${summary.correct_count || 0}/${summary.total_questions || 0} Correct)`;

  const medals = ["🥇", "🥈", "🥉"];
  document.getElementById("ppodium-list").innerHTML = (msg.leaderboard || [])
    .map((p, idx) => {
      const medal = medals[idx] || `#${p.rank}`;
      const isMe = p.player_id === myPlayerId;
      return `
        <div class="flex items-center justify-between px-4 py-3 rounded-2xl ${
          isMe
            ? "bg-cyan-500/20 border border-cyan-400/60"
            : idx === 0
            ? "bg-violet-600/20 border border-violet-500/40"
            : "bg-zinc-900 border border-zinc-800"
        }">
          <div class="flex items-center gap-3">
            <span class="font-display font-bold text-base w-7 text-center">${medal}</span>
            <span class="text-xl">${p.avatar || "🐼"}</span>
            <div>
              <div class="font-bold text-sm">${escapeHtml(p.name)} ${isMe ? "(You)" : ""}</div>
              <div class="text-[11px] text-zinc-400">${p.correct_count || 0} correct</div>
            </div>
          </div>
          <span class="font-display font-bold text-cyan-400">${p.score || 0} pts</span>
        </div>
      `;
    })
    .join("");

  if (msg.remediation_deck) {
    renderStudyKit(msg.remediation_deck);
  } else {
    document.getElementById("ppodium-study-loading")?.classList.remove("hidden");
  }

  fireSuccessConfetti();
}

function renderFlipCardHtml(fc, accentBorderClass, accentTextClass) {
  return `
    <div onclick="this.classList.toggle('flipped')"
      class="group p-4 rounded-2xl bg-zinc-950 border ${accentBorderClass} cursor-pointer transition hover:bg-zinc-900/90 space-y-2 text-xs select-none">
      <div class="flex items-center justify-between">
        <span class="text-[10px] uppercase tracking-wider ${accentTextClass} font-bold">${escapeHtml(fc.tag || "Concept")}</span>
        <span class="text-[10px] text-zinc-500">Tap to flip ↻</span>
      </div>
      <div class="font-semibold text-zinc-100 leading-snug">${escapeHtml(fc.front)}</div>
      <div class="hidden group-[.flipped]:block pt-2 border-t border-zinc-800 text-cyan-200 leading-relaxed">
        ${escapeHtml(fc.back)}
      </div>
    </div>
  `;
}

function renderStudyKit(deck) {
  const loadingEl = document.getElementById("ppodium-study-loading");
  if (loadingEl) loadingEl.classList.add("hidden");
  if (!deck) return;

  // 1. Topic Flashcards
  const topicBox = document.getElementById("ppodium-topic-cards-box");
  const topicList = document.getElementById("ppodium-topic-cards-list");
  const topicCards = Array.isArray(deck.topic_flashcards) ? deck.topic_flashcards : [];
  if (topicBox && topicList && topicCards.length > 0) {
    topicBox.classList.remove("hidden");
    topicList.innerHTML = topicCards
      .map((fc) => renderFlipCardHtml(fc, "border-cyan-500/35", "text-cyan-400"))
      .join("");
  }

  // 2. Mistake Remediation Flashcards
  const mistakeBox = document.getElementById("ppodium-flashcards-box");
  const mistakeList = document.getElementById("ppodium-flashcards-list");
  const mistakeCards = Array.isArray(deck.flashcards) ? deck.flashcards : [];
  if (mistakeBox && mistakeList && mistakeCards.length > 0) {
    mistakeBox.classList.remove("hidden");
    document.getElementById("ppodium-deck-summary").innerText = deck.summary || "";
    mistakeList.innerHTML = mistakeCards
      .map((fc) => renderFlipCardHtml(fc, "border-amber-500/40", "text-amber-400"))
      .join("");
  }

  // 3. Interactive Remediation Practice Quiz
  const practiceBox = document.getElementById("ppodium-practice-box");
  const practiceList = document.getElementById("ppodium-practice-list");
  practiceQuizData = Array.isArray(deck.remediation_quiz) ? deck.remediation_quiz : [];
  if (practiceBox && practiceList && practiceQuizData.length > 0) {
    practiceBox.classList.remove("hidden");
    const letters = ["A", "B", "C", "D"];
    practiceList.innerHTML = practiceQuizData
      .map(
        (pq, qIdx) => `
        <div id="pq-card-${qIdx}" class="p-4 rounded-2xl bg-zinc-950/90 border border-emerald-500/30 space-y-3">
          <div class="text-xs font-bold text-emerald-400">Practice Question #${qIdx + 1}</div>
          <div class="text-sm font-semibold text-zinc-100">${escapeHtml(pq.question)}</div>
          <div class="grid sm:grid-cols-2 gap-2">
            ${(pq.options || [])
              .map(
                (opt, oIdx) => `
                <button type="button" onclick="answerPracticeQuestion(${qIdx}, ${oIdx}, this)"
                  class="pq-opt-${qIdx} p-2.5 rounded-xl bg-zinc-900 border border-zinc-800 hover:border-emerald-400 text-left text-xs text-zinc-200 transition">
                  <strong>${letters[oIdx]}.</strong> ${escapeHtml(opt)}
                </button>
              `
              )
              .join("")}
          </div>
          <div id="pq-feedback-${qIdx}" class="hidden p-3 rounded-xl text-xs leading-relaxed"></div>
        </div>
      `
      )
      .join("");
  }
}

function answerPracticeQuestion(qIdx, chosenIdx, btnEl) {
  const pq = practiceQuizData[qIdx];
  if (!pq) return;

  const buttons = document.querySelectorAll(`.pq-opt-${qIdx}`);
  buttons.forEach((b) => (b.disabled = true));

  const correctIdx = intVal(pq.correct_option_index);
  const isRight = chosenIdx === correctIdx;

  buttons.forEach((b, idx) => {
    if (idx === correctIdx) {
      b.classList.remove("border-zinc-800", "bg-zinc-900");
      b.classList.add("border-emerald-500", "bg-emerald-500/20", "text-emerald-200");
    } else if (idx === chosenIdx && !isRight) {
      b.classList.remove("border-zinc-800", "bg-zinc-900");
      b.classList.add("border-rose-500", "bg-rose-500/20", "text-rose-200");
    }
  });

  const fb = document.getElementById(`pq-feedback-${qIdx}`);
  if (fb) {
    fb.classList.remove("hidden");
    if (isRight) {
      fb.className = "p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/40 text-emerald-200 text-xs";
      fb.innerText = `🎉 Spot on! ${pq.explanation || ""}`;
      fireSuccessConfetti();
    } else {
      fb.className = "p-3 rounded-xl bg-rose-500/15 border border-rose-500/40 text-rose-200 text-xs";
      fb.innerText = `Not quite — ${pq.explanation || ""}`;
    }
  }
}

function intVal(v) {
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? 0 : n;
}

function renderPlayerOptions(options, chosenIdx, isLocked) {
  const letters = ["A", "B", "C", "D"];
  const grid = document.getElementById("pq-options-grid");
  if (!grid) return;

  grid.innerHTML = options
    .map((opt, idx) => {
      const isSelected = idx === chosenIdx;
      const borderClass = isSelected
        ? "border-cyan-400 bg-cyan-500/20 text-white shadow-lg shadow-cyan-500/15"
        : isLocked
        ? "border-zinc-800 bg-zinc-950/60 text-zinc-500 cursor-not-allowed"
        : "border-zinc-700 bg-zinc-900/90 hover:border-violet-400 hover:bg-zinc-800/90 text-zinc-100 cursor-pointer";

      return `
        <button type="button" ${isLocked ? "disabled" : ""} onclick="selectAnswerOption(${idx})"
          class="p-4 rounded-2xl border ${borderClass} flex items-center gap-3 text-left transition">
          <span class="w-8 h-8 rounded-xl ${
            isSelected ? "bg-cyan-400 text-zinc-950" : "bg-violet-600/30 border border-violet-500/40 text-violet-300"
          } font-display font-bold flex items-center justify-center shrink-0">
            ${letters[idx]}
          </span>
          <span class="text-sm font-medium leading-snug">${escapeHtml(opt)}</span>
        </button>
      `;
    })
    .join("");
}

function selectAnswerOption(idx) {
  if (answerLocked) return;
  if (!playerWs || playerWs.readyState !== WebSocket.OPEN) return;

  selectedOptionIndex = idx;
  answerLocked = true;

  const letters = ["A", "B", "C", "D"];
  const chosenText = `${letters[idx]}) ${currentOptions[idx] || ""}`;
  document.getElementById("pq-locked-banner").classList.remove("hidden");
  document.getElementById("pq-locked-choice").innerText = `Selected: ${chosenText} — waiting for round results...`;
  renderPlayerOptions(currentOptions, idx, true);

  playerWs.send(
    JSON.stringify({
      action: "SUBMIT_ANSWER",
      selected_option_index: idx,
    })
  );
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

window.selectAnswerOption = selectAnswerOption;
window.answerPracticeQuestion = answerPracticeQuestion;

document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(location.search);
  const urlPin = (params.get("pin") || "").trim();
  if (urlPin) {
    const pinInput = document.getElementById("join-pin");
    if (pinInput) pinInput.value = urlPin;
  }

  document.getElementById("join-room-form")?.addEventListener("submit", handleJoinSubmit);
  document.getElementById("btn-play-again")?.addEventListener("click", () => {
    if (stateSyncInterval) clearInterval(stateSyncInterval);
    if (playerWs) playerWs.close();
    showPlayerView("pview-join");
  });
});