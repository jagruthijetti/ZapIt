import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
from motor.motor_asyncio import AsyncIOMotorClient

from backend.config import settings

_mongo_client: Optional[AsyncIOMotorClient] = None
_mongo_db = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_mongo_enabled() -> bool:
    uri = (settings.MONGODB_URI or "").strip()
    return bool(uri) and "your_user" not in uri and uri.startswith("mongodb")


async def init_db() -> None:
    global _mongo_client, _mongo_db
    if is_mongo_enabled():
        _mongo_client = AsyncIOMotorClient(settings.MONGODB_URI, serverSelectionTimeoutMS=5000)
        _mongo_db = _mongo_client[settings.MONGODB_DB_NAME]
        await _mongo_client.admin.command("ping")
        print(f"[ZapIt DB] Connected to MongoDB Atlas -> {settings.MONGODB_DB_NAME}", flush=True)
        return

    db_dir = os.path.dirname(settings.SQLITE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_text TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_label TEXT NOT NULL,
                host_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                reference_answer TEXT NOT NULL,
                max_marks INTEGER NOT NULL,
                rubric_json TEXT NOT NULL,
                misconceptions_json TEXT NOT NULL,
                time_limit INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_rooms (
                game_pin TEXT PRIMARY KEY,
                quiz_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'ZapIt Quiz',
                status TEXT NOT NULL,
                bots_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_pin TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                student_id TEXT NOT NULL,
                is_bot INTEGER NOT NULL DEFAULT 0,
                submitted_answer TEXT NOT NULL,
                response_time_sec REAL NOT NULL,
                awarded_marks INTEGER NOT NULL,
                max_marks INTEGER NOT NULL,
                round_score INTEGER NOT NULL,
                micro_tutoring TEXT NOT NULL,
                evidence_from_source TEXT NOT NULL,
                model_used TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS remediation_decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_pin TEXT NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                deck_json TEXT NOT NULL,
                model_used TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        try:
            await db.execute("ALTER TABLE game_rooms ADD COLUMN title TEXT NOT NULL DEFAULT 'ZapIt Quiz'")
        except Exception:
            pass
        await db.commit()
    print(f"[ZapIt DB] Using local SQLite fallback -> {settings.SQLITE_DB_PATH}", flush=True)


async def save_quiz_and_room(
    quiz_id: str,
    game_pin: str,
    title: str,
    source_text: str,
    difficulty: str,
    provider: str,
    model_label: str,
    host_name: str,
    questions: List[Dict[str, Any]],
    bots: List[str],
) -> None:
    now = _now_iso()
    if _mongo_db is not None:
        await _mongo_db.quizzes.insert_one(
            {
                "quiz_id": quiz_id,
                "title": title,
                "source_text": source_text,
                "difficulty": difficulty,
                "provider": provider,
                "model_label": model_label,
                "host_name": host_name,
                "questions": questions,
                "created_at": now,
            }
        )
        await _mongo_db.game_rooms.insert_one(
            {
                "game_pin": game_pin,
                "quiz_id": quiz_id,
                "title": title,
                "difficulty": difficulty,
                "model_label": model_label,
                "status": "LOBBY",
                "bots": bots,
                "created_at": now,
            }
        )
        return

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO quizzes (quiz_id, title, source_text, difficulty, provider, model_label, host_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (quiz_id, title, source_text, difficulty, provider, model_label, host_name, now),
        )
        for q in questions:
            await db.execute(
                """
                INSERT INTO questions (
                    quiz_id, question_id, question, reference_answer, max_marks,
                    rubric_json, misconceptions_json, time_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quiz_id,
                    q["question_id"],
                    q["question"],
                    q["reference_answer"],
                    q["max_marks"],
                    json.dumps(q.get("options", [])),
                    json.dumps(q.get("option_explanations", [])),
                    q.get("time_limit", 20),
                ),
            )
        await db.execute(
            """
            INSERT OR REPLACE INTO game_rooms (game_pin, quiz_id, title, status, bots_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (game_pin, quiz_id, title, "LOBBY", json.dumps(bots), now),
        )
        await db.commit()


async def update_room_status(game_pin: str, status: str) -> None:
    if _mongo_db is not None:
        await _mongo_db.game_rooms.update_one({"game_pin": game_pin}, {"$set": {"status": status}})
        return

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        await db.execute("UPDATE game_rooms SET status = ? WHERE game_pin = ?", (status, game_pin))
        await db.commit()


async def save_evaluations_batch(records: List[Dict[str, Any]]) -> None:
    if not records:
        return
    now = _now_iso()
    for r in records:
        r["created_at"] = now

    if _mongo_db is not None:
        await _mongo_db.evaluations.insert_many([dict(r) for r in records])
        return

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        await db.executemany(
            """
            INSERT INTO evaluations (
                game_pin, question_id, player_id, player_name, student_id, is_bot,
                submitted_answer, response_time_sec, awarded_marks, max_marks,
                round_score, micro_tutoring, evidence_from_source, model_used, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["game_pin"],
                    r["question_id"],
                    r["player_id"],
                    r["player_name"],
                    r["student_id"],
                    1 if r["is_bot"] else 0,
                    r["submitted_answer"],
                    r["response_time_sec"],
                    r["awarded_marks"],
                    r["max_marks"],
                    r["round_score"],
                    r["micro_tutoring"],
                    r["evidence_from_source"],
                    r["model_used"],
                    r["created_at"],
                )
                for r in records
            ],
        )
        await db.commit()


async def save_remediation_deck(
    game_pin: str,
    player_id: str,
    player_name: str,
    deck: Dict[str, Any],
    model_used: str,
) -> None:
    now = _now_iso()
    if _mongo_db is not None:
        await _mongo_db.remediation_decks.insert_one(
            {
                "game_pin": game_pin,
                "player_id": player_id,
                "player_name": player_name,
                "deck": deck,
                "model_used": model_used,
                "created_at": now,
            }
        )
        return

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        await db.execute(
            "INSERT INTO remediation_decks (game_pin, player_id, player_name, deck_json, model_used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (game_pin, player_id, player_name, json.dumps(deck), model_used, now),
        )
        await db.commit()


async def list_recent_rooms(limit: int = 15) -> List[Dict[str, Any]]:
    if _mongo_db is not None:
        cursor = _mongo_db.game_rooms.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT r.game_pin, r.quiz_id, r.title, r.status, r.created_at, q.difficulty, q.model_label
            FROM game_rooms r
            LEFT JOIN quizzes q ON r.quiz_id = q.quiz_id
            ORDER BY r.created_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def get_match_report(game_pin: str) -> Optional[Dict[str, Any]]:
    if _mongo_db is not None:
        room = await _mongo_db.game_rooms.find_one({"game_pin": game_pin}, {"_id": 0})
        if not room:
            return None
        evals = await _mongo_db.evaluations.find({"game_pin": game_pin}, {"_id": 0}).to_list(length=2000)
        decks = await _mongo_db.remediation_decks.find({"game_pin": game_pin}, {"_id": 0}).to_list(length=200)
        return {"storage": "MongoDB Atlas", "room": room, "evaluations": evals, "remediation_decks": decks}

    async with aiosqlite.connect(settings.SQLITE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT r.game_pin, r.status, r.created_at, r.title, q.difficulty, q.model_label
            FROM game_rooms r
            LEFT JOIN quizzes q ON r.quiz_id = q.quiz_id
            WHERE r.game_pin = ?
            """,
            (game_pin,),
        )
        room = await cur.fetchone()
        if not room:
            return None
        ev_cur = await db.execute(
            "SELECT * FROM evaluations WHERE game_pin = ? ORDER BY question_id, round_score DESC",
            (game_pin,),
        )
        evals = [dict(row) for row in await ev_cur.fetchall()]
        deck_cur = await db.execute(
            "SELECT player_id, player_name, deck_json, model_used FROM remediation_decks WHERE game_pin = ?",
            (game_pin,),
        )
        decks = [
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "deck": json.loads(row["deck_json"]),
                "model_used": row["model_used"],
            }
            for row in await deck_cur.fetchall()
        ]
        return {"storage": "SQLite Local", "room": dict(room), "evaluations": evals, "remediation_decks": decks}