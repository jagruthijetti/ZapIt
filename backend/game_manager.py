import asyncio
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from backend.ai_engine import (
    BOT_PROFILES,
    generate_bot_banter,
    generate_player_remediation,
    simulate_bot_answer,
)
from backend.database import (
    save_evaluations_batch,
    save_remediation_deck,
    update_room_status,
)
from backend.schemas import BotPersonaType, ProviderType

AVATARS = [
    "🦊", "🚀", "👾", "⚡", "🐉", "🔮", "🤖", "🦄", "🦉", "🔥",
    "🐼", "🦁", "🐙", "🐧", "🐯", "🦈", "🦅", "🐺", "🦖", "🐝",
]
OPTION_LETTERS = ["A", "B", "C", "D"]
MAX_PARTICIPANTS_PER_ROOM = 60
ROUND_REVIEW_SECONDS = 6


@dataclass
class Participant:
    player_id: str
    name: str
    student_id: str
    avatar: str
    is_bot: bool = False
    bot_persona: Optional[BotPersonaType] = None
    ws: Optional[WebSocket] = None
    score: int = 0
    streak: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GameRoom:
    pin: str
    quiz_id: str
    title: str
    source_text: str
    difficulty: str
    provider: ProviderType
    model_label: str
    questions: List[Dict[str, Any]]
    status: str = "LOBBY"  # LOBBY | IN_PROGRESS | ROUND_REVIEW | COMPLETED
    current_index: int = -1
    question_started_at: float = 0.0
    accepting_answers: bool = False
    host_ws: Optional[WebSocket] = None
    participants: Dict[str, Participant] = field(default_factory=dict)
    current_submissions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    timer_task: Optional[asyncio.Task] = None
    auto_next_task: Optional[asyncio.Task] = None
    bot_tasks: List[asyncio.Task] = field(default_factory=list)

    def get_public_players(self) -> List[Dict[str, Any]]:
        return [
            {
                "player_id": p.player_id,
                "name": p.name,
                "student_id": p.student_id,
                "avatar": p.avatar,
                "is_bot": p.is_bot,
                "score": p.score,
                "streak": p.streak,
            }
            for p in self.participants.values()
        ]

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        ranked = sorted(
            self.participants.values(),
            key=lambda x: (-x.score, -x.streak, x.name.lower()),
        )
        return [
            {
                "rank": idx + 1,
                "player_id": p.player_id,
                "name": p.name,
                "student_id": p.student_id,
                "avatar": p.avatar,
                "is_bot": p.is_bot,
                "score": p.score,
                "streak": p.streak,
            }
            for idx, p in enumerate(ranked)
        ]

    async def broadcast_to_all(self, payload: Dict[str, Any]) -> None:
        if self.host_ws:
            await self._safe_send(self.host_ws, payload)
        for p in list(self.participants.values()):
            if p.ws:
                await self._safe_send(p.ws, payload)

    async def send_to_host(self, payload: Dict[str, Any]) -> None:
        if self.host_ws:
            await self._safe_send(self.host_ws, payload)

    @staticmethod
    async def _safe_send(ws: WebSocket, payload: Dict[str, Any]) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            pass


_ROOMS: Dict[str, GameRoom] = {}
_RESERVED_PINS: Dict[str, float] = {}


def generate_unique_pin() -> str:
    now = time.time()
    expired = [p for p, ts in _RESERVED_PINS.items() if now - ts > 600]
    for p in expired:
        _RESERVED_PINS.pop(p, None)

    while True:
        pin = "".join(random.choices(string.digits, k=6))
        if pin not in _ROOMS and pin not in _RESERVED_PINS:
            return pin


def reserve_pin() -> str:
    pin = generate_unique_pin()
    _RESERVED_PINS[pin] = time.time()
    return pin


def get_room(pin: str) -> Optional[GameRoom]:
    return _ROOMS.get((pin or "").strip())


def is_pin_reserved(pin: str) -> bool:
    return (pin or "").strip() in _RESERVED_PINS


def create_room(
    quiz_id: str,
    title: str,
    source_text: str,
    difficulty: str,
    provider: ProviderType,
    model_label: str,
    questions: List[Dict[str, Any]],
    bots: List[BotPersonaType],
    reserved_pin: Optional[str] = None,
) -> GameRoom:
    clean_reserved = (reserved_pin or "").strip()
    if clean_reserved and clean_reserved in _RESERVED_PINS and clean_reserved not in _ROOMS:
        pin = clean_reserved
        _RESERVED_PINS.pop(clean_reserved, None)
    else:
        pin = generate_unique_pin()

    room = GameRoom(
        pin=pin,
        quiz_id=quiz_id,
        title=title,
        source_text=source_text,
        difficulty=difficulty,
        provider=provider,
        model_label=model_label,
        questions=questions,
    )
    for persona in bots:
        profile = BOT_PROFILES[persona]
        bot_id = f"bot_{persona}_{random.randint(100, 999)}"
        room.participants[bot_id] = Participant(
            player_id=bot_id,
            name=profile["name"],
            student_id=profile["student_id"],
            avatar=profile["avatar"],
            is_bot=True,
            bot_persona=persona,
        )
    _ROOMS[pin] = room
    return room


def _build_topic_heatmap(room: GameRoom) -> List[Dict[str, Any]]:
    human_players = [p for p in room.participants.values() if not p.is_bot]
    if not human_players:
        return []

    heatmap: List[Dict[str, Any]] = []
    for idx, q in enumerate(room.questions, start=1):
        q_id = q["question_id"]
        total_attempts = 0
        correct_count = 0
        wrong_counts: Dict[str, int] = {}

        for p in human_players:
            record = next((h for h in p.history if h["question_id"] == q_id), None)
            if not record:
                continue
            total_attempts += 1
            if record["awarded_marks"] >= record["max_marks"]:
                correct_count += 1
            else:
                ans = record.get("submitted_answer") or "No Answer"
                wrong_counts[ans] = wrong_counts.get(ans, 0) + 1

        accuracy_pct = round((correct_count / total_attempts) * 100) if total_attempts > 0 else 0
        most_common_wrong = max(wrong_counts.items(), key=lambda item: item[1])[0] if wrong_counts else None

        if accuracy_pct < 50:
            severity = "critical"
        elif accuracy_pct < 80:
            severity = "moderate"
        else:
            severity = "strong"

        heatmap.append(
            {
                "question_number": idx,
                "question_id": q_id,
                "question": q["question"],
                "reference_answer": q["reference_answer"],
                "correct_count": correct_count,
                "total_attempts": total_attempts,
                "accuracy_pct": accuracy_pct,
                "severity": severity,
                "most_common_wrong": most_common_wrong,
            }
        )

    # Sort weakest (lowest accuracy) first so the host immediately sees what to re-teach
    heatmap.sort(key=lambda item: (item["accuracy_pct"], item["question_number"]))
    return heatmap


def add_human_participant(
    room: GameRoom,
    name: str,
    student_id: str,
    ws: WebSocket,
) -> Participant:
    if len(room.participants) >= MAX_PARTICIPANTS_PER_ROOM:
        raise ValueError(f"This quiz room has reached maximum capacity ({MAX_PARTICIPANTS_PER_ROOM} participants).")

    clean_name = name.strip()[:32] or "Player"
    clean_sid = student_id.strip()[:24] or "STU-000"

    for existing in room.participants.values():
        if not existing.is_bot and existing.name.lower() == clean_name.lower() and existing.student_id.lower() == clean_sid.lower():
            existing.ws = ws
            return existing

    pid = f"p_{random.randint(10000, 99999)}"
    avatar = AVATARS[len(room.participants) % len(AVATARS)]
    participant = Participant(
        player_id=pid,
        name=clean_name,
        student_id=clean_sid,
        avatar=avatar,
        is_bot=False,
        ws=ws,
    )
    room.participants[pid] = participant
    return participant


def _cancel_task(task: Optional[asyncio.Task]) -> None:
    if task and not task.done():
        task.cancel()


async def start_next_question(room: GameRoom) -> None:
    _cancel_task(room.timer_task)
    _cancel_task(room.auto_next_task)
    for t in room.bot_tasks:
        _cancel_task(t)
    room.bot_tasks.clear()

    room.current_index += 1

    # End of Quiz -> Final Standings & Host-Only Weakest Topic Heatmap
    if room.current_index >= len(room.questions):
        room.status = "COMPLETED"
        await update_room_status(room.pin, "COMPLETED")
        lb = room.get_leaderboard()
        heatmap = _build_topic_heatmap(room)

        # Send PODIUM + Weakest Topic Heatmap exclusively to the Host
        if room.host_ws:
            await GameRoom._safe_send(
                room.host_ws,
                {
                    "type": "PODIUM",
                    "leaderboard": lb,
                    "topic_heatmap": heatmap,
                },
            )

        # Send standard PODIUM to participants (host-only heatmap excluded)
        for p in list(room.participants.values()):
            if p.ws:
                await GameRoom._safe_send(p.ws, {"type": "PODIUM", "leaderboard": lb})

        rem_sem = asyncio.Semaphore(6 if room.provider == "gemini" else 2)
        for p in list(room.participants.values()):
            if not p.is_bot and p.ws:
                asyncio.create_task(_build_and_send_remediation(room, p, rem_sem))
        return

    room.status = "IN_PROGRESS"
    if room.current_index == 0:
        await update_room_status(room.pin, "IN_PROGRESS")

    q = room.questions[room.current_index]
    room.current_submissions = {}
    room.question_started_at = time.monotonic()
    room.accepting_answers = True

    time_limit = int(q.get("time_limit", 20))

    await room.broadcast_to_all(
        {
            "type": "QUESTION_START",
            "question_number": room.current_index + 1,
            "total_questions": len(room.questions),
            "question_id": q["question_id"],
            "question": q["question"],
            "options": q.get("options", []),
            "max_marks": q["max_marks"],
            "time_limit": time_limit,
            "total_participants": len(room.participants),
        }
    )

    for p in room.participants.values():
        if p.is_bot and p.bot_persona:
            task = asyncio.create_task(_run_bot_turn(room, p, q))
            room.bot_tasks.append(task)

    room.timer_task = asyncio.create_task(_question_countdown(room, time_limit))


async def _run_bot_turn(room: GameRoom, bot: Participant, question: Dict[str, Any]) -> None:
    try:
        assert bot.bot_persona is not None
        bot_res = await simulate_bot_answer(room.provider, bot.bot_persona, question)
        time_limit = int(question.get("time_limit", 20))
        delay = min(float(time_limit - 1.0), max(1.5, bot_res.thinking_delay_sec))
        await asyncio.sleep(delay)
        await record_answer_submission(room, bot.player_id, bot_res.selected_option_index)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _question_countdown(room: GameRoom, time_limit: int) -> None:
    try:
        for remaining in range(time_limit - 1, -1, -1):
            await asyncio.sleep(1)
            if not room.accepting_answers:
                return
            await room.broadcast_to_all({"type": "TIMER_TICK", "remaining": remaining})
        await finalize_current_question(room)
    except asyncio.CancelledError:
        pass


async def record_answer_submission(
    room: GameRoom,
    player_id: str,
    selected_option_index: int,
) -> bool:
    if not room.accepting_answers:
        return False
    if player_id not in room.participants or player_id in room.current_submissions:
        return False
    if selected_option_index not in (0, 1, 2, 3):
        return False

    elapsed = max(0.2, round(time.monotonic() - room.question_started_at, 2))
    room.current_submissions[player_id] = {
        "selected_option_index": selected_option_index,
        "response_time_sec": elapsed,
    }

    await room.broadcast_to_all(
        {
            "type": "ANSWER_COUNT",
            "answered": len(room.current_submissions),
            "total": len(room.participants),
        }
    )

    if len(room.participants) > 0 and len(room.current_submissions) >= len(room.participants):
        _cancel_task(room.timer_task)
        asyncio.create_task(finalize_current_question(room))
    return True


async def finalize_current_question(room: GameRoom) -> None:
    if not room.accepting_answers:
        return
    room.accepting_answers = False
    room.status = "ROUND_REVIEW"

    q = room.questions[room.current_index]
    options = q.get("options", [])
    correct_idx = int(q["correct_option_index"])
    explanations = q.get("option_explanations", [])
    time_limit = max(1, int(q.get("time_limit", 20)))
    max_marks = int(q.get("max_marks", 2))

    await room.broadcast_to_all({"type": "EVALUATING_ROUND"})

    db_records: List[Dict[str, Any]] = []
    round_breakdown: List[Dict[str, Any]] = []
    player_feedback: Dict[str, Dict[str, Any]] = {}

    for p in room.participants.values():
        sub = room.current_submissions.get(p.player_id)
        if not sub:
            is_correct = False
            awarded_marks = 0
            response_time = float(time_limit)
            submitted_label = "(No option selected in time)"
            correct_opt_text = options[correct_idx] if correct_idx < len(options) else q["reference_answer"]
            micro_tutoring = (
                f"Time expired! The correct answer was {OPTION_LETTERS[correct_idx]}: {correct_opt_text}."
            )
            evidence = q["reference_answer"]
        else:
            sel_idx = int(sub["selected_option_index"])
            response_time = float(sub["response_time_sec"])
            is_correct = sel_idx == correct_idx
            awarded_marks = max_marks if is_correct else 0
            opt_text = options[sel_idx] if sel_idx < len(options) else f"Option {sel_idx + 1}"
            submitted_label = f"{OPTION_LETTERS[sel_idx]}) {opt_text}"
            micro_tutoring = (
                explanations[sel_idx]
                if sel_idx < len(explanations)
                else ("Correct choice!" if is_correct else f"Incorrect. Answer: {q['reference_answer']}")
            )
            evidence = q["reference_answer"]

        if is_correct:
            p.streak += 1
            speed_ratio = max(0.0, (time_limit - response_time) / time_limit)
            speed_bonus = int(round(350 * speed_ratio))
            streak_bonus = min(300, (p.streak - 1) * 75)
            round_score = 650 + speed_bonus + streak_bonus
        else:
            p.streak = 0
            round_score = 0

        p.score += round_score

        history_entry = {
            "question_id": q["question_id"],
            "question": q["question"],
            "submitted_answer": submitted_label,
            "reference_answer": q["reference_answer"],
            "awarded_marks": awarded_marks,
            "max_marks": max_marks,
            "round_score": round_score,
            "micro_tutoring": micro_tutoring,
            "evidence_from_source": evidence,
        }
        p.history.append(history_entry)
        player_feedback[p.player_id] = history_entry

        round_breakdown.append(
            {
                "player_id": p.player_id,
                "name": p.name,
                "avatar": p.avatar,
                "is_bot": p.is_bot,
                "submitted_answer": submitted_label,
                "is_correct": is_correct,
                "round_score": round_score,
            }
        )

        db_records.append(
            {
                "game_pin": room.pin,
                "question_id": q["question_id"],
                "player_id": p.player_id,
                "player_name": p.name,
                "student_id": p.student_id,
                "is_bot": p.is_bot,
                "submitted_answer": submitted_label,
                "response_time_sec": response_time,
                "awarded_marks": awarded_marks,
                "max_marks": max_marks,
                "round_score": round_score,
                "micro_tutoring": micro_tutoring,
                "evidence_from_source": evidence,
                "model_used": "Instant MCQ Engine",
            }
        )

    await save_evaluations_batch(db_records)
    leaderboard = room.get_leaderboard()
    is_last_question = (room.current_index + 1) >= len(room.questions)

    bot_banter = None
    bots_in_room = [p for p in room.participants.values() if p.is_bot and p.bot_persona]
    if bots_in_room and leaderboard:
        chosen_bot = random.choice(bots_in_room)
        top_summary = ", ".join([f"#{r['rank']} {r['name']} ({r['score']} pts)" for r in leaderboard[:4]])
        assert chosen_bot.bot_persona is not None
        bot_banter = await generate_bot_banter(room.provider, chosen_bot.bot_persona, top_summary)

    # Host receives full leaderboard, breakdown, and reference answer
    await room.send_to_host(
        {
            "type": "ROUND_RESULT",
            "question_number": room.current_index + 1,
            "total_questions": len(room.questions),
            "is_last_question": is_last_question,
            "auto_next_seconds": ROUND_REVIEW_SECONDS,
            "reference_answer": q["reference_answer"],
            "leaderboard": leaderboard,
            "breakdown": round_breakdown,
            "bot_banter": bot_banter,
        }
    )

    # Participants receive only their personal feedback (no mid-game leaderboard)
    for p in list(room.participants.values()):
        if p.ws:
            await GameRoom._safe_send(
                p.ws,
                {
                    "type": "ROUND_RESULT",
                    "question_number": room.current_index + 1,
                    "total_questions": len(room.questions),
                    "is_last_question": is_last_question,
                    "auto_next_seconds": ROUND_REVIEW_SECONDS,
                    "reference_answer": q["reference_answer"],
                    "your_feedback": player_feedback.get(p.player_id),
                    "your_total_score": p.score,
                    "your_streak": p.streak,
                },
            )

    room.auto_next_task = asyncio.create_task(_auto_advance_after_review(room, ROUND_REVIEW_SECONDS))


async def _auto_advance_after_review(room: GameRoom, delay_seconds: int) -> None:
    try:
        for rem in range(delay_seconds - 1, 0, -1):
            await asyncio.sleep(1)
            if room.status != "ROUND_REVIEW":
                return
            await room.broadcast_to_all({"type": "AUTO_NEXT_TICK", "remaining": rem})
        await asyncio.sleep(1)
        if room.status == "ROUND_REVIEW":
            await start_next_question(room)
    except asyncio.CancelledError:
        pass


async def _build_and_send_remediation(
    room: GameRoom,
    player: Participant,
    sem: asyncio.Semaphore,
) -> None:
    missed = [item for item in player.history if item["awarded_marks"] < item["max_marks"]]
    try:
        async with sem:
            deck, used_model = await generate_player_remediation(
                provider=room.provider,
                player_name=player.name,
                source_text=room.source_text,
                missed_items=missed,
            )
        deck_dict = deck.model_dump()
        await save_remediation_deck(room.pin, player.player_id, player.name, deck_dict, used_model)
        if player.ws:
            await GameRoom._safe_send(
                player.ws,
                {
                    "type": "REMEDIATION_READY",
                    "deck": deck_dict,
                    "model_used": used_model,
                },
            )
    except Exception as err:
        if player.ws:
            await GameRoom._safe_send(player.ws, {"type": "REMEDIATION_ERROR", "detail": str(err)})

finish_round = finalize_current_question