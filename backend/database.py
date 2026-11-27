import json
import os
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import certifi
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "zapit_db").strip() or "zapit_db"

BASE_DIR = Path(__file__).resolve().parent.parent
SQLITE_PATH = BASE_DIR / "zapit_local.db"

_mongo_client: Optional[AsyncIOMotorClient] = None
_mongo_db = None
_use_mongo = False
_db_status_label = "Initializing..."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_database_status() -> Dict[str, Any]:
    return {
        "database": _db_status_label,
        "using_mongo": _use_mongo,
        "db_name": MONGO_DB_NAME if _use_mongo else SQLITE_PATH.name,
    }


async def _init_sqlite() -> None:
    async with aiosqlite.connect(str(SQLITE_PATH)) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS hosts (
                email TEXT PRIMARY KEY,
                name TEXT,
                password_hash TEXT,
                login_count INTEGER DEFAULT 1,
                created_at TEXT,
                last_login TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS host_login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                name TEXT,
                event_type TEXT,
                success INTEGER,
                ip_address TEXT,
                user_agent TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id TEXT PRIMARY KEY,
                game_pin TEXT,
                title TEXT,
                host_name TEXT,
                host_email TEXT,
                source_text TEXT,
                difficulty TEXT,
                provider TEXT,
                model_label TEXT,
                questions_json TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_rooms (
                game_pin TEXT PRIMARY KEY,
                quiz_id TEXT,
                title TEXT,
                host_email TEXT,
                difficulty TEXT,
                model_label TEXT,
                status TEXT,
                bots_json TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_pin TEXT,
                question_id INTEGER,
                player_id TEXT,
                player_name TEXT,
                student_id TEXT,
                is_bot INTEGER,
                submitted_answer TEXT,
                response_time_sec REAL,
                awarded_marks INTEGER,
                max_marks INTEGER,
                round_score INTEGER,
                micro_tutoring TEXT,
                evidence_from_source TEXT,
                model_used TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_pin TEXT,
                player_id TEXT,
                player_name TEXT,
                deck_json TEXT,
                model_used TEXT,
                created_at TEXT
            )
            """
        )
        for alter_sql in [
            "ALTER TABLE hosts ADD COLUMN login_count INTEGER DEFAULT 1",
            "ALTER TABLE quizzes ADD COLUMN host_email TEXT",
            "ALTER TABLE game_rooms ADD COLUMN host_email TEXT",
        ]:
            try:
                await db.execute(alter_sql)
            except Exception:
                pass
        await db.commit()


async def init_database() -> None:
    global _mongo_client, _mongo_db, _use_mongo, _db_status_label
    await _init_sqlite()

    if MONGO_URI:
        try:
            _mongo_client = AsyncIOMotorClient(
                MONGO_URI,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=5000,
            )
            await _mongo_client.admin.command("ping")
            _mongo_db = _mongo_client[MONGO_DB_NAME]
            _use_mongo = True
            _db_status_label = f"MongoDB Atlas ({MONGO_DB_NAME})"
            return
        except Exception as err:
            print(f"[ZapIt DB] MongoDB connection failed, falling back to SQLite: {err}")

    _use_mongo = False
    _db_status_label = "SQLite Local Fallback"


# ----------------- HOST ACCOUNTS & LOGIN AUDIT LOGS -----------------

async def get_host_by_email(email: str) -> Optional[Dict[str, Any]]:
    clean_email = email.strip().lower()
    if _use_mongo and _mongo_db is not None:
        return await _mongo_db["hosts"].find_one({"email": clean_email}, {"_id": 0})

    async with aiosqlite.connect(str(SQLITE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM hosts WHERE email = ?", (clean_email,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_host_account(email: str, name: str, password_hash: str) -> Dict[str, Any]:
    clean_email = email.strip().lower()
    clean_name = name.strip() or "Instructor"
    now = _now_iso()
    doc = {
        "email": clean_email,
        "name": clean_name,
        "password_hash": password_hash,
        "login_count": 1,
        "created_at": now,
        "last_login": now,
    }

    if _use_mongo and _mongo_db is not None:
        await _mongo_db["hosts"].insert_one(dict(doc))
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.execute(
                """
                INSERT INTO hosts (email, name, password_hash, login_count, created_at, last_login)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (clean_email, clean_name, password_hash, 1, now, now),
            )
            await db.commit()

    return {"email": clean_email, "name": clean_name, "created_at": now}


async def update_host_last_login(email: str) -> None:
    clean_email = email.strip().lower()
    now = _now_iso()
    if _use_mongo and _mongo_db is not None:
        await _mongo_db["hosts"].update_one(
            {"email": clean_email},
            {"$set": {"last_login": now}, "$inc": {"login_count": 1}},
        )
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.execute(
                "UPDATE hosts SET last_login = ?, login_count = COALESCE(login_count, 0) + 1 WHERE email = ?",
                (now, clean_email),
            )
            await db.commit()


async def record_host_login_log(
    email: str,
    name: str,
    event_type: str,  # "REGISTER" | "LOGIN_SUCCESS" | "LOGIN_FAILED"
    success: bool,
    ip_address: str = "unknown",
    user_agent: str = "unknown",
) -> None:
    clean_email = email.strip().lower()
    now = _now_iso()
    log_doc = {
        "email": clean_email,
        "name": name.strip() or "Unknown",
        "event_type": event_type,
        "success": success,
        "ip_address": ip_address[:64],
        "user_agent": user_agent[:240],
        "created_at": now,
    }

    if _use_mongo and _mongo_db is not None:
        await _mongo_db["host_login_logs"].insert_one(dict(log_doc))
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.execute(
                """
                INSERT INTO host_login_logs (email, name, event_type, success, ip_address, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_doc["email"],
                    log_doc["name"],
                    log_doc["event_type"],
                    1 if success else 0,
                    log_doc["ip_address"],
                    log_doc["user_agent"],
                    now,
                ),
            )
            await db.commit()


async def get_host_login_logs(email: str, limit: int = 25) -> List[Dict[str, Any]]:
    clean_email = email.strip().lower()
    if _use_mongo and _mongo_db is not None:
        cursor = (
            _mongo_db["host_login_logs"]
            .find({"email": clean_email}, {"_id": 0})
            .sort("created_at", -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async with aiosqlite.connect(str(SQLITE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT email, name, event_type, success, ip_address, user_agent, created_at FROM host_login_logs WHERE email = ? ORDER BY id DESC LIMIT ?",
            (clean_email, limit),
        ) as cur:
            rows = await cur.fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["success"] = bool(d.get("success"))
                out.append(d)
            return out


# ----------------- QUIZZES, ROOMS & EVALUATIONS -----------------

async def save_published_quiz(
    game_pin: str,
    title: str,
    host_name: str,
    source_text: str,
    difficulty: str,
    provider: str,
    model_label: str,
    questions: List[Dict[str, Any]],
    bots: List[str],
    host_email: str = "",
) -> str:
    quiz_id = "qz_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    created_at = _now_iso()
    clean_email = host_email.strip().lower()

    quiz_doc = {
        "quiz_id": quiz_id,
        "game_pin": game_pin,
        "title": title,
        "host_name": host_name,
        "host_email": clean_email,
        "source_text": source_text,
        "difficulty": difficulty,
        "provider": provider,
        "model_label": model_label,
        "questions": questions,
        "created_at": created_at,
    }
    room_doc = {
        "game_pin": game_pin,
        "quiz_id": quiz_id,
        "title": title,
        "host_email": clean_email,
        "difficulty": difficulty,
        "model_label": model_label,
        "status": "LOBBY",
        "bots": bots,
        "created_at": created_at,
    }

    if _use_mongo and _mongo_db is not None:
        await _mongo_db["quizzes"].insert_one(dict(quiz_doc))
        await _mongo_db["game_rooms"].insert_one(dict(room_doc))
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.execute(
                """
                INSERT INTO quizzes (quiz_id, game_pin, title, host_name, host_email, source_text, difficulty, provider, model_label, questions_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quiz_id,
                    game_pin,
                    title,
                    host_name,
                    clean_email,
                    source_text,
                    difficulty,
                    provider,
                    model_label,
                    json.dumps(questions),
                    created_at,
                ),
            )
            await db.execute(
                """
                INSERT OR REPLACE INTO game_rooms (game_pin, quiz_id, title, host_email, difficulty, model_label, status, bots_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_pin,
                    quiz_id,
                    title,
                    clean_email,
                    difficulty,
                    model_label,
                    "LOBBY",
                    json.dumps(bots),
                    created_at,
                ),
            )
            await db.commit()

    return quiz_id


async def update_room_status(game_pin: str, status: str) -> None:
    if _use_mongo and _mongo_db is not None:
        await _mongo_db["game_rooms"].update_one(
            {"game_pin": game_pin},
            {"$set": {"status": status}},
        )
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.execute(
                "UPDATE game_rooms SET status = ? WHERE game_pin = ?",
                (status, game_pin),
            )
            await db.commit()


async def save_evaluations_batch(records: List[Dict[str, Any]]) -> None:
    if not records:
        return
    created_at = _now_iso()
    docs = [{**r, "created_at": created_at} for r in records]

    if _use_mongo and _mongo_db is not None:
        await _mongo_db["evaluations"].insert_many([dict(d) for d in docs])
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.executemany(
                """
                INSERT INTO evaluations (
                    game_pin, question_id, player_id, player_name, student_id, is_bot,
                    submitted_answer, response_time_sec, awarded_marks, max_marks,
                    round_score, micro_tutoring, evidence_from_source, model_used, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        d["game_pin"],
                        d["question_id"],
                        d["player_id"],
                        d["player_name"],
                        d["student_id"],
                        1 if d.get("is_bot") else 0,
                        d["submitted_answer"],
                        d["response_time_sec"],
                        d["awarded_marks"],
                        d["max_marks"],
                        d["round_score"],
                        d["micro_tutoring"],
                        d["evidence_from_source"],
                        d["model_used"],
                        d["created_at"],
                    )
                    for d in docs
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
    created_at = _now_iso()
    if _use_mongo and _mongo_db is not None:
        await _mongo_db["remediation_decks"].insert_one(
            {
                "game_pin": game_pin,
                "player_id": player_id,
                "player_name": player_name,
                "deck": deck,
                "model_used": model_used,
                "created_at": created_at,
            }
        )
    else:
        async with aiosqlite.connect(str(SQLITE_PATH)) as db:
            await db.execute(
                """
                INSERT INTO remediation_decks (game_pin, player_id, player_name, deck_json, model_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (game_pin, player_id, player_name, json.dumps(deck), model_used, created_at),
            )
            await db.commit()


async def get_host_history(limit: int = 25, host_email: Optional[str] = None) -> List[Dict[str, Any]]:
    clean_email = (host_email or "").strip().lower()
    if _use_mongo and _mongo_db is not None:
        query = {"host_email": clean_email} if clean_email else {}
        cursor = _mongo_db["game_rooms"].find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async with aiosqlite.connect(str(SQLITE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if clean_email:
            sql = "SELECT game_pin, quiz_id, title, difficulty, model_label, status, created_at FROM game_rooms WHERE host_email = ? ORDER BY created_at DESC LIMIT ?"
            params = (clean_email, limit)
        else:
            sql = "SELECT game_pin, quiz_id, title, difficulty, model_label, status, created_at FROM game_rooms ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_room_report(game_pin: str) -> Optional[Dict[str, Any]]:
    if _use_mongo and _mongo_db is not None:
        room = await _mongo_db["game_rooms"].find_one({"game_pin": game_pin}, {"_id": 0})
        if not room:
            return None
        evals = await _mongo_db["evaluations"].find({"game_pin": game_pin}, {"_id": 0}).to_list(length=2000)
        return {
            "storage": _db_status_label,
            "room": room,
            "evaluations": evals,
        }

    async with aiosqlite.connect(str(SQLITE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM game_rooms WHERE game_pin = ?", (game_pin,)) as cur:
            room_row = await cur.fetchone()
            if not room_row:
                return None
            room = dict(room_row)

        async with db.execute(
            "SELECT * FROM evaluations WHERE game_pin = ? ORDER BY question_id ASC, round_score DESC",
            (game_pin,),
        ) as cur:
            eval_rows = await cur.fetchall()
            evaluations = []
            for r in eval_rows:
                item = dict(r)
                item["is_bot"] = bool(item.get("is_bot"))
                evaluations.append(item)

        return {
            "storage": _db_status_label,
            "room": room,
            "evaluations": evaluations,
        }