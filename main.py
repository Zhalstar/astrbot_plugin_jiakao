import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.config import PluginConfig
from .core.models import (
    AnswerStatus,
    Participant,
    QuestionView,
    QuizEvent,
    RevealResult,
    Subject,
)
from .core.sender import QuizSender
from .core.service import QuizService
from .core.storage import QuestionBank, StatsStore


class JiakaoPlugin(Star):
    """Expose the driving quiz service through AstrBot chat commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config)
        self.question_bank = QuestionBank(
            self.cfg.question_bank_path, self.cfg.image_dir
        )
        self.stats_store = StatsStore(self.cfg.stats_path)
        self.quiz_service = QuizService(
            self.question_bank,
            self.stats_store,
            self.cfg,
            event_sink=self._handle_quiz_event,
        )
        self.sender = QuizSender(self.context, self.cfg, self.question_bank)

    async def initialize(self) -> None:
        """Load the cached question bank and persistent leaderboard."""
        await self.question_bank.initialize()
        await self.stats_store.initialize()

    async def terminate(self) -> None:
        """Release service-owned timeout tasks."""
        await self.quiz_service.close()
        await self.sender.close()
        await self.question_bank.close()

    @filter.command("驾考")
    async def start_quiz(self, event: AstrMessageEvent, subject: str = "科一"):
        """驾考 科一/科四"""
        selected_subject = {
            "1": Subject.ONE,
            "科一": Subject.ONE,
            "科目一": Subject.ONE,
            "4": Subject.FOUR,
            "科四": Subject.FOUR,
            "科目四": Subject.FOUR,
        }.get(subject.strip())
        if selected_subject is None:
            await self.sender.send_text(event, "科目参数仅支持科一或科四")
            return
        umo = event.unified_msg_origin
        view = await self.quiz_service.start(umo, selected_subject)
        await self._publish_question(event, view, headline="驾考刷题开始")

    @filter.command("驾考下一题")
    async def next_question(self, event: AstrMessageEvent):
        """切换到下一题"""
        umo = event.unified_msg_origin
        view = await self.quiz_service.next_question(umo)
        if view is None:
            await self.sender.send_text(event, "当前没有答题会话")
            return
        await self._publish_question(event, view)

    @filter.command("驾考解析")
    async def reveal_answer(self, event: AstrMessageEvent):
        """公布答案和解析"""
        umo = event.unified_msg_origin
        reveal = await self.quiz_service.reveal(umo)
        if reveal is None:
            await self.sender.send_text(event, "当前没有等待作答的题目")
            return
        await self.sender.send_reveal(event, "已公布本题答案", reveal)
        if reveal.next_question is not None:
            await asyncio.sleep(3)
            await self._publish_question(event, reveal.next_question)
        event.stop_event()

    @filter.command("驾考结束")
    async def stop_quiz(self, event: AstrMessageEvent):
        """结束驾考答题"""
        umo = event.unified_msg_origin
        stopped = await self.quiz_service.stop(umo)
        message = "当前会话的驾考刷题已结束" if stopped else "当前没有答题会话"
        await self.sender.send_text(event, message)

    @filter.command("驾考排行")
    async def show_leaderboard(self, event: AstrMessageEvent):
        """查看驾考排行榜"""
        umo = event.unified_msg_origin
        entries = await self.quiz_service.leaderboard(umo)
        if not entries:
            await self.sender.send_text(event, "当前会话还没有答题记录。")
            return
        await self.sender.send_leaderboard(event, entries)

    @filter.regex(r"^(?:[A-Da-d](?:[\s,，、/+]*[A-Da-d]){0,3}|正确|错误|对|错|√|×)$")
    async def answer_question(self, event: AstrMessageEvent):
        """Submit a bare message as an answer to the active question."""
        await self._submit_answer(event, event.message_str)

    @filter.button_action()
    async def answer_button(self, event: AstrMessageEvent) -> None:
        """Submit a single-choice answer from a portable button callback."""
        interaction = event.get_button_interaction()
        if interaction is None or not interaction.action_id.startswith(
            QuizSender.ANSWER_ACTION_PREFIX
        ):
            return

        event.stop_event()
        data = interaction.data
        if not isinstance(data, dict):
            await self.sender.send_text(event, "答题按钮数据无效")
            return
        question_id = data.get("question_id")
        question_number = data.get("question_number")
        answer = data.get("answer")
        expected_action = f"{QuizSender.ANSWER_ACTION_PREFIX}{str(answer).lower()}"
        if (
            not isinstance(question_id, str)
            or not isinstance(question_number, int)
            or isinstance(question_number, bool)
            or answer not in {"A", "B", "C", "D"}
            or interaction.action_id != expected_action
        ):
            await self.sender.send_text(event, "答题按钮数据无效")
            return

        await self._submit_answer(
            event,
            answer,
            question_number=question_number,
            question_id=question_id,
        )

    async def _submit_answer(
        self,
        event: AstrMessageEvent,
        raw_answer: str,
        *,
        question_number: int | None = None,
        question_id: str | None = None,
    ) -> None:
        """Submit text or callback input through the shared answer workflow.

        Args:
            event: Text message or button interaction event.
            raw_answer: Answer value to normalize and score.
            question_number: Question number captured by a callback button.
            question_id: Question ID captured by a callback button.
        """
        umo = event.unified_msg_origin
        snapshot = await self.quiz_service.snapshot(umo)
        if snapshot is None or snapshot.current_question is None:
            if question_id is not None:
                await self.sender.send_text(event, "该答题按钮已失效")
            return

        submitted_number = (
            question_number if question_number is not None else snapshot.question_number
        )

        sender_id = str(event.get_sender_id())
        participant = Participant(
            user_id=sender_id,
            name=event.get_sender_name() or sender_id,
        )
        result = await self.quiz_service.submit_answer(
            umo,
            submitted_number,
            participant,
            raw_answer,
            expected_question_id=question_id,
        )
        if result.status in {
            AnswerStatus.NO_ACTIVE_QUESTION,
            AnswerStatus.STALE_QUESTION,
        }:
            if question_id is not None:
                await self.sender.send_text(event, "该答题按钮已失效")
            return
        if result.status is AnswerStatus.INVALID:
            return
        if result.status is AnswerStatus.ALREADY_ANSWERED:
            await self.sender.send_text(
                event, f"{participant.name}，每道题只能作答一次"
            )
            event.stop_event()
            return
        if result.status is AnswerStatus.WRONG:
            await self.sender.send_text(
                event, f"{participant.name} 回答错误，本题不能再次作答"
            )
            event.stop_event()
            return

        if (
            result.question is None
            or result.number is None
            or result.selected_subject is None
        ):
            logger.error("Jiakao correct result is missing question context")
            return
        reveal = RevealResult(
            question=result.question,
            number=result.number,
            selected_subject=result.selected_subject,
            participant=participant,
            score=result.score,
            next_question=result.next_question,
        )
        streak = (
            f"连对{result.score.current_streak}题 "
            if result.score is not None and result.score.current_streak > 1
            else ""
        )
        await self.sender.send_reveal(
            event,
            f"{participant.name} 回答正确！{streak}",
            reveal,
        )
        if result.next_question is not None:
            await asyncio.sleep(1.5)
            await self._publish_question(event, result.next_question)
        event.stop_event()

    async def _publish_question(
        self,
        event: AstrMessageEvent,
        view: QuestionView,
        headline: str | None = None,
    ) -> None:
        """Send a question, then activate its timeout and image prefetch.

        Args:
            event: Platform event used to send the question.
            view: Question view selected by the quiz service.
            headline: Optional heading shown before the question.
        """
        await self.sender.send_question(event, view, headline)
        umo = event.unified_msg_origin
        next_question = await self.quiz_service.activate_question(umo, view.number)
        self.sender.prefetch(next_question)

    async def _handle_quiz_event(self, event: QuizEvent) -> None:
        await self.sender.send_timeout(event.umo, event.reveal)
