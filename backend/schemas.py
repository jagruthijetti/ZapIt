from typing import List, Literal, Optional
from pydantic import BaseModel, Field

ProviderType = Literal["gemini", "ollama"]
DifficultyType = Literal["Easy", "Medium", "Hard"]
BotPersonaType = Literal["speedrunner", "professor", "chaotic"]


class HostRegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    email: str = Field(..., min_length=5, max_length=150)
    password: str = Field(..., min_length=4, max_length=128)


class HostLoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=150)
    password: str = Field(..., min_length=4, max_length=128)


class ReservePinRequest(BaseModel):
    provider: ProviderType = "gemini"
    question_count: int = Field(default=5, ge=1, le=30)


class GenerateQuizRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=150)
    source_text: str = Field(..., min_length=20, max_length=30000)
    question_count: Literal[5, 10, 15] = 5
    difficulty: DifficultyType = "Medium"
    provider: ProviderType = "gemini"
    host_name: str = "Host"
    default_time_limit: int = Field(default=20, ge=10, le=90)


class GeneratedQuestion(BaseModel):
    question_id: int
    question: str
    options: List[str] = Field(..., min_length=4, max_length=4)
    correct_option_index: int = Field(..., ge=0, le=3)
    reference_answer: str
    option_explanations: List[str] = Field(default_factory=list)
    max_marks: int = Field(default=2, ge=1, le=10)
    time_limit: int = Field(default=20, ge=10, le=90)


class QuizGenerationResponse(BaseModel):
    questions: List[GeneratedQuestion]


class PublishQuizRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=150)
    source_text: str = Field(..., min_length=20, max_length=30000)
    difficulty: DifficultyType = "Medium"
    provider: ProviderType = "gemini"
    host_name: str = "Instructor"
    questions: List[GeneratedQuestion]
    bots: List[BotPersonaType] = Field(default_factory=list)
    reserved_pin: Optional[str] = None


class RedemptionVerifyRequest(BaseModel):
    game_pin: str = Field(default="", max_length=12)
    question: str = Field(..., min_length=2)
    reference_answer: str = Field(..., min_length=1)
    student_answer: str = Field(..., min_length=1)


class AnswerEvaluationResponse(BaseModel):
    is_correct: bool
    awarded_marks: int = Field(default=0, ge=0, le=10)
    micro_tutoring: str


class FlashcardItem(BaseModel):
    front: str
    back: str
    tag: str = "Concept Review"


class RemediationDeckResponse(BaseModel):
    summary: str
    flashcards: List[FlashcardItem]


class BotAnswerResponse(BaseModel):
    selected_option_index: int = Field(..., ge=0, le=3)
    thinking_delay_sec: float = Field(default=3.0, ge=0.5, le=85.0)