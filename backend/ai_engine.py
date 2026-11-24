import random
import re
from typing import Any, Dict, List, Tuple
from pydantic import BaseModel

from backend.llm_router import generate_structured, get_model_label
from backend.schemas import (
    AnswerEvaluationResponse,
    BotAnswerResponse,
    BotBanterResponse,
    BotPersonaType,
    DifficultyType,
    GeneratedQuestion,
    ProviderType,
    QuizGenerationResponse,
    RemediationDeckResponse,
)

OPTION_LETTERS = ["A", "B", "C", "D"]

BOT_PROFILES: Dict[BotPersonaType, Dict[str, Any]] = {
    "speedrunner": {
        "name": "ByteBlitz (Bot)",
        "student_id": "AI-BOT-01",
        "avatar": "⚡",
        "accuracy": 0.65,
        "style": "Overconfident speedrunner who answers fast and loves playful rivalry.",
        "min_delay": 2.0,
        "max_delay": 5.0,
    },
    "professor": {
        "name": "Prof. Qwenstein (Bot)",
        "student_id": "AI-BOT-02",
        "avatar": "🦉",
        "accuracy": 0.90,
        "style": "Methodical academic professor who reads carefully and speaks formally.",
        "min_delay": 7.0,
        "max_delay": 12.0,
    },
    "chaotic": {
        "name": "GlitchGremlin (Bot)",
        "student_id": "AI-BOT-03",
        "avatar": "👾",
        "accuracy": 0.40,
        "style": "Chaotic, witty guesser who makes funny remarks.",
        "min_delay": 4.0,
        "max_delay": 9.0,
    },
}


class _CompactMCQ(BaseModel):
    question: str
    options: List[str]
    correct_index: int
    explanation: str


class _CompactQuizResponse(BaseModel):
    questions: List[_CompactMCQ]


def _parse_correct_index(val: Any) -> int:
    if isinstance(val, int):
        return max(0, min(3, val))
    s = str(val or "0").strip().upper()
    letter_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    for ch in s:
        if ch in letter_map:
            return letter_map[ch]
        if ch in ("0", "1", "2", "3"):
            return int(ch)
    return 0


# 1. Fast Pre-Game MCQ Generation (Guaranteed Exact Count)
async def generate_quiz_from_source(
    provider: ProviderType,
    title: str,
    source_text: str,
    question_count: int,
    difficulty: DifficultyType,
) -> Tuple[QuizGenerationResponse, str]:
    collected: List[GeneratedQuestion] = []
    seen_questions: set[str] = set()
    model_label = get_model_label(provider)

    for _pass in range(question_count + 2):
        needed = question_count - len(collected)
        if needed <= 0:
            break

        existing_prompts = [q.question for q in collected]
        avoid_note = f" Do NOT repeat these questions: {existing_prompts}." if existing_prompts else ""

        system_prompt = (
            f"Generate a JSON object with a 'questions' list containing {needed} distinct multiple-choice questions "
            "from the source text. Each item in 'questions' must have:\n"
            "- 'question': 1 short sentence\n"
            "- 'options': 4 short distinct choices (3-8 words each)\n"
            "- 'correct_index': integer 0, 1, 2, or 3\n"
            "- 'explanation': 1 brief sentence explaining the right answer."
        )
        user_prompt = (
            f"Topic: {title} | Difficulty: {difficulty} | Generate {needed} items.{avoid_note}\n"
            f"Source:\n{source_text[:2800]}"
        )

        compact_res, model_label = await generate_structured(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=_CompactQuizResponse,
            temperature=0.35,
            max_retries=1,
        )

        for cq in (compact_res.questions or []):
            if len(collected) >= question_count:
                break
            q_clean = str(cq.question or "").strip()
            if not q_clean or q_clean.lower() in seen_questions:
                continue
            seen_questions.add(q_clean.lower())

            opts = [
                re.sub(r"^[A-Da-d][\)\.\:\-]\s*", "", str(o).strip())
                for o in (cq.options or [])
                if str(o).strip()
            ][:4]
            while len(opts) < 4:
                opts.append(f"Option {OPTION_LETTERS[len(opts)]}")

            c_idx = _parse_correct_index(cq.correct_index)
            exp = str(cq.explanation or "").strip() or f"'{opts[c_idx]}' is directly supported by the source text."

            option_explanations = [
                f"Correct! {exp}" if i == c_idx else f"Incorrect — '{opts[i]}' is not right. {exp}"
                for i in range(4)
            ]

            q_num = len(collected) + 1
            collected.append(
                GeneratedQuestion(
                    question_id=q_num,
                    question=q_clean,
                    options=opts,
                    correct_option_index=c_idx,
                    reference_answer=f"{OPTION_LETTERS[c_idx]}) {opts[c_idx]} — {exp}",
                    option_explanations=option_explanations,
                    max_marks=2,
                    time_limit=20,
                )
            )

    if not collected:
        raise ValueError("The AI model returned 0 valid questions. Try again or switch provider.")

    return QuizGenerationResponse(questions=collected), model_label


# 2. Redemption Open-Ended Verification (Used in Post-Game Study Deck)
async def evaluate_player_answer(
    provider: ProviderType,
    source_text: str,
    question: Dict[str, Any],
    submitted_answer: str,
) -> Tuple[AnswerEvaluationResponse, str]:
    cleaned = (submitted_answer or "").strip()[:500]
    model_label = get_model_label(provider)
    if not cleaned:
        return (
            AnswerEvaluationResponse(
                is_correct=False,
                awarded_marks=0,
                micro_tutoring=f"No answer entered. Reference: {question['reference_answer']}",
                evidence_from_source="Blank submission.",
            ),
            model_label,
        )

    system_prompt = (
        "Grade the student's practice answer (0, 1, or 2 marks) against the reference answer. "
        "Give 1 concise sentence of micro_tutoring."
    )
    user_prompt = (
        f"Question: {question['question']}\n"
        f"Reference: {question['reference_answer']}\n"
        f"Student Answer: \"\"\"{cleaned}\"\"\""
    )
    res, model_label = await generate_structured(
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_model=AnswerEvaluationResponse,
        temperature=0.1,
        max_retries=0,
    )
    res.awarded_marks = max(0, min(2, int(res.awarded_marks)))
    res.is_correct = res.awarded_marks == 2
    return res, model_label


# 3A. AI Persona Bot MCQ Selection
async def simulate_bot_answer(
    provider: ProviderType,
    persona: BotPersonaType,
    question: Dict[str, Any],
) -> BotAnswerResponse:
    profile = BOT_PROFILES[persona]
    correct_idx = int(question["correct_option_index"])
    delay = round(random.uniform(profile["min_delay"], profile["max_delay"]), 1)

    if random.random() < profile["accuracy"]:
        chosen = correct_idx
    else:
        wrong_choices = [i for i in range(4) if i != correct_idx]
        chosen = random.choice(wrong_choices)

    return BotAnswerResponse(selected_option_index=chosen, thinking_delay_sec=delay)


# 3B. Live Leaderboard Bot Banter
async def generate_bot_banter(
    provider: ProviderType,
    persona: BotPersonaType,
    leaderboard_summary: str,
) -> str:
    profile = BOT_PROFILES[persona]
    system_prompt = (
        f"You are {profile['name']} in a live ZapIt quiz. Persona: {profile['style']} "
        "Write ONE witty 1-sentence reaction to the leaderboard."
    )
    try:
        res, _ = await generate_structured(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=f"Standings: {leaderboard_summary}",
            response_model=BotBanterResponse,
            temperature=0.8,
            max_retries=0,
        )
        return f"{profile['avatar']} {profile['name']}: \"{res.banter_line}\""
    except Exception:
        return f"{profile['avatar']} {profile['name']}: \"Watch out on the next question!\""


# 4. Post-Game Personalized Remediation Deck
async def generate_player_remediation(
    provider: ProviderType,
    player_name: str,
    source_text: str,
    missed_items: List[Dict[str, Any]],
) -> Tuple[RemediationDeckResponse, str]:
    if not missed_items:
        focus_prompt = (
            f"{player_name} scored 100%! Create 5 short advanced flashcards "
            "and 2 challenge redemption questions from the source."
        )
    else:
        formatted_misses = "\n".join(
            [
                f"- Q: {m['question']} | Chose: '{m['submitted_answer']}' | Correct: '{m['reference_answer']}'"
                for m in missed_items
            ]
        )
        focus_prompt = (
            f"{player_name} missed:\n{formatted_misses}\n"
            "Create 5 concise flashcards and 2 redemption practice questions fixing these gaps."
        )

    system_prompt = (
        "Build a concise study deck from the source text. Keep all strings short (1 sentence max). "
        "Return 'weak_concepts_summary', 5 'flashcards', and 2 'redemption_questions'."
    )
    user_prompt = f"Source:\n{source_text[:2500]}\n\n{focus_prompt}"

    return await generate_structured(
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_model=RemediationDeckResponse,
        temperature=0.3,
        max_retries=0,
    )