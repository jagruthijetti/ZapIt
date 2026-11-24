import asyncio
import random
import string
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from backend.ai_engine import (
    BOT_PROFILES,
    OPTION_LETTERS,
    generate_bot_banter,
    generate_player_remediation,
    simulate_bot_answer,
)
from backend.database import (
    save_evaluations_batch,
    save_remediation_deck,
    update_room_status,
)
from backend.llm_router import get_model_label
from backend.schemas import BotPersonaType, ProviderType

AVATARS = ["🦊", "🚀", "👾", "⚡", "🐉", "🔮", "🤖", "🦄", "🦉", "🔥", "🐼", "🦁", "🐙", "🐧", "🐯"]
MAX_PARTICIPANTS_PER_ROOM = 60
AUTO_NEXT_DELAY_SEC = 6


class Participant:
    def __init__(
        self,
        player_id: str,
        name: str,
        student_id: str,
        avatar: str,
        is_bot: bool = False,
        persona: Optional[BotPersonaType] = None,
        ws: Optional[WebSocket] = None,
    ):
        self.player_id = player_id
        self.name = name
        self.student_id = student_id
        self.avatar = avatar
        self.is_bot = is_bot
        self.persona = persona
        self.ws = ws
        self.score: int = 0
        self.streak: int = 0
        self.selected_option: Optional[int] = None
        self.response_time: float = 0.0
        self.history: List[Dict[str, Any]] = []


class GameRoom:
    def __init__(
        self,
        pin: str,
        quiz_id: str,
        title: str,
        source_text: str,
        provider: ProviderType,
        questions: List[Dict[str, Any]],
        bots: List[BotPersonaType],
    ):
        self.pin = pin
        self.quiz_id = quiz_id
        self.title = title
        self.source_text = source_text
        self.provider = provider
        self.model_label = get_model_label(provider)
        self.questions = questions
        self.status = "LOBBY"  # PREPARING | LOBBY | QUESTION | ROUND_RESULT | PODIUM
        self.current_idx = -1
        self.question_start_time = 0.0
        self.host_ws: Optional[WebSocket] = None
        self.participants: Dict[str, Participant] = {}
        self.timer_task: Optional[asyncio.Task] = None
        self.auto_next_task: Optional[asyncio.Task] = None
        self.bot_tasks: List[asyncio.Task] = []
        self.round_lock = asyncio.Lock()

        for idx, persona in enumerate(bots, start=1):
            profile = BOT_PROFILES[persona]
            bot_id = f"bot_{persona}_{idx}"
            self.participants[bot_id] = Participant(
                player_id=bot_id,
                name=profile["name"],
                student_id=profile["student_id"],
                avatar=profile["avatar"],
                is_bot=True,
                persona=persona,
            )

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        ranked = sorted(self.participants.values(), key=lambda p: p.score, reverse=True)
        return [
            {
                "rank": i + 1,
                "player_id": p.player_id,
                "name": p.name,
                "student_id": p.student_id,
                "avatar": p.avatar,
                "is_bot": p.is_bot,
                "score": p.score,
                "streak": p.streak,
            }
            for i, p in enumerate(ranked)
        ]

    @staticmethod
    async def _safe_send(ws: WebSocket, payload: Dict[str, Any]) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        tasks = []
        if self.host_ws:
            tasks.append(self._safe_send(self.host_ws, payload))
        for p in list(self.participants.values()):
            if p.ws and not p.is_bot:
                tasks.append(self._safe_send(p.ws, payload))
        if tasks:
            await asyncio.gather(*tasks)


rooms: Dict[str, GameRoom] = {}
reserved_pins: Set[str] = set()


def generate_unique_pin() -> str:
    while True:
        pin = "".join(random.choices(string.digits, k=6))
        if pin not in rooms and pin not in reserved_pins:
            return pin


def reserve_pin() -> str:
    pin = generate_unique_pin()
    reserved_pins.add(pin)
    return pin


def _cancel_round_tasks(room: GameRoom) -> None:
    current = asyncio.current_task()
    if room.timer_task and room.timer_task is not current and not room.timer_task.done():
        room.timer_task.cancel()
    if room.auto_next_task and room.auto_next_task is not current and not room.auto_next_task.done():
        room.auto_next_task.cancel()
    for task in room.bot_tasks:
        if task is not current and not task.done():
            task.cancel()
    room.bot_tasks.clear()


async def _run_bot_turn(room: GameRoom, bot: Participant, question: Dict[str, Any], time_limit: int) -> None:
    if not bot.persona:
        return
    try:
        sim = await simulate_bot_answer(room.provider, bot.persona, question)
        wait_sec = max(1.0, min(sim.thinking_delay_sec, time_limit - 1.5))
        await asyncio.sleep(wait_sec)

        if room.status == "QUESTION" and bot.selected_option is None:
            bot.selected_option = sim.selected_option_index
            bot.response_time = round(time.time() - room.question_start_time, 2)
            await notify_answer_progress(room)
    except asyncio.CancelledError:
        pass


async def notify_answer_progress(room: GameRoom) -> None:
    answered = sum(1 for p in room.participants.values() if p.selected_option is not None)
    total = len(room.participants)
    if room.host_ws:
        await GameRoom._safe_send(room.host_ws, {"type": "ANSWER_COUNT", "answered": answered, "total": total})

    if total > 0 and answered == total:
        await finish_round(room)


async def _run_question_timer(room: GameRoom, duration: int) -> None:
    try:
        for remaining in range(duration, -1, -1):
            await room.broadcast({"type": "TIMER_TICK", "remaining": remaining, "total": duration})
            if remaining == 0:
                break
            await asyncio.sleep(1)
        await finish_round(room)
    except asyncio.CancelledError:
        pass


async def _auto_advance_after_result(room: GameRoom, expected_idx: int, delay: int = AUTO_NEXT_DELAY_SEC) -> None:
    try:
        for rem in range(delay, 0, -1):
            await room.broadcast({"type": "AUTO_NEXT_TICK", "remaining": rem, "total": delay})
            await asyncio.sleep(1)
        if room.status == "ROUND_RESULT" and room.current_idx == expected_idx:
            await start_next_question(room)
    except asyncio.CancelledError:
        pass


async def start_next_question(room: GameRoom) -> None:
    _cancel_round_tasks(room)
    room.current_idx += 1

    if room.current_idx >= len(room.questions):
        room.status = "PODIUM"
        await update_room_status(room.pin, "COMPLETED")
        leaderboard = room.get_leaderboard()
        rank_map = {row["player_id"]: row["rank"] for row in leaderboard}

        if room.host_ws:
            await GameRoom._safe_send(
                room.host_ws,
                {
                    "type": "PODIUM",
                    "leaderboard": leaderboard,
                    "model_label": room.model_label,
                },
            )

        # Unlock Final Standings & Mistakes Review for participants ONLY when entire quiz completes
        send_tasks = []
        for p in list(room.participants.values()):
            if not p.is_bot and p.ws:
                send_tasks.append(
                    GameRoom._safe_send(
                        p.ws,
                        {
                            "type": "PODIUM",
                            "leaderboard": leaderboard,
                            "model_label": room.model_label,
                            "your_rank": rank_map.get(p.player_id, 0),
                            "your_score": p.score,
                            "your_review": p.history,
                        },
                    )
                )
        if send_tasks:
            await asyncio.gather(*send_tasks)

        rem_sem = asyncio.Semaphore(3 if room.provider == "ollama" else 6)
        for p in list(room.participants.values()):
            if not p.is_bot and p.ws:
                asyncio.create_task(_build_and_send_remediation(room, p, rem_sem))
        return

    room.status = "QUESTION"
    await update_room_status(room.pin, "IN_PROGRESS")
    room.question_start_time = time.time()
    q = room.questions[room.current_idx]
    duration = int(q.get("time_limit", 20))

    for p in room.participants.values():
        p.selected_option = None
        p.response_time = 0.0

    await room.broadcast(
        {
            "type": "QUESTION_START",
            "question_number": room.current_idx + 1,
            "total_questions": len(room.questions),
            "question": q["question"],
            "options": q["options"],
            "max_marks": q["max_marks"],
            "time_limit": duration,
            "total_participants": len(room.participants),
        }
    )

    room.timer_task = asyncio.create_task(_run_question_timer(room, duration))
    for p in room.participants.values():
        if p.is_bot:
            room.bot_tasks.append(asyncio.create_task(_run_bot_turn(room, p, q, duration)))


async def finish_round(room: GameRoom) -> None:
    async with room.round_lock:
        if room.status != "QUESTION":
            return
        room.status = "ROUND_RESULT"
        current = asyncio.current_task()
        if room.timer_task and room.timer_task is not current and not room.timer_task.done():
            room.timer_task.cancel()
        for task in room.bot_tasks:
            if task is not current and not task.done():
                task.cancel()
        room.bot_tasks.clear()

    q = room.questions[room.current_idx]
    options: List[str] = q["options"]
    correct_idx = int(q["correct_option_index"])
    explanations: List[str] = q.get("option_explanations", [])
    time_limit = int(q.get("time_limit", 20))
    max_marks = int(q.get("max_marks", 2))
    participants_list = list(room.participants.values())

    per_player_feedback: Dict[str, Dict[str, Any]] = {}
    host_breakdown: List[Dict[str, Any]] = []
    db_batch: List[Dict[str, Any]] = []
    option_counts = [0, 0, 0, 0]

    for p in participants_list:
        sel = p.selected_option
        if sel is not None and 0 <= sel <= 3:
            option_counts[sel] += 1
            submitted_label = f"{OPTION_LETTERS[sel]}) {options[sel]}"
            is_correct = sel == correct_idx
            tutoring = explanations[sel] if sel < len(explanations) else q["reference_answer"]
        else:
            submitted_label = "(No Option Selected — Time Expired)"
            is_correct = False
            tutoring = f"Time ran out! Correct option was {OPTION_LETTERS[correct_idx]}: {options[correct_idx]}"

        if is_correct:
            awarded = max_marks
            speed_ratio = max(0.0, (time_limit - p.response_time) / time_limit)
            round_points = int(700 + speed_ratio * 300)
            p.streak += 1
            if p.streak >= 2:
                round_points += 100
        else:
            awarded = 0
            round_points = 0
            p.streak = 0

        p.score += round_points

        p.history.append(
            {
                "question_number": room.current_idx + 1,
                "question_id": q["question_id"],
                "question": q["question"],
                "submitted_answer": submitted_label,
                "reference_answer": q["reference_answer"],
                "is_correct": is_correct,
                "awarded_marks": awarded,
                "max_marks": max_marks,
                "micro_tutoring": tutoring,
            }
        )

        db_batch.append(
            {
                "game_pin": room.pin,
                "question_id": q["question_id"],
                "player_id": p.player_id,
                "player_name": p.name,
                "student_id": p.student_id,
                "is_bot": p.is_bot,
                "submitted_answer": submitted_label,
                "response_time_sec": p.response_time,
                "awarded_marks": awarded,
                "max_marks": max_marks,
                "round_score": round_points,
                "micro_tutoring": tutoring,
                "evidence_from_source": q["reference_answer"],
                "model_used": room.model_label,
            }
        )

        per_player_feedback[p.player_id] = {
            "selected_option": sel,
            "correct_option_index": correct_idx,
            "is_correct": is_correct,
            "micro_tutoring": tutoring,
        }

        host_breakdown.append(
            {
                "avatar": p.avatar,
                "name": p.name,
                "student_id": p.student_id,
                "is_bot": p.is_bot,
                "submitted_answer": submitted_label,
                "is_correct": is_correct,
                "awarded_marks": awarded,
                "max_marks": max_marks,
            }
        )

    await save_evaluations_batch(db_batch)
    leaderboard = room.get_leaderboard()

    bot_banter: Optional[str] = None
    active_bots = [p for p in participants_list if p.is_bot and p.persona]
    if active_bots:
        chosen_bot = random.choice(active_bots)
        top_summary = ", ".join([f"#{row['rank']} {row['name']} ({row['score']} pts)" for row in leaderboard[:3]])
        bot_banter = await generate_bot_banter(room.provider, chosen_bot.persona, top_summary)  # type: ignore

    is_last = room.current_idx == len(room.questions) - 1

    # Host receives full standings, breakdown, and banter anytime
    if room.host_ws:
        await GameRoom._safe_send(
            room.host_ws,
            {
                "type": "ROUND_RESULT",
                "correct_option_index": correct_idx,
                "options": options,
                "option_counts": option_counts,
                "reference_answer": q["reference_answer"],
                "leaderboard": leaderboard,
                "bot_banter": bot_banter,
                "breakdown": host_breakdown,
                "is_last_question": is_last,
                "auto_next_seconds": AUTO_NEXT_DELAY_SEC,
            },
        )

    # Participants receive ONLY explanation & auto-next timer (standings stay hidden until PODIUM)
    send_tasks = []
    for p in participants_list:
        if not p.is_bot and p.ws:
            send_tasks.append(
                GameRoom._safe_send(
                    p.ws,
                    {
                        "type": "ROUND_RESULT",
                        "reference_answer": q["reference_answer"],
                        "your_feedback": per_player_feedback[p.player_id],
                        "is_last_question": is_last,
                        "auto_next_seconds": AUTO_NEXT_DELAY_SEC,
                    },
                )
            )
    if send_tasks:
        await asyncio.gather(*send_tasks)

    room.auto_next_task = asyncio.create_task(_auto_advance_after_result(room, room.current_idx, AUTO_NEXT_DELAY_SEC))


async def _build_and_send_remediation(room: GameRoom, player: Participant, sem: asyncio.Semaphore) -> None:
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