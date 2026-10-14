"""火山方舟（Ark）Seedance 契约门面 —— 本服务对外的主契约面。

对外形态逐字段对齐方舟《创建视频生成任务》/《查询视频生成任务》
（`POST|GET /api/v3/contents/generations/tasks`），底层翻译到 chat.qwen.ai 视频链路：
方舟 SDK / 既有调用方**只换 Base URL + Key** 即可接入。

范围（冻结）：**创建 + 查询**两个端点；列表 / 取消（DELETE）**不实现**（路由不存在，
接入文档显式声明；不返回空列表之类的假数据）。

降维（Seedance 超集 → qwen 子集）：
  · `content[]`：`text` 按序换行拼接；`image_url`（first_frame | 无 role）→ i2v 首帧（**仅一张**）；
    其余角色/类型（last_frame / reference_image / video_url / audio_url / draft_task）→ **400** ——
    它们承担"控制"，丢掉会改变"用户想要什么"，一律响亮拒绝、不静默降级；
  · `ratio`：只吃 `1:1 / 3:4 / 4:3 / 16:9 / 9:16`；枚举外（含 `adaptive`、缺失、写错）
    一律落 **1:1 + 降级说明**（用户 2026-09-17 冻结口径，刻意偏离"缺省 16:9"，用 warning 换可观测性）；
  · `duration`：上游固定 ~5s 不可配 ⇒ >5 吸附到 5、`-1` 替换成 5、**<5 直接 400**；
  · `resolution` / `seed` / `watermark` / `camera_fixed` / `generate_audio` / `return_last_frame` /
    `frames` / `service_tier` / `draft` / `priority` / `safety_identifier` / `tools` /
    `omni_reference_task_type` / `output_format` / `callback_url` / `execution_expires_after`
    → 认得但做不到 ⇒ **只进服务端日志**（不假装支持、不静默丢弃；2026-10-14 起不再下发给调用方）。

诚实边界（不许编）：
  · `usage` **不给** —— 上游没有 token 口径（视频额度是 3 次/天计数，不是 token）；
  · `resolution` **不回填** —— 上游不回传（编一个尺寸比留空更糟）；
  · `duration` 仅在 `succeeded` 时给 **5** —— 上游出片固定 ~5.042s（n≥5 次容器实测），
    这是"实际产出值"而非请求值；
  · 降级说明**不下发**（2026-10-14 用户口径）：本层照旧算出完整 `degradations[]`（供服务端
    日志与排队记录留痕），但**任何对外报文都不再带该键** —— 响应与方舟原生逐字段一致，
    "unknown field 报错"的严格 SDK 问题随之消失。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import InvalidParameterError
from .media import host_warning, validate_image_url
from .store import TaskRecord

#: 上游（qwen）只吃这 5 个比例；其余（含 adaptive / 缺失 / 写错）一律落 1:1 + 说明。
RATIO_ENUM = ("1:1", "3:4", "4:3", "16:9", "9:16")
FALLBACK_RATIO = "1:1"

#: 上游出片时长固定 ~5.042s（不可指定）——仅成功时回填。
UPSTREAM_FIXED_DURATION = 5

#: 本服务只接一个上游；`model` 的 provider 段必须等于它（不认识的一律 400，不猜）。
SUPPORTED_PROVIDER = "qwen"

#: 认得、但（本上游）做不到的顶层参数 → 降级说明。
_ARK_UNSUPPORTED: dict[str, str] = {
    "resolution": "上游分辨率不可指定（由链路决定）—— 已忽略，不回填。",
    "seed": "上游没有 seed 参数 —— 已忽略。",
    "watermark": "上游没有水印开关（产物是否带水印由上游决定）—— 已忽略。",
    "camera_fixed": "上游没有固定镜头参数 —— 已忽略。",
    "generate_audio": "上游产物无音轨（链路不支持音画联合）—— 已忽略。",
    "return_last_frame": "上游不返回尾帧图 —— 已忽略（连续视频拼接链路会断）。",
    "frames": "上游按时长（固定 ~5s）而非帧数生成 —— 已忽略。",
    "service_tier": "上游没有服务等级概念 —— 已忽略。",
    "draft": "上游没有样片模式 —— 已忽略。",
    "priority": "上游不支持执行优先级 —— 已忽略。",
    "safety_identifier": "上游无对应槽位 —— 已忽略。",
    "tools": "上游不支持工具配置 —— 已忽略。",
    "omni_reference_task_type": "上游无多模态参考（仅文生视频与单张首帧图生视频）—— 已忽略。",
    "output_format": "输出格式由上游决定（实测 mp4）—— 已忽略。",
    "callback_url": "本服务未实现回调（范围：仅创建 + 查询）—— 请轮询查询接口。",
    "execution_expires_after": "超时阈值由本服务 TASK_TIMEOUT 决定 —— 已忽略。",
}


@dataclass
class CreatePlan:
    model_requested: str
    prompt: str
    ratio: str
    chat_type: str          # "t2v" | "i2v"
    image_url: str | None
    degradations: list[str] = field(default_factory=list)


def _resolve_model(raw) -> str:
    model = str(raw or "").strip()
    if not model:
        raise InvalidParameterError(
            "缺少 model（本服务形态：qwen/<任意名> 或裸模型名）", param="model")
    if "/" in model:
        provider, _, tail = model.partition("/")
        if provider != SUPPORTED_PROVIDER:
            raise InvalidParameterError(
                f"provider {provider!r} 不是本服务支持的上游 —— 本服务只有 "
                f"{SUPPORTED_PROVIDER}（其他上游请走对应渠道）。", param="model")
        if not tail.strip():
            raise InvalidParameterError("model 的模型段为空", param="model")
    return model


def translate_ark_create(body: dict) -> CreatePlan:
    """方舟创建请求 → 本服务受理计划 + 降级说明。

    **契约不符当场 400**（content 角色 / 图片数量 / 模型名 / duration<5）；
    **能力不及降级留痕**（ratio 吸附 / duration 吸附 / 认得但做不到的参数）。
    """
    if not isinstance(body, dict):
        raise InvalidParameterError("请求体必须是 JSON 对象")
    model_requested = _resolve_model(body.get("model"))

    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise InvalidParameterError(
            "content 必须是非空数组（方舟形态：[{type: \"text\", text: …}, …]）", param="content")

    degradations: list[str] = []
    texts: list[str] = []
    images: list[str] = []

    for i, item in enumerate(content):
        if not isinstance(item, dict):
            raise InvalidParameterError(f"content[{i}] 必须是对象", param="content")
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                texts.append(text)
            continue
        if item_type == "image_url":
            role = str(item.get("role") or "")
            if role == "last_frame":
                raise InvalidParameterError(
                    "上游没有尾帧能力：last_frame 无法满足，也不把它悄悄当首帧"
                    "（首帧 + 尾帧 ≠ 只有首帧，静默丢一帧就是换了产品）。",
                    param=f"content[{i}].role")
            if role == "reference_image":
                raise InvalidParameterError(
                    "上游不支持参考图（只有首帧图生视频）—— 参考图承担控制，不能静默丢。",
                    param=f"content[{i}].role")
            if role not in ("first_frame", ""):
                raise InvalidParameterError(
                    f"content[{i}] 的 role={role!r} 不是方舟图片角色的标准取值。",
                    param=f"content[{i}].role")
            url = (item.get("image_url") or {}).get("url") if isinstance(item.get("image_url"), dict) else None
            if isinstance(item.get("image_url"), str):  # 第三方兼容形态（url 平铺）也收
                url = item["image_url"]
            images.append(validate_image_url(url, param=f"content[{i}].image_url"))
            continue
        if item_type in ("video_url", "audio_url", "draft_task"):
            raise InvalidParameterError(
                f"content[{i}]: 上游 qwen 视频链路不支持 {item_type}"
                f"（仅文生视频与单张首帧图生视频）—— 不静默降级。",
                param=f"content[{i}]")
        raise InvalidParameterError(
            f"content[{i}] 的 type {item_type!r} 不是方舟视频生成契约的取值。", param=f"content[{i}]")

    if not texts:
        raise InvalidParameterError("content 里缺少 text（prompt 不能为空）", param="content")
    if len(images) > 1:
        raise InvalidParameterError(
            f"上游只支持**一张**首帧图（收到 {len(images)} 张）—— 不替调用方挑一张；"
            "请只给一张 first_frame。", param="content")

    image_url = images[0] if images else None
    if image_url:
        warn = host_warning(image_url)
        if warn:
            degradations.append(warn)
    chat_type = "i2v" if image_url else "t2v"

    # ---- ratio：枚举外一律 1:1 + 说明（用户冻结口径 D-2）
    raw_ratio = body.get("ratio")
    ratio = str(raw_ratio).strip() if raw_ratio is not None else ""
    if ratio not in RATIO_ENUM:
        shown = ratio or "(缺失)"
        degradations.append(
            f"ratio {shown}：上游只吃 {' / '.join(RATIO_ENUM)}；枚举外（含 adaptive、缺失、写错）"
            f"一律按 {FALLBACK_RATIO} 处理（冻结口径 D-2）。")
        ratio = FALLBACK_RATIO

    # ---- duration：固定 ~5s
    raw_duration = body.get("duration")
    if raw_duration is not None:
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float, str)):
            raise InvalidParameterError("duration 必须是整数秒", param="duration")
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError) as exc:
            raise InvalidParameterError("duration 必须是整数秒", param="duration") from exc
        if duration == -1:
            degradations.append("duration=-1（模型自选）：上游出片固定 ~5s ⇒ 已替换为 5。")
        elif duration < UPSTREAM_FIXED_DURATION:
            raise InvalidParameterError(
                f"duration={duration}：上游出片固定 ~5s，短于 {UPSTREAM_FIXED_DURATION}s 无法满足"
                "（不做「看起来像」的假支持）。", param="duration")
        elif duration > UPSTREAM_FIXED_DURATION:
            degradations.append(
                f"duration={duration}：上游固定 ~{UPSTREAM_FIXED_DURATION}s 不可配 ⇒ 已吸附到 "
                f"{UPSTREAM_FIXED_DURATION}（向下吸附，不变贵）。")

    # ---- 认得但做不到的参数
    for key, note in _ARK_UNSUPPORTED.items():
        value = body.get(key)
        if value is None:
            continue
        if key == "seed" and value == -1:   # 方舟默认 -1 = 随机 ⇒ 等价"没给"
            continue
        degradations.append(f"参数 {key}={value!r}：{note}")

    extra_body = body.get("extra_body")
    if isinstance(extra_body, dict):
        for key in extra_body:
            degradations.append(f"extra_body.{key}：未建模字段，上游不支持 —— 已忽略。")

    return CreatePlan(
        model_requested=model_requested,
        prompt="\n".join(texts),
        ratio=ratio,
        chat_type=chat_type,
        image_url=image_url,
        degradations=degradations,
    )


def ark_task_view(rec: TaskRecord) -> dict:
    """任务记录 → 方舟《查询视频生成任务》响应形状。

    只给**真知道**的值：`content` / `duration` 仅成功时出现；`resolution` / `usage` /
    `seed` 等一概不出现（不编常量、不拿 null 占位）。`error` 是唯一显式可空字段
    （方舟规定成功时为 null）。降级说明**不下发**（只进服务端日志，2026-10-14 口径）。
    """
    out: dict = {
        "id": rec.local_id,
        "model": rec.model_requested,
        "status": rec.status,
        "error": None,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }
    if rec.status == "failed" and rec.error_message:
        out["error"] = {"code": rec.error_code or "internal", "message": rec.error_message}
    if rec.status == "succeeded" and rec.video_url:
        out["content"] = {"video_url": rec.video_url}
        out["duration"] = UPSTREAM_FIXED_DURATION
    if rec.ratio:
        out["ratio"] = rec.ratio
    # 降级说明（rec.degradations）**刻意不读** —— 它是服务端日志与排队记录的口袋，
    # 不出现在任何对外响应里（2026-10-14 用户口径）。
    return out


__all__ = [
    "CreatePlan",
    "RATIO_ENUM",
    "FALLBACK_RATIO",
    "SUPPORTED_PROVIDER",
    "UPSTREAM_FIXED_DURATION",
    "ark_task_view",
    "translate_ark_create",
]
