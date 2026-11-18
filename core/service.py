from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from .config import PluginConfig
from .models import (
    AnswerResult,
    AnswerStatus,
    LeaderboardEntry,
    Participant,
    Question,
    QuestionType,
    QuestionView,
    QuizEvent,
    QuizEventType,
    QuizSession,
    QuizSnapshot,
    RevealResult,
    Subject,
)
from .storage import QuestionBank, StatsStore

EventSink = Callable[[QuizEvent], Awaitable[None]]


class QuizService:
    """Coordinate quiz sessions without depending on AstrBot message types."""

    def __init__(
        self,
        question_source: QuestionBank,
        score_store: StatsStore,
        config: PluginConfig,
        event_sink: EventSink,
    ):
        self.question_source = question_source
        self.score_store = score_store
        self.cfg = config
        self.event_sink = event_sink
        self.random = random.Random()
        self.sessions: dict[str, QuizSession] = {}
        self.timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()

    async def start(self, scope_id: str, subject: Subject) -> QuestionView:
        """Start or replace a quiz session and draw its first question.

        Args:
            scope_id: Independent group or future web-room identifier.
            subject: Selected question-bank subject.

        Returns:
            First question view for the new session.
        """
        async with self.lock:
            self._cancel_timeout(scope_id)
            session = QuizSession(scope_id=scope_id, selected_subject=subject)
            self.sessions[scope_id] = session
            return self._draw_question(session)

    async def next_question(self, scope_id: str) -> QuestionView | None:
        """Draw the next question for an existing session.

        Args:
            scope_id: Independent quiz scope.

        Returns:
            New question view, or None when no session exists.
        """
        async with self.lock:
            session = self.sessions.get(scope_id)
            if session is None:
                return None
            self._cancel_timeout(scope_id)
            return self._draw_question(session)

    async def submit_answer(
        self,
        scope_id: str,
        question_number: int,
        participant: Participant,
        raw_answer: str,
    ) -> AnswerResult:
        """Validate and score one participant answer.

        Args:
            scope_id: Independent quiz scope.
            question_number: Scope-local question number observed by the adapter.
            participant: User submitting the answer.
            raw_answer: Adapter-provided answer text.

        Returns:
            Structured answer outcome for any presentation adapter.
        """
        async with self.lock:
            session = self.sessions.get(scope_id)
            question = session.current_question if session else None
            if session is None or question is None:
                return AnswerResult(
                    status=AnswerStatus.NO_ACTIVE_QUESTION,
                    participant=participant,
                    normalized_answer=None,
                    question=None,
                )
            if session.question_number != question_number:
                return AnswerResult(
                    status=AnswerStatus.STALE_QUESTION,
                    participant=participant,
                    normalized_answer=None,
                    question=None,
                )

            normalized = self._normalize_answer(raw_answer, question)
            if normalized is None:
                return AnswerResult(
                    status=AnswerStatus.INVALID,
                    participant=participant,
                    normalized_answer=None,
                    question=question,
                    number=session.question_number,
                    selected_subject=session.selected_subject,
                )
            if participant.user_id in session.answered_user_ids:
                return AnswerResult(
                    status=AnswerStatus.ALREADY_ANSWERED,
                    participant=participant,
                    normalized_answer=normalized,
                    question=question,
                    number=session.question_number,
                    selected_subject=session.selected_subject,
                )

            session.answered_user_ids.add(participant.user_id)
            question_number = session.question_number
            selected_subject = session.selected_subject
            is_correct = tuple(normalized) == question.answer
            score = await self.score_store.record(
                scope_id, participant, correct=is_correct
            )
            next_question = None
            if is_correct:
                session.current_question = None
                self._cancel_timeout(scope_id)
                next_question = self._draw_question(session)

            return AnswerResult(
                status=(AnswerStatus.CORRECT if is_correct else AnswerStatus.WRONG),
                participant=participant,
                normalized_answer=normalized,
                question=question,
                number=question_number,
                selected_subject=selected_subject,
                score=score,
                next_question=next_question,
            )

    async def reveal(self, scope_id: str) -> RevealResult | None:
        """Reveal and close the active question without changing scores.

        Args:
            scope_id: Independent quiz scope.

        Returns:
            Revealed question, or None when no question is active.
        """
        async with self.lock:
            session = self.sessions.get(scope_id)
            if session is None or session.current_question is None:
                return None
            question = session.current_question
            question_number = session.question_number
            selected_subject = session.selected_subject
            session.current_question = None
            self._cancel_timeout(scope_id)
            next_question = self._draw_question(session)
            return RevealResult(
                question=question,
                number=question_number,
                selected_subject=selected_subject,
                next_question=next_question,
            )

    async def stop(self, scope_id: str) -> bool:
        """Stop a scope and discard its active in-memory session.

        Args:
            scope_id: Independent quiz scope.

        Returns:
            True when an existing session was stopped.
        """
        async with self.lock:
            session = self.sessions.pop(scope_id, None)
            self._cancel_timeout(scope_id)
            return session is not None

    async def snapshot(self, scope_id: str) -> QuizSnapshot | None:
        """Return the current scope state for an adapter.

        Args:
            scope_id: Independent quiz scope.

        Returns:
            Immutable state projection, or None when no session exists.
        """
        async with self.lock:
            session = self.sessions.get(scope_id)
            if session is None:
                return None
            return QuizSnapshot(
                scope_id=scope_id,
                selected_subject=session.selected_subject,
                question_number=session.question_number,
                current_question=session.current_question,
            )

    async def leaderboard(
        self, scope_id: str, limit: int = 10
    ) -> list[LeaderboardEntry]:
        """Return the persistent leaderboard for a scope.

        Args:
            scope_id: Independent quiz scope.
            limit: Maximum number of entries.

        Returns:
            Ranked score entries.
        """
        async with self.lock:
            return self.score_store.leaderboard(scope_id, limit)

    async def close(self) -> None:
        """Cancel service-owned tasks and discard active sessions."""
        async with self.lock:
            tasks = list(self.timeout_tasks.values())
            self.timeout_tasks.clear()
            self.sessions.clear()
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _draw_question(self, session: QuizSession) -> QuestionView:
        """Select a non-recent question and arm its timeout.

        Args:
            session: Mutable scope state protected by the service lock.

        Returns:
            Adapter-facing question view.

        Raises:
            RuntimeError: If the selected question bank is empty.
        """
        questions = self.question_source.questions_for(session.selected_subject)
        if not questions:
            raise RuntimeError(
                f"Question bank {session.selected_subject.value} is empty"
            )

        recent_ids = set(session.recent_question_ids)
        candidates = [
            question for question in questions if question.question_id not in recent_ids
        ]
        question = self.random.choice(candidates or list(questions))
        session.current_question = question
        session.answered_user_ids.clear()
        session.question_number += 1

        if self.cfg.recent_question_count:
            session.recent_question_ids.append(question.question_id)
            del session.recent_question_ids[: -self.cfg.recent_question_count]
        else:
            session.recent_question_ids.clear()

        self._schedule_timeout(session)
        return QuestionView(
            question=question,
            number=session.question_number,
            selected_subject=session.selected_subject,
        )

    def _schedule_timeout(self, session: QuizSession) -> None:
        """Create a timeout task for the current question when configured.

        Args:
            session: Mutable session containing the newly selected question.
        """
        question = session.current_question
        if question is None:
            return
        self.timeout_tasks[session.scope_id] = asyncio.create_task(
            self._expire_after(
                session.scope_id,
                question.question_id,
                session.question_number,
            )
        )

    async def _expire_after(
        self, scope_id: str, question_id: str, question_number: int
    ) -> None:
        """Expire a question only if its identity still matches.

        Args:
            scope_id: Independent quiz scope.
            question_id: Question identity captured when the task was created.
            question_number: Progress number captured for the reveal event.
        """
        try:
            await asyncio.sleep(self.cfg.question_timeout_seconds)
            async with self.lock:
                session = self.sessions.get(scope_id)
                question = session.current_question if session else None
                if session is None or question is None:
                    return
                if question.question_id != question_id:
                    return
                session.current_question = None
                event = QuizEvent(
                    event_type=QuizEventType.QUESTION_EXPIRED,
                    scope_id=scope_id,
                    reveal=RevealResult(
                        question=question,
                        number=question_number,
                        selected_subject=session.selected_subject,
                    ),
                )
            if self.event_sink is not None:
                await self.event_sink(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Jiakao question timeout event failed")
        finally:
            current_task = asyncio.current_task()
            if self.timeout_tasks.get(scope_id) is current_task:
                self.timeout_tasks.pop(scope_id, None)

    def _cancel_timeout(self, scope_id: str) -> None:
        """Cancel and forget the pending timeout for a scope.

        Args:
            scope_id: Independent quiz scope.
        """
        task = self.timeout_tasks.pop(scope_id, None)
        if task is not None:
            task.cancel()

    @staticmethod
    def _normalize_answer(raw_answer: str, question: Question) -> str | None:
        """Normalize user text while enforcing the question answer shape.

        Args:
            raw_answer: User-provided answer text.
            question: Active normalized question.

        Returns:
            Sorted answer letters, or None when the input is invalid.
        """
        text = raw_answer.strip().upper()
        aliases = {
            "正确": "A",
            "对": "A",
            "√": "A",
            "错误": "B",
            "错": "B",
            "×": "B",
        }
        text = aliases.get(text, text)
        compact = re.sub(r"[\s,，、/+]+", "", text)
        if not re.fullmatch(r"[A-D]+", compact):
            return None
        if len(compact) != len(set(compact)):
            return None
        if any(ord(letter) - ord("A") >= len(question.options) for letter in compact):
            return None
        if question.question_type is QuestionType.MULTIPLE:
            if len(compact) < 2:
                return None
        elif len(compact) != 1:
            return None
        return "".join(sorted(compact))
