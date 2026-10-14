"""降级说明的**服务端落点** —— 2026-10-14 用户口径：不再展示给客户端。

翻译层（`ark.py` / `openai_chat.py`）会把"能力不及、参数吸附、隐式拍平、思考泄漏降档"
这类事实记成 `degradations[]`。此前它是随响应下发的**加性扩展**；现在**只写服务端日志**，
任何对外报文（方舟门查询/创建、chat 门、Responses 门、两扇门的 dry-run）都不再出现该键。

为什么保留计算：可观测性从「调用方可见」挪到「运营方可查」—— 降级照样发生、照样可查，
只是不再污染契约（严格 SDK 的 unknown-field 报错问题也随之消失，响应键集与原生逐字段一致）。

本模块**只做打一条日志**，不做任何格式化以外的决策：是否降级、降什么，一律由翻译层定。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger("qwen.degradations")


def log_degradations(channel: str, items: Iterable[str] | None, **context: object) -> None:
    """把一次请求产生的降级说明写进服务端日志（**空清单不打**，避免刷屏）。

    `channel`：门/路径标签（`video` / `chat` / `responses` …），便于按门检索；
    `context`：定位用的键值对（`model` / `request_id` …），只打印有值的键。
    """
    notes = [str(item) for item in (items or ()) if str(item)]
    if not notes:
        return
    where = " ".join(f"{key}={value}" for key, value in context.items()
                     if value not in (None, ""))
    logger.info("降级说明[%s]%s：%s", channel, f" {where}" if where else "", "；".join(notes))


__all__ = ["log_degradations"]
