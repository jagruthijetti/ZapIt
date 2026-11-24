from typing import List, Literal, Optional
from pydantic import BaseModel, Field

ProviderType = Literal["ollama", "gemini"]
DifficultyType = Literal["Easy", "Medium", "Hard"]
BotPersonaType = Literal["speedrunner", "professor", "chaotic"]


class GeneratedQuestion(BaseModel):
    question_id: int
    question: str
    options: List[str] = Field(..., description="Exactly 4 distinct multiple-choice options (A, B, C, D)")
    correct_option_index: int = Field(..., ge=0, le=3, description="Zero-based index (0 to 3) of the correct option")
    reference_answer: str = Field(..., description="Text of the correct option plus a concise 1-sentence explanation")
    option_explanations: List[str] = Field(
        ...,
        description="4 brief 1-sentence micro-tutoring explanations (one for each option 0..3)"
    )
    max_marks: int = Field(..., ge=1, le=10)
    time_limit: int = Field(..., ge=10, le=90)


class QuizGenerationResponse(BaseModel):
    questions: List[GeneratedQuestion]


class AnswerEvaluationResponse(BaseModel):
    is_correct: bool
    awarded_marks: int = Field(..., ge=0)
    micro_tutoring: str
    evidence_from_source: str


class BotAnswerResponse(BaseModel):
    selected_option_index: int = Field(..., ge=0, le=3)
    thinking_delay_sec: float = Field(..., ge=1.5, le=20.0)


class BotBanterResponse(BaseModel):
    banter_line: str


class FlashcardItem(BaseModel):
    concept_title: str
    front_prompt: str
    back_explanation: str
    memory_hook: str


class RedemptionQuestion(BaseModel):
    question: str
    reference_answer: str
    hint: str


class RemediationDeckResponse(BaseModel):
    weak_concepts_summary: str
    flashcards: List[FlashcardItem]
    redemption_questions: List[RedemptionQuestion]


class GenerateQuizRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=150)
    source_text: str = Field(..., min_length=20, max_length=30000)
    question_count: Literal[5, 10, 15] = 5
    difficulty: DifficultyType = "Medium"
    provider: ProviderType = "gemini"
    host_name: str = "Host"


class PublishQuizRequest(BaseModel):
    title: str
    source_text: str
    difficulty: DifficultyType
    provider: ProviderType
    host_name: str
    questions: List[GeneratedQuestion]
    bots: List[BotPersonaType] = Field(default_factory=list, max_length=3)
    reserved_pin: Optional[str] = None