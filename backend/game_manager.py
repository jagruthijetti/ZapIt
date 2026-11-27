import asyncio
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from backend.ai_engine import generate_bot_banter, generate_bot_choice, generate_remediation_deck
from backend.database import (
    save_evaluations_batch,
    save_remediation_deck,
    update_room_status,
)

MAX_PARTICIPANTS_PER_ROOM = 120
AUTO_NEXT_SECONDS = 6

AVATAR_POOL = ["🦊", "🐼", "🦁", "🐯", "🐸", "🐨", "🐙", "🦄", "🐲", "🦉", "🐧", "🐺", "🦖", "🐝", "🐬"]

BOT_PROFILES = {
    "speedrunner": {"name": "ByteBlitz", "student_id": "BOT-01", "avatar": "⚡"},
    "professor": {"name": "Prof. Qwenstein", "student_id": "BOT-02", "avatar": "🦉"},
    "chaotic": {"name": "GlitchGremlin", "student_id": "BOT-03", "avatar": "👾"},
}


@dataclass
class Participant:
    player_id: str
    name: str
    student_id: str
    avatar: str
    is_bot: bool = False
    bot_persona: Optional[str] = None
    ws: Optional[WebSocket] = None
    score: int = 0
    streak: int = 0
    correct_count: int = 0
    missed_questions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GameRoom:
    pin: str
    quiz_id: str
    title: str
    source_text: str
    difficulty: str
    provider: str
    model_label: str
    questions: List[Dict[str, Any]]
    status: str = "LOBBY"  # LOBBY | IN_PROGRESS | ROUND_REVIEW | COMPLETED
    current_index: int = -1
    question_started_at: float = 0.0
    host_ws: Optional[WebSocket] = None
    participants: Dict[str, Participant] = field(default_factory=dict)
    current_submissions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    round_task: Optional[asyncio.Task] = None
    auto_next_task: Optional[asyncio.Task] = None
    bot_tasks: List[asyncio.Task] = field(default_factory=list)
    round_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def cancel_active_tasks(self) -> None:
        if self.round_task and not self.round_task.done():
            self.round_task.cancel()
        self.round_task = None

        if self.auto_next_task and not self.auto_next_task.done():
            self.auto_next_task.cancel()
        self.auto_next_task = None

        for t in self.bot_tasks:
            if not t.done():
                t.cancel()
        self.bot_tasks.clear()

    def get_public_players(self) -> List[Dict[str, Any]]:
        return [
            {
                "player_id": p.player_id,
                "name": p.name,
                "student_id": p.student_id,
                "avatar": p.avatar,
                "is_bot": p.is_bot,
                "score": p.score,
            }
            for p in self.participants.values()
        ]

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        ordered = sorted(
            self.participants.values(),
            key=lambda p: (-p.score, -p.correct_count, p.name.lower()),
        )
        board = []
        for idx, p in enumerate(ordered, start=1):
            board.append(
                {
                    "rank": idx,
                    "player_id": p.player_id,
                    "name": p.name,
                    "student_id": p.student_id,
                    "avatar": p.avatar,
                    "is_bot": p.is_bot,
                    "score": p.score,
                    "correct_count": p.correct_count,
                    "streak": p.streak,
                }
            )
        return board

    async def send_to_host(self, payload: Dict[str, Any]) -> None:
        if self.host_ws is not None:
            try:
                await self.host_ws.send_json(payload)
            except Exception:
                self.host_ws = None

    async def broadcast_to_players(self, payload: Dict[str, Any]) -> None:
        for p in list(self.participants.values()):
            if not p.is_bot and p.ws is not None:
                try:
                    await p.ws.send_json(payload)
                except Exception:
                    p.ws = None

    async def broadcast_to_all(self, payload: Dict[str, Any]) -> None:
        await self.send_to_host(payload)
        await self.broadcast_to_players(payload)


_ROOMS: Dict[str, GameRoom] = {}
_RESERVED_PINS: Dict[str, float] = {}


def _generate_unique_pin() -> str:
    for _ in range(200):
        pin = "".join(random.choices(string.digits, k=6))
        if pin not in _ROOMS and pin not in _RESERVED_PINS:
            return pin
    return "".join(random.choices(string.digits, k=6))


def reserve_pin() -> str:
    now = time.monotonic()
    expired = [k for k, ts in _RESERVED_PINS.items() if now - ts > 600]
    for k in expired:
        _RESERVED_PINS.pop(k, None)
    pin = _generate_unique_pin()
    _RESERVED_PINS[pin] = now
    return pin


def is_pin_reserved(pin: str) -> bool:
    ts = _RESERVED_PINS.get(pin)
    if ts is None:
        return False
    if time.monotonic() - ts > 600:
        _RESERVED_PINS.pop(pin, None)
        return False
    return True


def get_room(pin: str) -> Optional[GameRoom]:
    return _ROOMS.get((pin or "").strip())


def create_room(
    quiz_id: str,
    title: str,
    source_text: str,
    difficulty: str,
    provider: str,
    model_label: str,
    questions: List[Dict[str, Any]],
    bots: List[str],
    reserved_pin: Optional[str] = None,
) -> GameRoom:
    clean_reserved = (reserved_pin or "").strip()
    if clean_reserved and clean_reserved not in _ROOMS:
        pin = clean_reserved
        _RESERVED_PINS.pop(clean_reserved, None)
    else:
        pin = _generate_unique_pin()

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
        profile = BOT_PROFILES.get(persona)
        if profile:
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


def add_human_participant(
    room: GameRoom,
    name: str,
    student_id: str,
    ws: WebSocket,
) -> Participant:
    clean_name = (name or "Player").strip()[:40] or "Player"
    clean_sid = (student_id or "STU-000").strip()[:30] or "STU-000"

    # Reconnect existing participant with same student_id & name if they refreshed
    for existing in room.participants.values():
        if (
            not existing.is_bot
            and existing.student_id.lower() == clean_sid.lower()
            and existing.name.lower() == clean_name.lower()
        ):
            existing.ws = ws
            return existing

    if len(room.participants) >= MAX_PARTICIPANTS_PER_ROOM:
        raise ValueError(f"Room {room.pin} is full ({MAX_PARTICIPANTS_PER_ROOM} participants max).")

    player_id = "plr_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

    # Pick an emoji not yet used in this room so every player gets a distinct avatar
    used_avatars = {p.avatar for p in room.participants.values()}
    available_avatars = [a for a in AVATAR_POOL if a not in used_avatars]
    avatar = random.choice(available_avatars) if available_avatars else random.choice(AVATAR_POOL)

    participant = Participant(
        player_id=player_id,
        name=clean_name,
        student_id=clean_sid,
        avatar=avatar,
        is_bot=False,
        ws=ws,
    )
    room.participants[player_id] = participant
    return participant


async def notify_answer_progress(room: GameRoom) -> None:
    await room.send_to_host(
        {
            "type": "ANSWER_COUNT",
            "answered": len(room.current_submissions),
            "total": len(room.participants),
        }
    )


async def record_answer_submission(
    room: GameRoom,
    player_id: str,
    selected_option_index: int,
) -> bool:
    if room.status != "IN_PROGRESS":
        return False
    if player_id not in room.participants:
        return False
    if player_id in room.current_submissions:
        return False
    if not (0 <= selected_option_index <= 3):
        return False

    elapsed = max(0.1, round(time.monotonic() - room.question_started_at, 2))
    room.current_submissions[player_id] = {
        "selected_option_index": int(selected_option_index),
        "response_time_sec": elapsed,
    }
    await notify_answer_progress(room)

    # If every participant in the room has answered, finish the round early
    if len(room.current_submissions) >= len(room.participants) and len(room.participants) > 0:
        asyncio.create_task(finish_round(room))

    return True


async def _simulate_bot_answer(room: GameRoom, bot: Participant, question: Dict[str, Any]) -> None:
    try:
        time_limit = int(question.get("time_limit", 20))
        choice_res = await generate_bot_choice(
            provider=room.provider,
            persona=bot.bot_persona or "speedrunner",
            question=question,
            time_limit=time_limit,
        )
        delay = min(max(1.0, float(choice_res.thinking_delay_sec)), max(1.5, time_limit - 1.5))
        await asyncio.sleep(delay)
        if room.status == "IN_PROGRESS":
            await record_answer_submission(room, bot.player_id, int(choice_res.selected_option_index))
    except asyncio.CancelledError:
        pass
    except Exception:
        # Fallback random choice if bot LLM call fails
        await asyncio.sleep(2.0)
        if room.status == "IN_PROGRESS":
            await record_answer_submission(room, bot.player_id, random.randint(0, 3))


async def _run_question_timer(room: GameRoom, expected_index: int, time_limit: int) -> None:
    try:
        for remaining in range(time_limit - 1, -1, -1):
            await asyncio.sleep(1.0)
            if room.status != "IN_PROGRESS" or room.current_index != expected_index:
                return
            await room.broadcast_to_all({"type": "TIMER_TICK", "remaining": remaining})

        if room.status == "IN_PROGRESS" and room.current_index == expected_index:
            await finish_round(room)
    except asyncio.CancelledError:
        pass


async def _run_auto_next_countdown(room: GameRoom, expected_index: int) -> None:
    try:
        for remaining in range(AUTO_NEXT_SECONDS - 1, 0, -1):
            await asyncio.sleep(1.0)
            if room.status != "ROUND_REVIEW" or room.current_index != expected_index:
                return
            await room.broadcast_to_all({"type": "AUTO_NEXT_TICK", "remaining": remaining})

        await asyncio.sleep(1.0)
        if room.status == "ROUND_REVIEW" and room.current_index == expected_index:
            await start_next_question(room)
    except asyncio.CancelledError:
        pass


async def start_next_question(room: GameRoom) -> None:
    async with room.round_lock:
        room.cancel_active_tasks()
        next_idx = room.current_index + 1

        if next_idx >= len(room.questions):
            await _complete_match(room)
            return

        room.current_index = next_idx
        room.status = "IN_PROGRESS"
        room.current_submissions = {}
        room.question_started_at = time.monotonic()

        if next_idx == 0:
            await update_room_status(room.pin, "IN_PROGRESS")

        q = room.questions[next_idx]
        time_limit = max(10, min(90, int(q.get("time_limit", 20))))

        payload = {
            "type": "QUESTION_START",
            "question_number": next_idx + 1,
            "total_questions": len(room.questions),
            "question_id": q.get("question_id", next_idx + 1),
            "question": q["question"],
            "options": q.get("options", []),
            "max_marks": int(q.get("max_marks", 2)),
            "time_limit": time_limit,
            "total_participants": len(room.participants),
        }
        await room.broadcast_to_all(payload)

        room.round_task = asyncio.create_task(_run_question_timer(room, next_idx, time_limit))

        for p in room.participants.values():
            if p.is_bot:
                t = asyncio.create_task(_simulate_bot_answer(room, p, q))
                room.bot_tasks.append(t)


async def finish_round(room: GameRoom) -> None:
    async with room.round_lock:
        if room.status != "IN_PROGRESS":
            return

        room.cancel_active_tasks()
        room.status = "ROUND_REVIEW"

        q = room.questions[room.current_index]
        options: List[str] = q.get("options", [])
        correct_idx = int(q.get("correct_option_index", 0))
        max_marks = int(q.get("max_marks", 2))
        time_limit = max(10, int(q.get("time_limit", 20)))
        letters = ["A", "B", "C", "D"]

        correct_text = options[correct_idx] if 0 <= correct_idx < len(options) else ""
        reference_answer = q.get("reference_answer") or f"{letters[correct_idx]}) {correct_text}"
        explanations: List[str] = q.get("option_explanations") or []

        eval_records: List[Dict[str, Any]] = []
        breakdown: List[Dict[str, Any]] = []
        per_player_results: Dict[str, Dict[str, Any]] = {}

        for p in room.participants.values():
            sub = room.current_submissions.get(p.player_id)
            if sub is None:
                sel_idx = -1
                resp_time = float(time_limit)
                submitted_label = "No option selected"
                is_correct = False
                micro_tutoring = f"Time expired before an answer was selected. The correct answer is {reference_answer}."
            else:
                sel_idx = int(sub["selected_option_index"])
                resp_time = float(sub["response_time_sec"])
                opt_text = options[sel_idx] if 0 <= sel_idx < len(options) else f"Option {sel_idx + 1}"
                submitted_label = f"{letters[sel_idx]}) {opt_text}" if 0 <= sel_idx < 4 else opt_text
                is_correct = sel_idx == correct_idx

                if 0 <= sel_idx < len(explanations) and explanations[sel_idx]:
                    micro_tutoring = explanations[sel_idx]
                elif is_correct:
                    micro_tutoring = f"Spot on! {reference_answer} is correct."
                else:
                    micro_tutoring = f"You selected {submitted_label}. The correct answer is {reference_answer}."

            if is_correct:
                speed_ratio = max(0.0, min(1.0, (time_limit - resp_time) / time_limit))
                round_score = int(round((500 + 500 * speed_ratio) * (max_marks / 2.0)))
                awarded_marks = max_marks
                p.score += round_score
                p.correct_count += 1
                p.streak += 1
            else:
                round_score = 0
                awarded_marks = 0
                p.streak = 0
                p.missed_questions.append(
                    {
                        "question": q["question"],
                        "your_answer": submitted_label,
                        "correct_answer": reference_answer,
                        "explanation": micro_tutoring,
                    }
                )

            per_player_results[p.player_id] = {
                "is_correct": is_correct,
                "selected_option_index": sel_idx,
                "submitted_answer": submitted_label,
                "round_score": round_score,
                "total_score": p.score,
                "streak": p.streak,
                "micro_tutoring": micro_tutoring,
            }

            breakdown.append(
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

            eval_records.append(
                {
                    "game_pin": room.pin,
                    "question_id": q.get("question_id", room.current_index + 1),
                    "player_id": p.player_id,
                    "player_name": p.name,
                    "student_id": p.student_id,
                    "is_bot": p.is_bot,
                    "submitted_answer": submitted_label,
                    "response_time_sec": resp_time,
                    "awarded_marks": awarded_marks,
                    "max_marks": max_marks,
                    "round_score": round_score,
                    "micro_tutoring": micro_tutoring,
                    "evidence_from_source": reference_answer,
                    "model_used": room.model_label,
                }
            )

        await save_evaluations_batch(eval_records)

        leaderboard = room.get_leaderboard()
        rank_map = {row["player_id"]: row["rank"] for row in leaderboard}
        is_last = (room.current_index + 1) >= len(room.questions)

        bot_banter = ""
        if any(p.is_bot for p in room.participants.values()) and leaderboard:
            try:
                bot_banter = await generate_bot_banter(
                    provider=room.provider,
                    question=q["question"],
                    leader_name=leaderboard[0]["name"],
                )
            except Exception:
                bot_banter = ""

        # Send host view
        await room.send_to_host(
            {
                "type": "ROUND_RESULT",
                "question_number": room.current_index + 1,
                "total_questions": len(room.questions),
                "correct_option_index": correct_idx,
                "reference_answer": reference_answer,
                "leaderboard": leaderboard,
                "breakdown": breakdown,
                "bot_banter": bot_banter,
                "is_last_question": is_last,
                "auto_next_seconds": AUTO_NEXT_SECONDS,
            }
        )

        # Send personalized round outcome to each human participant
        for p in list(room.participants.values()):
            if not p.is_bot and p.ws is not None:
                res_info = per_player_results[p.player_id]
                try:
                    await p.ws.send_json(
                        {
                            "type": "ROUND_RESULT",
                            "question_number": room.current_index + 1,
                            "total_questions": len(room.questions),
                            "correct_option_index": correct_idx,
                            "reference_answer": reference_answer,
                            "your_result": {
                                **res_info,
                                "rank": rank_map.get(p.player_id, 1),
                                "total_players": len(leaderboard),
                            },
                            "leaderboard": leaderboard,
                            "bot_banter": bot_banter,
                            "is_last_question": is_last,
                            "auto_next_seconds": AUTO_NEXT_SECONDS,
                        }
                    )
                except Exception:
                    p.ws = None

        room.auto_next_task = asyncio.create_task(_run_auto_next_countdown(room, room.current_index))


async def _complete_match(room: GameRoom) -> None:
    room.status = "COMPLETED"
    leaderboard = room.get_leaderboard()
    rank_map = {row["player_id"]: row["rank"] for row in leaderboard}

    # Broadcast final standings to Host immediately so the podium displays without delay
    await room.send_to_host(
        {
            "type": "PODIUM",
            "title": room.title,
            "total_questions": len(room.questions),
            "leaderboard": leaderboard,
        }
    )

    try:
        await update_room_status(room.pin, "COMPLETED")
    except Exception:
        pass

    for p in list(room.participants.values()):
        if not p.is_bot and p.ws is not None:
            deck_dict = None
            if p.missed_questions:
                try:
                    deck_obj, model_used = await generate_remediation_deck(
                        provider=room.provider,
                        source_text=room.source_text,
                        missed_questions=p.missed_questions,
                    )
                    deck_dict = deck_obj.model_dump()
                    await save_remediation_deck(
                        game_pin=room.pin,
                        player_id=p.player_id,
                        player_name=p.name,
                        deck=deck_dict,
                        model_used=model_used,
                    )
                except Exception:
                    deck_dict = None

            try:
                await p.ws.send_json(
                    {
                        "type": "PODIUM",
                        "title": room.title,
                        "leaderboard": leaderboard,
                        "your_summary": {
                            "rank": rank_map.get(p.player_id, 1),
                            "total_players": len(leaderboard),
                            "score": p.score,
                            "correct_count": p.correct_count,
                            "total_questions": len(room.questions),
                        },
                        "remediation_deck": deck_dict,
                    }
                )
            except Exception:
                p.ws = None