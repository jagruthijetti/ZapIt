import random
from typing import Any, Dict, List, Optional, Tuple

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
    all_questions: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[RemediationDeckResponse, str]:
    """
    Generates:
    1. Topic flashcards covering the main concepts tested in the quiz.
    2. Mistake-specific remediation flashcards for any missed questions.
    3. A short interactive remediation mini-quiz targeting missed concepts.
    """
    all_questions = all_questions or []
    topics_summary = "\n".join(
        f"- {q.get('question', '')} (Answer: {q.get('reference_answer', '')})"
        for q in all_questions[:10]
    )

    missed_summary = (
        "\n".join(
            f"- Question: {m.get('question', '')}\n  Your Answer: {m.get('your_answer', '')}\n  Correct Answer: {m.get('correct_answer', '')}"
            for m in missed_questions[:8]
        )
        if missed_questions
        else "None — the student answered every question correctly!"
    )

    system_prompt = (
        "You are an expert study coach. Build a personalized post-quiz study kit grounded in the class notes: "
        "(1) `topic_flashcards`: 3-4 flashcards summarizing the core concepts from the quiz; "
        "(2) `flashcards`: 2-4 targeted remediation flashcards for the questions the student missed (empty list if none missed); "
        "(3) `remediation_quiz`: 2-3 new 4-option multiple-choice practice questions testing the exact concepts the student missed (empty list if none missed)."
    )

    user_prompt = f"""
Quiz Questions Covered:
{topics_summary}

Questions Missed by Student:
{missed_summary}

Class Notes Reference:
\"\"\"
{(source_text or "")[:10000]}
\"\"\"

Return a brief encouraging `summary`, `topic_flashcards`, `flashcards` (mistake remediation), and `remediation_quiz` (4 options each, `correct_option_index` 0..3, and `explanation`).
"""

    try:
        return await call_structured_llm(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=RemediationDeckResponse,
        )
    except Exception:
        topic_cards = [
            FlashcardItem(
                front=q.get("question", "Key Topic"),
                back=f"Key Takeaway: {q.get('reference_answer', '')}",
                tag="Quiz Topic",
            )
            for q in all_questions[:4]
        ]
        mistake_cards = [
            FlashcardItem(
                front=m.get("question", "Missed Concept"),
                back=f"Correct Answer: {m.get('correct_answer', '')}. {m.get('explanation', '')}".strip(),
                tag="Mistake Review",
            )
            for m in missed_questions[:4]
        ]
        fallback_deck = RemediationDeckResponse(
            summary=(
                "Mastery achieved! Review the key topic flashcards below."
                if not missed_questions
                else "Review these key concepts from the quiz and test yourself on the concepts you missed:"
            ),
            topic_flashcards=topic_cards,
            flashcards=mistake_cards,
            remediation_quiz=[],
        )
        return fallback_deck, "Fallback Study Kit"

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