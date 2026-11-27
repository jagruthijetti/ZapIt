import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from backend.ai_engine import evaluate_player_answer, generate_quiz_from_source
from backend.database import (
    get_database_status,
    get_host_history,
    get_room_report,
    init_database,
    save_published_quiz,
)
from backend.doc_parser import extract_text_from_file
from backend.game_manager import (
    MAX_PARTICIPANTS_PER_ROOM,
    add_human_participant,
    create_room,
    finalize_current_question,
    finish_round,
    get_room,
    is_pin_reserved,
    notify_answer_progress,
    record_answer_submission,
    reserve_pin,
    start_next_question,
)
from backend.llm_router import LLMRouterError, get_model_label
from backend.schemas import (
    GenerateQuizRequest,
    PublishQuizRequest,
    RedemptionVerifyRequest,
    ReservePinRequest,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    yield


app = FastAPI(title="ZapIt — AI Live Assessment Platform", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal Server Error: {type(exc).__name__}: {exc}"},
    )


# ----------------- HTML PORTALS -----------------

@app.get("/")
async def serve_participant_portal():
    return FileResponse(str(STATIC_DIR / "play.html"))


@app.get("/host")
async def serve_host_studio():
    return FileResponse(str(STATIC_DIR / "host.html"))


# ----------------- SYSTEM & DOCUMENT EXTRACTION -----------------

@app.get("/api/system/status")
async def api_system_status():
    return get_database_status()


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


# ----------------- HOST QUIZ AUTHORING & LOBBY -----------------

@app.post("/api/rooms/reserve")
async def api_reserve_pin(req: ReservePinRequest):
    pin = reserve_pin()
    est_sec = max(6, req.question_count * (2 if req.provider == "gemini" else 4))
    return {"game_pin": pin, "estimated_seconds": est_sec}


@app.post("/api/quizzes/generate")
async def api_generate_quiz(req: GenerateQuizRequest):
    try:
        quiz_data, model_label = await generate_quiz_from_source(
            provider=req.provider,
            title=req.title,
            source_text=req.source_text,
            question_count=req.question_count,
            difficulty=req.difficulty,
            default_time_limit=req.default_time_limit,
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
    try:
        model_label = get_model_label(req.provider)
        questions_dicts = [q.model_dump() for q in req.questions]
        room = create_room(
            quiz_id="",
            title=req.title,
            source_text=req.source_text,
            difficulty=req.difficulty,
            provider=req.provider,
            model_label=model_label,
            questions=questions_dicts,
            bots=req.bots,
            reserved_pin=req.reserved_pin,
        )
        quiz_id = await save_published_quiz(
            game_pin=room.pin,
            title=req.title,
            host_name=req.host_name,
            source_text=req.source_text,
            difficulty=req.difficulty,
            provider=req.provider,
            model_label=model_label,
            questions=questions_dicts,
            bots=req.bots,
        )
        room.quiz_id = quiz_id
        return {"game_pin": room.pin, "quiz_id": quiz_id}
    except Exception as err:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to publish quiz: {err}")


# ----------------- PARTICIPANT JOIN CHECK & STATE SYNC -----------------

@app.get("/api/rooms/{pin}/check")
async def api_check_room(pin: str):
    clean_pin = (pin or "").strip()
    room = get_room(clean_pin)
    if room:
        if len(room.participants) >= MAX_PARTICIPANTS_PER_ROOM:
            raise HTTPException(
                status_code=403,
                detail=f"Room {clean_pin} has reached maximum capacity ({MAX_PARTICIPANTS_PER_ROOM} participants).",
            )
        return {
            "exists": True,
            "status": room.status,
            "title": room.title,
            "participant_count": len(room.participants),
        }
    if is_pin_reserved(clean_pin):
        return {
            "exists": True,
            "status": "PREPARING",
            "title": "Quiz is being prepared by Host...",
            "participant_count": 0,
        }
    raise HTTPException(status_code=404, detail=f"No active quiz room found with PIN {clean_pin}.")


@app.get("/api/rooms/{pin}/state")
async def api_room_state_sync(pin: str, player_id: Optional[str] = Query(default=None)):
    room = get_room(pin)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found.")

    base = {
        "status": room.status,
        "title": room.title,
        "question_number": room.current_index + 1,
        "total_questions": len(room.questions),
    }

    if room.status == "IN_PROGRESS" and 0 <= room.current_index < len(room.questions):
        q = room.questions[room.current_index]
        time_limit = int(q.get("time_limit", 20))
        elapsed = time.monotonic() - room.question_started_at
        remaining = max(0, int(round(time_limit - elapsed)))
        base.update(
            {
                "question_id": q["question_id"],
                "question": q["question"],
                "options": q.get("options", []),
                "max_marks": q["max_marks"],
                "time_limit": time_limit,
                "remaining": remaining,
                "already_submitted": bool(player_id and player_id in room.current_submissions),
            }
        )
    elif room.status == "COMPLETED":
        base["leaderboard"] = room.get_leaderboard()

    return base


@app.post("/api/redemption/verify")
async def api_verify_redemption(req: RedemptionVerifyRequest):
    room = get_room(req.game_pin)
    source_text = room.source_text if room else ""
    provider = room.provider if room else "gemini"
    question_dict = {
        "question": req.question,
        "reference_answer": req.reference_answer,
    }
    try:
        res, model_label = await evaluate_player_answer(
            provider=provider,
            source_text=source_text,
            question=question_dict,
            submitted_answer=req.student_answer,
        )
        return {
            "is_correct": res.is_correct,
            "awarded_marks": res.awarded_marks,
            "micro_tutoring": res.micro_tutoring,
            "model_used": model_label,
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


# ----------------- HOST HISTORY & REPORTS -----------------

@app.get("/api/host/history")
async def api_host_history():
    sessions = await get_host_history(limit=25)
    return {"sessions": sessions}


@app.get("/api/host/reports/{pin}")
async def api_host_report(pin: str):
    report = await get_room_report(pin)
    if not report:
        raise HTTPException(status_code=404, detail="Match report not found.")
    return report


# ----------------- WEBSOCKETS (HOST & PARTICIPANT) -----------------

@app.websocket("/ws/{pin}/host")
async def ws_host_endpoint(websocket: WebSocket, pin: str):
    await websocket.accept()
    room = get_room(pin)
    if not room:
        await websocket.send_json({"type": "ERROR", "detail": "Room not found."})
        await websocket.close()
        return

    room.host_ws = websocket
    await websocket.send_json(
        {
            "type": "LOBBY_UPDATE",
            "players": room.get_public_players(),
            "status": room.status,
        }
    )

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "START_GAME" and room.status == "LOBBY":
                await start_next_question(room)
            elif action == "END_QUESTION" and room.status == "IN_PROGRESS":
                await finish_round(room)
            elif action == "NEXT_QUESTION" and room.status == "ROUND_REVIEW":
                await start_next_question(room)
    except WebSocketDisconnect:
        if room.host_ws == websocket:
            room.host_ws = None
    except Exception:
        if room.host_ws == websocket:
            room.host_ws = None


@app.websocket("/ws/{pin}/player")
async def ws_player_endpoint(
    websocket: WebSocket,
    pin: str,
    name: str = Query(default="Player"),
    student_id: str = Query(default="STU-000"),
):
    await websocket.accept()
    room = get_room(pin)
    if not room:
        await websocket.send_json({"type": "ERROR", "detail": f"Quiz room {pin} not found."})
        await websocket.close()
        return

    try:
        participant = add_human_participant(room, name=name, student_id=student_id, ws=websocket)
    except ValueError as cap_err:
        await websocket.send_json({"type": "ERROR", "detail": str(cap_err)})
        await websocket.close()
        return

    await websocket.send_json(
        {
            "type": "JOINED_OK",
            "player_id": participant.player_id,
            "name": participant.name,
            "student_id": participant.student_id,
            "avatar": participant.avatar,
            "title": room.title,
            "status": room.status,
        }
    )

    await room.broadcast_to_all(
        {
            "type": "LOBBY_UPDATE",
            "players": room.get_public_players(),
            "status": room.status,
        }
    )

    # If a student joins mid-question, immediately sync the active question to them
    if room.status == "IN_PROGRESS" and 0 <= room.current_index < len(room.questions):
        q = room.questions[room.current_index]
        time_limit = int(q.get("time_limit", 20))
        elapsed = time.monotonic() - room.question_started_at
        remaining = max(1, int(round(time_limit - elapsed)))
        await websocket.send_json(
            {
                "type": "QUESTION_START",
                "question_number": room.current_index + 1,
                "total_questions": len(room.questions),
                "question_id": q["question_id"],
                "question": q["question"],
                "options": q.get("options", []),
                "max_marks": q["max_marks"],
                "time_limit": remaining,
                "total_participants": len(room.participants),
            }
        )

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("action") == "SUBMIT_ANSWER":
                sel_idx = int(data.get("selected_option_index", -1))
                accepted = await record_answer_submission(room, participant.player_id, sel_idx)
                if accepted:
                    await websocket.send_json({"type": "ANSWER_LOCKED"})
    except WebSocketDisconnect:
        participant.ws = None
    except Exception:
        participant.ws = None