from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from mewcode.hooks.executors import execute_action
from mewcode.hooks.models import ActionResult, Hook, HookContext, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []
        #: 要送给**模型**（system reminder）的通知，和 UI 那条通道分开。
        #
        # ⚠️ 修复记录：旧实现只有一个 `_notifications` 队列，而 `agent.run()`
        #   里 `_drain_hook_events()`（送 UI）和 `drain_notifications()`（喂模型）
        #   抢的是同一个队列 —— 前者先跑，队列被清空，于是**同步 hook 的输出
        #   永远到不了模型**。想让模型看到（比如"lint 报了 3 个错，去改"），
        #   唯一的办法是把它塞进 system reminder，所以这里必须分成两个队列。
        self._model_reminders: list[HookNotification] = []


    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched


    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                asyncio.ensure_future(self._run_single(hook, ctx))
            else:
                await self._run_single(hook, ctx)


    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            note = HookNotification(
                hook_id=hook.id,
                event=hook.event,
                output=result.output,
                success=result.success,
            )
            self._notifications.append(note)
            # 显式声明 notify_model 的 hook 才会额外进"喂模型"那条队列——
            # 默认不进，避免所有 hook 的输出都变成 system reminder 造成噪声。
            if hook.notify_model and result.output:
                self._model_reminders.append(note)
            if not result.success:
                log.warning(
                    "Hook '%s' action failed: %s", hook.id, result.output
                )
        except Exception as e:
            log.warning("Hook '%s' execution error: %s", hook.id, e)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=str(e),
                    success=False,
                )
            )


    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
                    )
                )
                if hook.reject:
                    return ToolRejectedError(
                        tool=ctx.tool_name,
                        reason=result.output,
                        hook_id=hook.id,
                    )
            except Exception as e:
                log.warning("Hook '%s' execution error: %s", hook.id, e)
        return None

    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages


    def drain_notifications(self) -> list[HookNotification]:
        """取走**给 UI 看**的通知。和 `drain_model_reminders` 是两个独立队列。"""
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications


    def drain_model_reminders(self) -> list[HookNotification]:
        """取走**要喂给模型**的通知（写成 system reminder）。

        只有声明了 `notify_model: true` 的 hook 才会进这个队列。
        单独一个方法、单独一个队列，是为了修掉"UI 那条通道把队列清空、
        导致 hook 输出永远到不了模型"这个缺陷。
        """
        reminders = list(self._model_reminders)
        self._model_reminders.clear()
        return reminders
