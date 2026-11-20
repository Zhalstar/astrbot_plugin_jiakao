from dataclasses import dataclass, field
from enum import Enum


class Subject(str, Enum):
    """Supported question-bank selections."""

    ONE = "1"
    FOUR = "4"


class QuestionType(str, Enum):
    """Question interaction types from the source bank."""

    JUDGE = "judge"
    SINGLE = "single"
    MULTIPLE = "multiple"


class AnswerStatus(str, Enum):
    """Possible outcomes of an answer submission."""

    CORRECT = "correct"
    WRONG = "wrong"
    INVALID = "invalid"
    ALREADY_ANSWERED = "already_answered"
    NO_ACTIVE_QUESTION = "no_active_question"
    STALE_QUESTION = "stale_question"


class QuizEventType(str, Enum):
    """Asynchronous events emitted by the quiz service."""

    QUESTION_EXPIRED = "question_expired"


@dataclass(frozen=True, slots=True)
class Participant:
    """Identify a participant independently of a chat adapter."""

    user_id: str
    name: str


@dataclass(frozen=True, slots=True)
class Question:
    """Normalized question loaded from the external bank."""

    question_id: str
    subject: Subject
    question_type: QuestionType
    text: str
    options: tuple[str, ...]
    answer: tuple[str, ...]
    explanation: str
    image_url: str | None
    chapter: str

    @property
    def answer_text(self) -> str:
        """Return the canonical answer letters.

        Returns:
            Comma-separated answer letters.
        """
        return ",".join(self.answer)


@dataclass(frozen=True, slots=True)
class QuestionView:
    """Question and progress information exposed to adapters."""

    question: Question
    number: int
    selected_subject: Subject


@dataclass(slots=True)
class PlayerScore:
    """Mutable score record stored for one participant."""

    user_id: str
    name: str
    correct: int = 0
    wrong: int = 0
    current_streak: int = 0
    best_streak: int = 0

    @property
    def total(self) -> int:
        """Return the total submitted answers.

        Returns:
            Sum of correct and wrong submissions.
        """
        return self.correct + self.wrong


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """Immutable leaderboard projection returned to adapters."""

    user_id: str
    name: str
    correct: int
    wrong: int
    current_streak: int
    best_streak: int

    @property
    def total(self) -> int:
        """Return the total submitted answers.

        Returns:
            Sum of correct and wrong submissions.
        """
        return self.correct + self.wrong


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """Structured result of one answer submission."""

    status: AnswerStatus
    participant: Participant
    normalized_answer: str | None
    question: Question | None
    number: int | None = None
    selected_subject: Subject | None = None
    score: LeaderboardEntry | None = None
    next_question: QuestionView | None = None


@dataclass(frozen=True, slots=True)
class RevealResult:
    """Question data returned when an answer is revealed."""

    question: Question
    number: int
    selected_subject: Subject
    participant: Participant | None = None
    score: LeaderboardEntry | None = None
    next_question: QuestionView | None = None


@dataclass(frozen=True, slots=True)
class QuizSnapshot:
    """Current state projection for commands or future web APIs."""

    umo: str
    selected_subject: Subject
    question_number: int
    current_question: Question | None


@dataclass(frozen=True, slots=True)
class QuizEvent:
    """Asynchronous service event suitable for chat or web adapters."""

    event_type: QuizEventType
    umo: str
    reveal: RevealResult


@dataclass(slots=True)
class QuizSession:
    """Internal state for one independent unified message origin."""

    umo: str
    selected_subject: Subject
    question_number: int = 0
    current_question: Question | None = None
    next_question: Question | None = None
    answered_user_ids: set[str] = field(default_factory=set)
    recent_question_ids: list[str] = field(default_factory=list)
