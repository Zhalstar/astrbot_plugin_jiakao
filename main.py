import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .core.config import PluginConfig
from .core.models import (
    AnswerStatus,
    Participant,
    QuestionType,
    QuestionView,
    QuizEvent,
    RevealResult,
    Subject,
)
from .core.service import QuizService
from .core.storage import QuestionBank, StatsStore


class JiakaoPlugin(Star):
    """Expose the driving quiz service through AstrBot group commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config)
        self.question_bank = QuestionBank(self.cfg.question_bank_path)
        self.stats_store = StatsStore(self.cfg.stats_path)
        self.quiz_service = QuizService(
            self.question_bank,
            self.stats_store,
            self.cfg,
            event_sink=self._handle_quiz_event,
        )

    async def initialize(self) -> None:
        """Load the cached question bank and persistent leaderboard."""
        await self.question_bank.initialize()
        await self.stats_store.initialize()

    async def terminate(self) -> None:
        """Release service-owned timeout tasks."""
        await self.quiz_service.close()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
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
            yield event.plain_result("科目参数仅支持科一或科四")
            return
        result = await self._start_quiz(event, selected_subject)
        yield event.chain_result(result.chain)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("驾考下一题")
    async def next_question(self, event: AstrMessageEvent):
        """切换到下一题"""
        view = await self.quiz_service.next_question(event.unified_msg_origin)
        if view is None:
            yield event.plain_result("当前没有答题会话")
            return
        yield event.chain_result(self._question_chain(view).chain)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("驾考解析")
    async def reveal_answer(self, event: AstrMessageEvent):
        """公布答案和解析"""
        reveal = await self.quiz_service.reveal(event.unified_msg_origin)
        if reveal is None:
            yield event.plain_result("当前没有等待作答的题目")
            return
        await event.send(self._reveal_chain("已公布本题答案", reveal))
        if reveal.next_question is not None:
            await asyncio.sleep(3)
            await event.send(self._question_chain(reveal.next_question))
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("驾考结束")
    async def stop_quiz(self, event: AstrMessageEvent):
        """结束驾考答题"""
        stopped = await self.quiz_service.stop(event.unified_msg_origin)
        message = "本群驾考刷题已结束" if stopped else "当前没有答题会话"
        yield event.plain_result(message)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("驾考排行")
    async def show_leaderboard(self, event: AstrMessageEvent):
        """查看驾考排行榜"""
        entries = await self.quiz_service.leaderboard(event.unified_msg_origin)
        if not entries:
            yield event.plain_result("本群还没有答题记录。")
            return

        lines = ["【驾考刷题排行榜】"]
        for index, entry in enumerate(entries, start=1):
            accuracy = entry.correct / entry.total * 100 if entry.total else 0
            lines.append(
                f"{index}. {entry.name}  "
                f"答对 {entry.correct} / {entry.total}  "
                f"正确率 {accuracy:.0f}%  最佳连对 {entry.best_streak}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.regex(r"^(?:[A-Da-d](?:[\s,，、/+]*[A-Da-d]){0,3}|正确|错误|对|错|√|×)$")
    async def answer_question(self, event: AstrMessageEvent):
        """Submit a bare group message as an answer to the active question"""
        scope_id = event.unified_msg_origin
        snapshot = await self.quiz_service.snapshot(scope_id)
        if snapshot is None or snapshot.current_question is None:
            return

        sender_id = str(event.get_sender_id())
        participant = Participant(
            user_id=sender_id,
            name=event.get_sender_name() or sender_id,
        )
        result = await self.quiz_service.submit_answer(
            scope_id,
            snapshot.question_number,
            participant,
            event.message_str,
        )
        if result.status in {
            AnswerStatus.NO_ACTIVE_QUESTION,
            AnswerStatus.STALE_QUESTION,
            AnswerStatus.INVALID,
        }:
            return
        if result.status is AnswerStatus.ALREADY_ANSWERED:
            yield event.plain_result(f"{participant.name}，每道题只能作答一次")
            event.stop_event()
            return
        if result.status is AnswerStatus.WRONG:
            yield event.plain_result(f"{participant.name} 回答错误，本题不能再次作答")
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
        await event.send(
            self._reveal_chain(f"{participant.name} 回答正确！{streak}", reveal)
        )
        if result.next_question is not None:
            await asyncio.sleep(1.5)
            await event.send(self._question_chain(result.next_question))
        event.stop_event()

    async def _start_quiz(
        self, event: AstrMessageEvent, subject: Subject
    ) -> MessageChain:
        view = await self.quiz_service.start(event.unified_msg_origin, subject)
        return self._question_chain(view, headline="驾考刷题开始")

    def _question_chain(
        self, view: QuestionView, headline: str | None = None
    ) -> MessageChain:
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
        lines.extend(
            f"{chr(ord('A') + index)}. {option}"
            for index, option in enumerate(question.options)
        )
        chain = MessageChain([Plain("\n".join(lines))])
        if question.image_url:
            chain.chain.append(Image.fromURL(question.image_url))
        return chain

    def _reveal_chain(self, headline: str, reveal: RevealResult) -> MessageChain:
        question = reveal.question
        answer_options = []
        for letter in question.answer:
            option_index = ord(letter) - ord("A")
            answer_options.append(f"{letter}. {question.options[option_index]}")
        lines = [
            f"【{headline}】",
            f"答案：{'；'.join(answer_options)}",
        ]
        if question.explanation:
            lines.append(f"解析：{question.explanation}")
        return MessageChain([Plain("\n".join(lines))])

    async def _handle_quiz_event(self, event: QuizEvent) -> None:
        chain = self._reveal_chain("本题超时", event.reveal)
        try:
            sent = await self.context.send_message(event.scope_id, chain)
            if not sent:
                logger.warning(
                    f"Jiakao timeout message has no matching platform: {event.scope_id}"
                )
        except Exception:
            logger.exception("Jiakao timeout message delivery failed")
