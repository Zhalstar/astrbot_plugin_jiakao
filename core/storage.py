from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

from .models import (
    LeaderboardEntry,
    Participant,
    PlayerScore,
    Question,
    QuestionType,
    Subject,
)

SOURCE_REVISION = "9c3545279ffd31650ed7ad2fc5fe17ca914346ec"
SOURCE_ROOT = (
    "https://raw.githubusercontent.com/nujgnoix/chun-jing-jia-kao/"
    f"{SOURCE_REVISION}/public"
)
BANK_URL = f"{SOURCE_ROOT}/bank.json"
IMAGE_BASE_URL = f"{SOURCE_ROOT}/"


class QuestionBank:
    """Load, validate, and cache the pinned external question bank."""

    def __init__(self, cache_path: Path, image_dir: Path):
        """Create a question-bank repository.

        Args:
            cache_path: Plugin-owned path used for the validated JSON cache.
            image_dir: Plugin-owned directory used for persistent image files.
        """
        self.cache_path = cache_path
        self.image_dir = image_dir
        self._questions: dict[Subject, tuple[Question, ...]] = {}
        self._image_locks: dict[str, asyncio.Lock] = {}
        self._image_session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        """Load the cache or download it when no valid cache exists.

        Raises:
            RuntimeError: If neither the local cache nor remote bank is usable.
        """
        if self.cache_path.exists():
            try:
                payload = await asyncio.to_thread(
                    self.cache_path.read_text, encoding="utf-8"
                )
                self._questions = self._parse_payload(json.loads(payload))
                return
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning(f"Jiakao question cache is invalid: {exc}")

        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(BANK_URL) as response:
                    response.raise_for_status()
                    payload = await response.text(encoding="utf-8")
            questions = self._parse_payload(json.loads(payload))
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Unable to load the Jiakao question bank: {exc}"
            ) from exc

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.cache_path.with_suffix(".json.tmp")
        await asyncio.to_thread(temp_path.write_text, payload, encoding="utf-8")
        await asyncio.to_thread(temp_path.replace, self.cache_path)
        self._questions = questions
        logger.info(
            "Jiakao question bank cached: "
            f"subject 1={len(questions[Subject.ONE])}, "
            f"subject 4={len(questions[Subject.FOUR])}"
        )

    async def cache_image(self, question: Question) -> Path | None:
        """Return a persistent local copy of a question image when available.

        Args:
            question: Question whose optional image should be cached.

        Returns:
            Cached image path, or None when the question has no image or the
            remote image could not be downloaded.
        """
        if not question.image_url:
            return None
        if not question.image_url.startswith(IMAGE_BASE_URL):
            logger.warning(
                f"Jiakao question {question.question_id} has an unexpected image URL"
            )
            return None

        image_path = question.image_url.removeprefix(IMAGE_BASE_URL)
        relative_path = Path(image_path)
        if relative_path.parts and relative_path.parts[0] == "images":
            relative_path = Path(*relative_path.parts[1:])
        if (
            not relative_path.parts
            or relative_path.is_absolute()
            or ".." in relative_path.parts
        ):
            logger.warning(
                f"Jiakao question {question.question_id} has an unsafe image path"
            )
            return None

        cache_path = self.image_dir / relative_path
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                return cache_path
        except OSError:
            pass

        lock = self._image_locks.setdefault(question.image_url, asyncio.Lock())
        async with lock:
            try:
                if cache_path.is_file() and cache_path.stat().st_size > 0:
                    return cache_path

                if self._image_session is None or self._image_session.closed:
                    timeout = aiohttp.ClientTimeout(total=60)
                    self._image_session = aiohttp.ClientSession(timeout=timeout)
                async with self._image_session.get(question.image_url) as response:
                    response.raise_for_status()
                    payload = await response.read()
                if not payload:
                    raise ValueError("downloaded image is empty")

                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
                await asyncio.to_thread(temp_path.write_bytes, payload)
                await asyncio.to_thread(temp_path.replace, cache_path)
                return cache_path
            except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
                temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
                await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                logger.warning(
                    f"Unable to cache Jiakao image for question "
                    f"{question.question_id}: {exc}"
                )
                return None

    async def close(self) -> None:
        """Close the reusable image download session."""
        if self._image_session is not None:
            await self._image_session.close()
            self._image_session = None

    def questions_for(self, subject: Subject) -> tuple[Question, ...]:
        """Return questions for one concrete subject.

        Args:
            subject: Subject one or subject four.

        Returns:
            Immutable sequence of normalized questions.
        """
        return self._questions.get(subject, ())

    @staticmethod
    def _parse_payload(payload: Any) -> dict[Subject, tuple[Question, ...]]:
        """Validate source JSON and convert it to domain models.

        Args:
            payload: Decoded JSON value from the source bank.

        Returns:
            Questions indexed by concrete subject.

        Raises:
            ValueError: If the payload or any question has an invalid structure.
        """
        if not isinstance(payload, dict):
            raise ValueError("Question bank root must be an object")

        result: dict[Subject, tuple[Question, ...]] = {}
        for subject in (Subject.ONE, Subject.FOUR):
            rows = payload.get(subject.value)
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"Question bank subject {subject.value} is empty")

            questions: list[Question] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Question entries must be objects")
                try:
                    question_id = str(row["id"]).strip()
                    question_type = QuestionType(str(row["type"]))
                    text = str(row["question"]).strip()
                    raw_options = row["options"]
                    raw_answer = str(row["answer"]).upper()
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValueError(f"Invalid question entry: {exc}") from exc

                if not question_id or not text:
                    raise ValueError("Question ID and text cannot be empty")
                if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 4:
                    raise ValueError(f"Question {question_id} has invalid options")
                options = tuple(str(option).strip() for option in raw_options)
                if any(not option for option in options):
                    raise ValueError(f"Question {question_id} has an empty option")

                compact_answer = re.sub(r"[\s,，、/]+", "", raw_answer)
                if not re.fullmatch(r"[A-D]+", compact_answer):
                    raise ValueError(f"Question {question_id} has an invalid answer")
                answer = tuple(compact_answer)
                if len(answer) != len(set(answer)) or any(
                    ord(letter) - ord("A") >= len(options) for letter in answer
                ):
                    raise ValueError(f"Question {question_id} has an invalid answer")

                image_path = str(row.get("image", "")).strip().lstrip("/")
                questions.append(
                    Question(
                        question_id=question_id,
                        subject=subject,
                        question_type=question_type,
                        text=text,
                        options=options,
                        answer=tuple(sorted(answer)),
                        explanation=str(row.get("explain", "")).strip(),
                        image_url=(
                            f"{IMAGE_BASE_URL}{image_path}" if image_path else None
                        ),
                        chapter=str(row.get("chapter", "")).strip(),
                    )
                )
            result[subject] = tuple(questions)
        return result


class StatsStore:
    """Persist per-scope participant scores as a small JSON document."""

    def __init__(self, path: Path):
        """Create a score repository.

        Args:
            path: Plugin-owned JSON file path.
        """
        self.path = path
        self._scores: dict[str, dict[str, PlayerScore]] = {}

    async def initialize(self) -> None:
        """Load existing scores, falling back to an empty store on corruption."""
        if not self.path.exists():
            return
        try:
            payload = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            raw_scopes = json.loads(payload)
            if not isinstance(raw_scopes, dict):
                raise ValueError("Score root must be an object")

            loaded: dict[str, dict[str, PlayerScore]] = {}
            for umo, raw_players in raw_scopes.items():
                if not isinstance(raw_players, dict):
                    continue
                players: dict[str, PlayerScore] = {}
                for user_id, raw_score in raw_players.items():
                    if not isinstance(raw_score, dict):
                        continue
                    try:
                        players[str(user_id)] = PlayerScore(
                            user_id=str(user_id),
                            name=str(raw_score.get("name", user_id)),
                            correct=max(0, int(raw_score.get("correct", 0))),
                            wrong=max(0, int(raw_score.get("wrong", 0))),
                            current_streak=max(
                                0, int(raw_score.get("current_streak", 0))
                            ),
                            best_streak=max(0, int(raw_score.get("best_streak", 0))),
                        )
                    except (TypeError, ValueError):
                        continue
                loaded[str(umo)] = players
            self._scores = loaded
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(f"Jiakao score data is invalid and will be ignored: {exc}")

    async def record(
        self,
        umo: str,
        participant: Participant,
        *,
        correct: bool,
    ) -> LeaderboardEntry:
        """Record one answer and persist the updated scores.

        Args:
            umo: Unified message origin identifying one quiz session.
            participant: User who submitted the answer.
            correct: Whether the submitted answer was correct.

        Returns:
            Updated immutable score projection.
        """
        players = self._scores.setdefault(umo, {})
        score = players.setdefault(
            participant.user_id,
            PlayerScore(user_id=participant.user_id, name=participant.name),
        )
        score.name = participant.name
        if correct:
            score.correct += 1
            score.current_streak += 1
            score.best_streak = max(score.best_streak, score.current_streak)
        else:
            score.wrong += 1
            score.current_streak = 0
        await self._save()
        return self._entry(score)

    def leaderboard(self, umo: str, limit: int = 10) -> list[LeaderboardEntry]:
        """Return the highest-ranked participants for a scope.

        Args:
            umo: Unified message origin identifying one quiz session.
            limit: Maximum number of entries to return.

        Returns:
            Entries ordered by correct answers, accuracy, and best streak.
        """
        scores = list(self._scores.get(umo, {}).values())
        scores.sort(
            key=lambda score: (
                score.correct,
                score.correct / score.total if score.total else 0,
                score.best_streak,
            ),
            reverse=True,
        )
        return [self._entry(score) for score in scores[: max(0, limit)]]

    async def _save(self) -> None:
        """Atomically persist the current in-memory score snapshot."""
        payload = {
            umo: {user_id: asdict(score) for user_id, score in players.items()}
            for umo, players in self._scores.items()
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        await asyncio.to_thread(temp_path.write_text, serialized, encoding="utf-8")
        await asyncio.to_thread(temp_path.replace, self.path)

    @staticmethod
    def _entry(score: PlayerScore) -> LeaderboardEntry:
        """Create an immutable projection from a mutable score.

        Args:
            score: Internal mutable score record.

        Returns:
            Adapter-safe score projection.
        """
        return LeaderboardEntry(
            user_id=score.user_id,
            name=score.name,
            correct=score.correct,
            wrong=score.wrong,
            current_streak=score.current_streak,
            best_streak=score.best_streak,
        )
