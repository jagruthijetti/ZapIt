import random
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from backend.doc_parser import extract_text_from_file
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.ai_engine import BOT_PROFILES, evaluate_player_answer, generate_quiz_from_source
from backend.database import (
    get_match_report,
    init_db,
    is_mongo_enabled,
    list_recent_rooms,
    save_quiz_and_room,
)
from backend.game_manager import (
    AVATARS,
    MAX_PARTICIPANTS_PER_ROOM,
    GameRoom,
    Participant,
    finish_round,
    generate_unique_pin,
    notify_answer_progress,
    reserved_pins,
    rooms,
    start_next_question,
)
from backend.llm_router import LLMRouterError, get_model_label
from backend.schemas import GenerateQuizRequest, ProviderType, PublishQuizRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="ZapIt — Generative AI Live Quiz Arena", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def disable_static_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


class ReservePinRequest(BaseModel):
    provider: ProviderType = "ollama"
    question_count: Literal[5, 10, 15] = 5


class RedemptionVerifyRequest(BaseModel):
    provider: ProviderType
    question: str
    reference_answer: str
    student_answer: str


# --- SEPARATE ENTRY PAGES FOR PARTICIPANTS VS HOST ---
@app.get("/")
@app.get("/play")
async def serve_participant_page():
    return FileResponse("static/play.html")


@app.get("/host")
async def serve_host_page():
    return FileResponse("static/host.html")


@app.get("/api/system/status")
async def api_system_status():
    return {
        "app": "ZapIt",
        "database": "MongoDB Atlas" if is_mongo_enabled() else "SQLite Local Fallback",
    }


@app.get("/api/rooms/{pin}/check")
async def api_check_room(pin: str):
    clean_pin = pin.strip()
    room = rooms.get(clean_pin)
    if not room:
        raise HTTPException(
            status_code=404,
            detail="Quiz PIN not found. Make sure you entered the exact 6-digit PIN from the host's screen.",
        )
    if room.status == "PODIUM":
        raise HTTPException(status_code=403, detail="This quiz match has already ended.")
    if len(room.participants) >= MAX_PARTICIPANTS_PER_ROOM:
        raise HTTPException(status_code=403, detail="This quiz room has reached maximum capacity.")
    return {"ok": True, "title": room.title, "status": room.status}


@app.get("/api/rooms/{pin}/state")
async def api_participant_room_state(pin: str):
    """Safe state snapshot for participants (no standings exposed before PODIUM)."""
    room = rooms.get(pin.strip())
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    if room.status == "QUESTION" and 0 <= room.current_idx < len(room.questions):
        q = room.questions[room.current_idx]
        duration = int(q.get("time_limit", 20))
        rem = max(1, duration - int(time.time() - room.question_start_time))
        return {
            "status": "QUESTION",
            "question_number": room.current_idx + 1,
            "total_questions": len(room.questions),
            "question": q["question"],
            "options": q["options"],
            "max_marks": q["max_marks"],
            "time_limit": rem,
            "total_participants": len(room.participants),
        }

    return {"status": room.status}


@app.post("/api/rooms/reserve")
async def api_reserve_pin(req: ReservePinRequest):
    pin = generate_unique_pin()
    staging_room = GameRoom(
        pin=pin,
        quiz_id="preparing",
        title="Preparing Quiz...",
        source_text="",
        provider=req.provider,
        questions=[],
        bots=[],
    )
    staging_room.status = "PREPARING"
    rooms[pin] = staging_room

    est_seconds = (3 + req.question_count) if req.provider == "gemini" else (6 + req.question_count * 2)
    return {
        "game_pin": pin,
        "estimated_seconds": est_seconds,
        "model_label": get_model_label(req.provider),
    }

@app.post("/api/documents/extract")
async def api_extract_document(file: UploadFile = File(...)):
    try:
        raw_bytes = await file.read()
        extracted = extract_text_from_file(file.filename or "notes.txt", raw_bytes)
        if len(extracted) < 20:
            raise HTTPException(
                status_code=400,
                detail="Could not find enough readable text in that file (scanned image PDFs are not supported). Try a text-based PPTX, DOCX, PDF, or paste notes directly.",
            )
        return {
            "filename": file.filename,
            "extracted_text": extracted[:30000],
            "char_count": len(extracted),
            "truncated": len(extracted) > 30000,
        }
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except HTTPException:
        raise
    except Exception as err:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to read file: {err}")

@app.post("/api/quizzes/generate")
async def api_generate_quiz(req: GenerateQuizRequest):
    try:
        quiz_data, model_label = await generate_quiz_from_source(
            provider=req.provider,
            title=req.title,
            source_text=req.source_text,
            question_count=req.question_count,
            difficulty=req.difficulty,
        )
        return {
            "questions": [q.model_dump() for q in quiz_data.questions],
            "model_label": model_label,
        }
    except LLMRouterError as err:
        raise HTTPException(status_code=502, detail=str(err))
    except Exception as err:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"{type(err).__name__}: {err}")


@app.post("/api/quizzes/publish")
async def api_publish_quiz(req: PublishQuizRequest):
    if not req.questions:
        raise HTTPException(status_code=400, detail="At least one question is required to publish.")

    quiz_id = f"qz_{uuid.uuid4().hex[:8]}"
    pin = req.reserved_pin if (req.reserved_pin and req.reserved_pin in rooms) else generate_unique_pin()
    reserved_pins.discard(pin)

    model_label = get_model_label(req.provider)
    questions_dicts = [q.model_dump() for q in req.questions]

    await save_quiz_and_room(
        quiz_id=quiz_id,
        game_pin=pin,
        title=req.title,
        source_text=req.source_text,
        difficulty=req.difficulty,
        provider=req.provider,
        model_label=model_label,
        host_name=req.host_name,
        questions=questions_dicts,
        bots=req.bots,
    )

    existing = rooms.get(pin)
    if existing:
        existing.quiz_id = quiz_id
        existing.title = req.title
        existing.source_text = req.source_text
        existing.provider = req.provider
        existing.model_label = model_label
        existing.questions = questions_dicts
        existing.status = "LOBBY"
        for idx, persona in enumerate(req.bots, start=1):
            profile = BOT_PROFILES[persona]
            bot_id = f"bot_{persona}_{idx}"
            if bot_id not in existing.participants:
                existing.participants[bot_id] = Participant(
                    player_id=bot_id,
                    name=profile["name"],
                    student_id=profile["student_id"],
                    avatar=profile["avatar"],
                    is_bot=True,
                    persona=persona,
                )
    else:
        rooms[pin] = GameRoom(
            pin=pin,
            quiz_id=quiz_id,
            title=req.title,
            source_text=req.source_text,
            provider=req.provider,
            questions=questions_dicts,
            bots=req.bots,
        )

    return {"quiz_id": quiz_id, "game_pin": pin, "model_label": model_label}


@app.post("/api/redemption/verify")
async def api_verify_redemption(req: RedemptionVerifyRequest):
    q_stub = {
        "question": req.question,
        "reference_answer": req.reference_answer,
        "max_marks": 2,
    }
    eval_res, model_label = await evaluate_player_answer(
        provider=req.provider,
        source_text=req.reference_answer,
        question=q_stub,
        submitted_answer=req.student_answer,
    )
    return {"evaluation": eval_res.model_dump(), "model_label": model_label}


# --- HOST-ONLY HISTORY & REPORT ENDPOINTS ---
@app.get("/api/host/history")
async def api_host_history():
    return {"sessions": await list_recent_rooms(limit=20)}


@app.get("/api/host/reports/{game_pin}")
async def api_get_report(game_pin: str):
    report = await get_match_report(game_pin.strip())
    if not report:
        raise HTTPException(status_code=404, detail="No saved match report found for that Quiz PIN.")
    return report


# --- WEBSOCKETS ---
@app.websocket("/ws/{pin}/host")
async def host_websocket(websocket: WebSocket, pin: str):
    await websocket.accept()
    room = rooms.get(pin)
    if not room:
        await websocket.close(code=4004)
        return
    room.host_ws = websocket
    await websocket.send_json(
        {
            "type": "LOBBY_UPDATE",
            "players": room.get_leaderboard(),
            "title": room.title,
            "model_label": room.model_label,
        }
    )

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "START_GAME" and room.status == "LOBBY":
                await start_next_question(room)
            elif action == "END_QUESTION" and room.status == "QUESTION":
                await finish_round(room)
            elif action == "NEXT_QUESTION" and room.status == "ROUND_RESULT":
                await start_next_question(room)
    except WebSocketDisconnect:
        room.host_ws = None


@app.websocket("/ws/{pin}/player")
async def player_websocket(websocket: WebSocket, pin: str, name: str = "Student", student_id: str = "ID-00"):
    await websocket.accept()

    room = rooms.get(pin)
    if not room or room.status == "PODIUM":
        await websocket.close(code=4003)
        return
    if len(room.participants) >= MAX_PARTICIPANTS_PER_ROOM:
        await websocket.close(code=4006)
        return

    player_id = f"p_{random.randint(100000, 999999)}"
    avatar = AVATARS[len(room.participants) % len(AVATARS)]
    participant = Participant(
        player_id=player_id,
        name=name.strip()[:24] or "Student",
        student_id=student_id.strip()[:24] or "ID-00",
        avatar=avatar,
        is_bot=False,
        ws=websocket,
    )
    room.participants[player_id] = participant

    await websocket.send_json(
        {
            "type": "JOINED",
            "player_id": player_id,
            "avatar": avatar,
            "title": room.title,
            "model_label": room.model_label,
            "provider": room.provider,
        }
    )

    if room.status == "QUESTION" and 0 <= room.current_idx < len(room.questions):
        q = room.questions[room.current_idx]
        duration = int(q.get("time_limit", 20))
        rem = max(1, duration - int(time.time() - room.question_start_time))
        await websocket.send_json(
            {
                "type": "QUESTION_START",
                "question_number": room.current_idx + 1,
                "total_questions": len(room.questions),
                "question": q["question"],
                "options": q["options"],
                "max_marks": q["max_marks"],
                "time_limit": rem,
                "total_participants": len(room.participants),
            }
        )

    if room.host_ws:
        await GameRoom._safe_send(room.host_ws, {"type": "LOBBY_UPDATE", "players": room.get_leaderboard()})

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "SUBMIT_ANSWER" and room.status == "QUESTION":
                if participant.selected_option is None:
                    opt_idx = int(data.get("option_index", -1))
                    if 0 <= opt_idx <= 3:
                        participant.selected_option = opt_idx
                        participant.response_time = round(time.time() - room.question_start_time, 2)
                        await notify_answer_progress(room)
            elif action == "SYNC_STATE" and room.status == "QUESTION" and 0 <= room.current_idx < len(room.questions):
                q = room.questions[room.current_idx]
                duration = int(q.get("time_limit", 20))
                rem = max(1, duration - int(time.time() - room.question_start_time))
                await websocket.send_json(
                    {
                        "type": "QUESTION_START",
                        "question_number": room.current_idx + 1,
                        "total_questions": len(room.questions),
                        "question": q["question"],
                        "options": q["options"],
                        "max_marks": q["max_marks"],
                        "time_limit": rem,
                        "total_participants": len(room.participants),
                    }
                )
    except WebSocketDisconnect:
        room.participants.pop(player_id, None)
        if room.host_ws:
            await GameRoom._safe_send(room.host_ws, {"type": "LOBBY_UPDATE", "players": room.get_leaderboard()})