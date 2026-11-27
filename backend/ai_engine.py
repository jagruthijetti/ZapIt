import random
from typing import Any, Dict, List, Tuple

from backend.llm_router import call_structured_llm
from backend.schemas import (
    AnswerEvaluationResponse,
    BotAnswerResponse,
    BotBanterResponse,
    FlashcardItem,
    QuizGenerationResponse,
    RemediationDeckResponse,
)


async def generate_quiz_from_source(
    provider: str,
    title: str,
    source_text: str,
    question_count: int = 5,
    difficulty: str = "Medium",
    default_time_limit: int = 20,
) -> Tuple[QuizGenerationResponse, str]:
    """
    Generates a structured 4-option MCQ assessment strictly grounded in `source_text`.
    Returns (QuizGenerationResponse, model_label).
    """
    system_prompt = (
        "You are an expert educator and assessment designer. "
        "Generate clear, unambiguous multiple-choice questions strictly based on the provided class notes. "
        "Each question must have exactly 4 distinct options, one unambiguously correct option (index 0 to 3), "
        "a concise reference answer, and a helpful 1-sentence explanation for each of the 4 options."
    )

    user_prompt = f"""
Quiz Title: {title}
Difficulty Level: {difficulty}
Number of Questions: {question_count}
Default Time Limit per Question: {default_time_limit} seconds

Class Notes / Source Material:
\"\"\"
{source_text[:28000]}
\"\"\"

Generate exactly {question_count} multiple-choice questions strictly grounded in the text above.
Set `question_id` sequentially from 1 to {question_count}, `max_marks` to 2, and `time_limit` to {default_time_limit}.
Ensure `correct_option_index` (0, 1, 2, or 3) accurately matches the right option in `options`.
"""

    result, model_label = await call_structured_llm(
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=QuizGenerationResponse,
    )

    letters = ["A", "B", "C", "D"]
    for idx, q in enumerate(result.questions, start=1):
        q.question_id = idx
        q.time_limit = default_time_limit
        c_idx = max(0, min(3, int(q.correct_option_index)))
        q.correct_option_index = c_idx
        right_text = q.options[c_idx] if c_idx < len(q.options) else ""
        if not q.reference_answer:
            q.reference_answer = f"{letters[c_idx]}) {right_text}"
        if len(q.option_explanations) != 4:
            q.option_explanations = [
                f"Correct! '{opt}' is right." if i == c_idx else f"Incorrect — '{opt}' is not right."
                for i, opt in enumerate(q.options)
            ]

    return result, model_label


async def evaluate_player_answer(
    provider: str,
    source_text: str,
    question: Dict[str, Any],
    submitted_answer: str,
) -> Tuple[AnswerEvaluationResponse, str]:
    """
    Evaluates a participant's answer and generates concise micro-tutoring feedback.
    """
    system_prompt = (
        "You are a supportive AI tutor grading a student's quiz response. "
        "Compare the student's answer against the reference answer and class notes, "
        "determine correctness, award marks (0 or 2), and provide 1-2 sentences of clear micro-tutoring."
    )

    user_prompt = f"""
Question: {question.get("question", "")}
Reference Answer: {question.get("reference_answer", "")}
Student's Answer: {submitted_answer}

Context from Class Notes:
\"\"\"
{(source_text or "")[:6000]}
\"\"\"
"""

    return await call_structured_llm(
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=AnswerEvaluationResponse,
    )


async def generate_remediation_deck(
    provider: str,
    source_text: str,
    missed_questions: List[Dict[str, Any]],
) -> Tuple[RemediationDeckResponse, str]:
    """
    Generates personalized study flashcards targeting concepts the participant missed during the match.
    """
    if not missed_questions:
        empty_deck = RemediationDeckResponse(
            summary="Great job! You answered every question correctly.",
            flashcards=[],
        )
        return empty_deck, "None"

    missed_summary = "\n".join(
        f"- Question: {m.get('question', '')}\n  Your Answer: {m.get('your_answer', '')}\n  Correct Answer: {m.get('correct_answer', '')}"
        for m in missed_questions[:8]
    )

    system_prompt = (
        "You are an expert study coach. Create concise, high-impact revision flashcards "
        "for the concepts a student missed in a live quiz, grounded in the provided class notes."
    )

    user_prompt = f"""
Missed Questions:
{missed_summary}

Class Notes Reference:
\"\"\"
{(source_text or "")[:10000]}
\"\"\"

Generate a brief 1-sentence encouragement/study summary and 2 to 4 concise flashcards (`front`, `back`, `tag`) to help the student master these concepts.
"""

    try:
        return await call_structured_llm(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=RemediationDeckResponse,
        )
    except Exception:
        # Deterministic fallback if LLM call times out at match end
        fallback_cards = [
            FlashcardItem(
                front=m.get("question", "Review Concept"),
                back=f"Correct Answer: {m.get('correct_answer', '')}. {m.get('explanation', '')}".strip(),
                tag="Missed Question Review",
            )
            for m in missed_questions[:4]
        ]
        fallback_deck = RemediationDeckResponse(
            summary="Review these key concepts from the questions you missed:",
            flashcards=fallback_cards,
        )
        return fallback_deck, "Fallback Review"


async def generate_bot_choice(
    provider: str,
    persona: str,
    question: Dict[str, Any],
    time_limit: int = 20,
) -> BotAnswerResponse:
    """
    Simulates an AI persona competitor selecting one of the 4 MCQ options
    with realistic accuracy and response timing.
    """
    correct_idx = int(question.get("correct_option_index", 0))
    options: List[str] = question.get("options", [])
    wrong_indices = [i for i in range(len(options)) if i != correct_idx] or [0]

    if persona == "professor":
        accuracy = 0.90
        min_delay, max_delay = 3.5, min(9.0, max(4.5, time_limit * 0.45))
    elif persona == "speedrunner":
        accuracy = 0.65
        min_delay, max_delay = 1.2, min(4.0, max(2.0, time_limit * 0.25))
    else:
        accuracy = 0.45
        min_delay, max_delay = 1.5, min(11.0, max(3.0, time_limit * 0.6))

    delay = round(random.uniform(min_delay, max_delay), 2)
    chosen_idx = correct_idx if random.random() < accuracy else random.choice(wrong_indices)

    return BotAnswerResponse(
        selected_option_index=max(0, min(3, chosen_idx)),
        thinking_delay_sec=delay,
    )


async def generate_bot_banter(
    provider: str,
    question: str,
    leader_name: str,
) -> str:
    """
    Generates a short, playful 1-line live commentary reaction from AI competitors between rounds.
    """
    system_prompt = (
        "You are a witty live quiz show commentator representing AI bot competitors. "
        "Write one short, energetic, classroom-friendly sentence (under 18 words) reacting to the round."
    )
    user_prompt = f"Question topic: {question}\nCurrent leaderboard leader: {leader_name}"

    try:
        res, _ = await call_structured_llm(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=BotBanterResponse,
        )
        return res.banter.strip()
    except Exception:
        return f"🤖 Bot Squad: {leader_name} is setting a blazing pace—can anyone catch up next round?"