from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import ActionRow, At, Button, Image, Plain
from astrbot.api.star import Context

from .models import (
    LeaderboardEntry,
    Participant,
    Question,
    QuestionType,
    QuestionView,
    RevealResult,
    Subject,
)
from .storage import QuestionBank


class QuizSender:
    """Render and deliver quiz messages without owning quiz state."""

    ANSWER_ACTION_PREFIX = "jiakao.answer."

    def __init__(
        self,
        context: Context,
        question_bank: QuestionBank,
    ) -> None:
        """Create a quiz message sender.

        Args:
            context: AstrBot context used for proactive timeout messages.
            question_bank: Repository used to resolve cached question images.
        """
        self.context = context
        self.question_bank = question_bank
        self._prefetch_tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        """Cancel outstanding image prefetch tasks."""
        tasks = list(self._prefetch_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def prefetch(self, question: Question | None) -> None:
        """Start caching the next question image in the background.

        Args:
            question: Preselected next question, if one exists.
        """
        if question is None or not question.image_url:
            return
        task = asyncio.create_task(self.question_bank.cache_image(question))
        self._prefetch_tasks.add(task)
        task.add_done_callback(self._prefetch_tasks.discard)

    async def send_text(self, event: AstrMessageEvent, text: str) -> None:
        """Send a plain text message through the originating event.

        Args:
            event: Incoming event that determines the reply target.
            text: Text to send.
        """
        await event.send(MessageChain([Plain(text)]))

    async def send_question(
        self,
        event: AstrMessageEvent,
        view: QuestionView,
        headline: str | None = None,
    ) -> None:
        """Render and send one question with platform-appropriate controls.

        Args:
            event: Incoming event that determines the reply target and platform.
            view: Question and session-local sequence number.
            headline: Optional heading shown before the question.
        """
        question = view.question
        subject_name = "科目一" if question.subject is Subject.ONE else "科目四"
        type_name = {
            QuestionType.JUDGE: "判断题",
            QuestionType.SINGLE: "单选题",
            QuestionType.MULTIPLE: "多选题",
        }[question.question_type]
        lines = []
        if headline:
            lines.append(f"【{headline}】")
        lines.extend(
            [
                f"【{subject_name} · 第 {view.number} 题 · {type_name}】",
                question.text,
                "",
            ]
        )
        use_answer_buttons = (
            event.get_platform_name() == "qq_official"
            and question.question_type is not QuestionType.MULTIPLE
        )
        if not (use_answer_buttons and question.question_type is QuestionType.JUDGE):
            lines.extend(
                f"{chr(ord('A') + index)}. {option}"
                for index, option in enumerate(question.options)
            )

        chain = MessageChain([Plain("\n".join(lines))])
        image_path = await self.question_bank.cache_image(question)
        if image_path is not None:
            chain.chain.append(Image.fromFileSystem(image_path))
        elif question.image_url:
            chain.chain.append(Image.fromURL(question.image_url))

        if use_answer_buttons:
            buttons = []
            for index in range(len(question.options)):
                answer = chr(ord("A") + index)
                label = answer
                if question.question_type is QuestionType.JUDGE:
                    label = f"{answer}. {question.options[index]}"[:10]
                buttons.append(
                    Button(
                        id=f"{self.ANSWER_ACTION_PREFIX}{answer.lower()}",
                        label=label,
                        style=Button.Style.PRIMARY,
                        action=Button.CallbackAction(
                            data={
                                "question_id": question.question_id,
                                "question_number": view.number,
                                "answer": answer,
                            }
                        ),
                    )
                )
            chain.chain.append(ActionRow(buttons=buttons))

        await event.send(chain)

    async def send_reveal(
        self,
        event: AstrMessageEvent,
        headline: str,
        reveal: RevealResult,
    ) -> None:
        """Send an answer reveal through the originating event.

        Args:
            event: Incoming event that determines the reply target.
            headline: Reveal heading.
            reveal: Answer and optional explanation to render.
        """
        participant = reveal.participant
        mention = None
        participant_name = None
        if participant is not None:
            if (
                event.get_platform_name() == "qq_official"
                and not event.is_private_chat()
            ):
                mention = At(qq=participant.user_id)
            elif (
                event.get_platform_name() == "qq_official"
                and participant.name == participant.user_id
            ):
                participant_name = "你"
            else:
                participant_name = participant.name
        await event.send(
            self._reveal_chain(
                headline,
                reveal,
                mention=mention,
                participant_name=participant_name,
            )
        )

    async def send_participant_text(
        self,
        event: AstrMessageEvent,
        participant: Participant,
        suffix: str,
    ) -> None:
        """Send participant feedback without exposing QQ Official OpenIDs.

        Args:
            event: Incoming event that determines the reply target.
            participant: Participant receiving the feedback.
            suffix: Text appended after the participant identity.
        """
        if event.get_platform_name() == "qq_official" and not event.is_private_chat():
            await event.send(MessageChain([At(qq=participant.user_id), Plain(suffix)]))
            return
        name = participant.name
        if event.get_platform_name() == "qq_official" and name == participant.user_id:
            name = "你"
        await self.send_text(event, f"{name}{suffix}")

    async def send_leaderboard(
        self,
        event: AstrMessageEvent,
        entries: list[LeaderboardEntry],
    ) -> None:
        """Render and send the current session leaderboard.

        Args:
            event: Incoming event that determines the reply target.
            entries: Ranked leaderboard entries.
        """
        chain = MessageChain([Plain("【驾考刷题排行榜】")])
        for index, entry in enumerate(entries, start=1):
            accuracy = entry.correct / entry.total * 100 if entry.total else 0
            chain.chain.append(Plain(f"\n{index}. "))
            if (
                event.get_platform_name() == "qq_official"
                and not event.is_private_chat()
            ):
                chain.chain.append(At(qq=entry.user_id))
            else:
                name = entry.name
                if event.get_platform_name() == "qq_official" and name == entry.user_id:
                    name = "你"
                chain.chain.append(Plain(name))
            chain.chain.append(
                Plain(
                    f"  答对 {entry.correct} / {entry.total}  "
                    f"正确率 {accuracy:.0f}%  最佳连对 {entry.best_streak}"
                )
            )
        await event.send(chain)

    async def send_timeout(self, umo: str, reveal: RevealResult) -> None:
        """Send an asynchronous timeout reveal to a unified message origin.

        Args:
            umo: Unified message origin identifying the target session.
            reveal: Timed-out question and answer data.
        """
        try:
            sent = await self.context.send_message(
                umo, self._reveal_chain("本题超时", reveal)
            )
            if not sent:
                logger.warning(
                    f"Jiakao timeout message has no matching platform: {umo}"
                )
        except Exception:
            logger.exception("Jiakao timeout message delivery failed")

    @staticmethod
    def _reveal_chain(
        headline: str,
        reveal: RevealResult,
        *,
        mention: At | None = None,
        participant_name: str | None = None,
    ) -> MessageChain:
        """Build a reveal chain shared by direct and timeout delivery.

        Args:
            headline: Reveal heading.
            reveal: Answer and optional explanation to render.
            mention: Optional platform mention rendered before the headline.
            participant_name: Optional plain-text participant identity.

        Returns:
            Rendered reveal message chain.
        """
        question = reveal.question
        answer_options = []
        for letter in question.answer:
            option_index = ord(letter) - ord("A")
            answer_options.append(f"{letter}. {question.options[option_index]}")
        lines = [f"答案：{'；'.join(answer_options)}"]
        if question.explanation:
            lines.append(f"解析：{question.explanation}")
        body = "\n".join(lines)
        if mention is not None:
            return MessageChain(
                [
                    Plain("【"),
                    mention,
                    Plain(f" {headline}】\n{body}"),
                ]
            )
        if participant_name:
            headline = f"{participant_name} {headline}"
        return MessageChain([Plain(f"【{headline}】\n{body}")])
