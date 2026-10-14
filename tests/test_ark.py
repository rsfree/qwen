"""翻译层（`app/ark.py`）：方舟请求 → 受理计划；任务记录 → 方舟视图。零网络。"""
from __future__ import annotations

import pytest

from app.ark import (
    FALLBACK_RATIO,
    UPSTREAM_FIXED_DURATION,
    ark_task_view,
    translate_ark_create,
)
from app.errors import InvalidParameterError
from app.store import TaskRecord

IMG = "https://cdn.qwenlm.ai/output/u/image_gen/m1/1.png"
FOREIGN_IMG = "https://example.com/a.png"


def body(**overrides) -> dict:
    """基线：合法的最小 t2v 请求（ratio 显式给 16:9，避免"缺失降级"混进各用例）。"""
    base = {"model": "qwen/video",
            "content": [{"type": "text", "text": "一只猫在草地上奔跑"}],
            "ratio": "16:9"}
    base.update(overrides)
    return base


# ------------------------------------------------------------------ model

def test_missing_model_is_400():
    with pytest.raises(InvalidParameterError, match="model"):
        translate_ark_create({"content": [{"type": "text", "text": "x"}]})


def test_foreign_provider_is_400():
    with pytest.raises(InvalidParameterError, match="provider"):
        translate_ark_create(body(model="jimeng/whatever"))


def test_bare_model_is_accepted_for_the_single_upstream():
    plan = translate_ark_create(body(model="qwen3.7-plus"))
    assert plan.model_requested == "qwen3.7-plus"


# ------------------------------------------------------------------ content[]

def test_multiple_texts_are_joined_in_order():
    plan = translate_ark_create(body(content=[
        {"type": "text", "text": " 第一段 "}, {"type": "text", "text": "第二段"}]))
    assert plan.prompt == "第一段\n第二段"


def test_missing_text_is_400():
    with pytest.raises(InvalidParameterError, match="text"):
        translate_ark_create(body(content=[
            {"type": "image_url", "image_url": {"url": IMG}, "role": "first_frame"}]))


def test_unknown_content_type_is_400():
    with pytest.raises(InvalidParameterError):
        translate_ark_create(body(content=[{"type": "text", "text": "x"}, {"type": "mystery"}]))


@pytest.mark.parametrize("item", [
    {"type": "video_url", "video_url": {"url": "https://x/v.mp4"}, "role": "reference_video"},
    {"type": "audio_url", "audio_url": {"url": "https://x/a.mp3"}, "role": "reference_audio"},
    {"type": "draft_task", "draft_task": {"id": "t"}},
])
def test_upstream_missing_families_are_400_not_silently_dropped(item):
    with pytest.raises(InvalidParameterError):
        translate_ark_create(body(content=[{"type": "text", "text": "x"}, item]))


def test_first_frame_switches_to_i2v():
    plan = translate_ark_create(body(content=[
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": IMG}, "role": "first_frame"}]))
    assert plan.chat_type == "i2v" and plan.image_url == IMG
    assert plan.degradations == []


def test_image_without_role_is_first_frame_too():
    plan = translate_ark_create(body(content=[
        {"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": IMG}}]))
    assert plan.chat_type == "i2v" and plan.image_url == IMG


def test_last_frame_is_400():
    with pytest.raises(InvalidParameterError, match="尾帧"):
        translate_ark_create(body(content=[
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": IMG}, "role": "last_frame"}]))


def test_reference_image_is_400():
    with pytest.raises(InvalidParameterError, match="参考图"):
        translate_ark_create(body(content=[
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": IMG}, "role": "reference_image"}]))


def test_two_images_are_400_and_never_silently_trimmed():
    with pytest.raises(InvalidParameterError, match="一张"):
        translate_ark_create(body(content=[
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": IMG}, "role": "first_frame"},
            {"type": "image_url", "image_url": {"url": IMG}, "role": "first_frame"}]))


def test_foreign_host_image_forwarded_with_warning():
    plan = translate_ark_create(body(content=[
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": FOREIGN_IMG}}]))
    assert plan.image_url == FOREIGN_IMG
    assert any("非上游域名" in item for item in plan.degradations)


def test_data_uri_is_rejected_with_actionable_message():
    with pytest.raises(InvalidParameterError, match="data:"):
        translate_ark_create(body(content=[
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]))


# ------------------------------------------------------------------ ratio

@pytest.mark.parametrize("ratio", ["1:1", "3:4", "4:3", "16:9", "9:16"])
def test_ratio_enum_passthrough_silently(ratio):
    plan = translate_ark_create(body(ratio=ratio))
    assert plan.ratio == ratio
    assert plan.degradations == []


@pytest.mark.parametrize("ratio", ["adaptive", "1:2", ""])
def test_ratio_outside_enum_falls_back_with_note(ratio):
    plan = translate_ark_create(body(ratio=ratio))
    assert plan.ratio == FALLBACK_RATIO
    assert any(FALLBACK_RATIO in item for item in plan.degradations)


def test_ratio_missing_falls_back_with_note():
    plan = translate_ark_create(body(ratio=None))
    assert plan.ratio == FALLBACK_RATIO
    assert any("缺失" in item for item in plan.degradations)


# ------------------------------------------------------------------ duration

def test_duration_five_is_silent():
    assert translate_ark_create(body(duration=5)).degradations == []


def test_duration_greater_is_clamped_down_with_note():
    plan = translate_ark_create(body(duration=12))
    assert any("吸附" in item for item in plan.degradations)


def test_duration_minus_one_replaced_with_note():
    plan = translate_ark_create(body(duration=-1))
    assert any("-1" in item for item in plan.degradations)


def test_duration_below_five_is_400():
    with pytest.raises(InvalidParameterError, match="duration"):
        translate_ark_create(body(duration=2))


# ------------------------------------------------------------------ 认得但做不到

def test_recognized_but_unsupported_params_are_reported():
    plan = translate_ark_create(body(
        watermark=True, return_last_frame=True, generate_audio=True,
        resolution="1080p", camera_fixed=False, callback_url="https://cb.example/x"))
    joined = "\n".join(plan.degradations)
    for key in ("watermark", "return_last_frame", "generate_audio",
                "resolution", "camera_fixed", "callback_url"):
        assert key in joined


def test_seed_minus_one_is_not_reported():
    assert translate_ark_create(body(seed=-1)).degradations == []


def test_explicit_seed_is_reported():
    assert any("seed" in item for item in translate_ark_create(body(seed=11)).degradations)


def test_extra_body_unmodeled_keys_reported():
    plan = translate_ark_create(body(extra_body={"omni_reference_task_type": "edit"}))
    assert any("extra_body" in item for item in plan.degradations)


# ------------------------------------------------------------------ 视图

def make_rec(**overrides) -> TaskRecord:
    rec = TaskRecord(local_id="cgt-20260922000000-abcde", model_requested="qwen/video",
                     status="running", created_at=100, updated_at=200)
    for key, value in overrides.items():
        setattr(rec, key, value)
    return rec


def test_view_running_is_minimal_and_truthful():
    view = ark_task_view(make_rec())
    assert set(view) == {"id", "model", "status", "error", "created_at", "updated_at"}
    assert view["error"] is None


def test_view_succeeded_adds_content_and_duration():
    view = ark_task_view(make_rec(status="succeeded", video_url="https://x/v.mp4", ratio="16:9"))
    assert view["content"] == {"video_url": "https://x/v.mp4"}
    assert view["duration"] == UPSTREAM_FIXED_DURATION
    assert view["ratio"] == "16:9"


def test_view_does_not_fabricate_unknown_fields():
    view = ark_task_view(make_rec(status="running"))
    for key in ("resolution", "seed", "usage", "frames", "framespersecond", "draft"):
        assert key not in view


def test_view_failed_carries_error():
    view = ark_task_view(make_rec(status="failed", error_code="InternalServiceError",
                                  error_message="上游挂了"))
    assert view["error"] == {"code": "InternalServiceError", "message": "上游挂了"}


def test_view_never_exposes_degradations():
    """🔴 2026-10-14 用户口径：降级说明**不下发**（只进服务端日志）—— 记录里有也不出现。"""
    rec = make_rec()
    rec.set_degradations(["x"])
    assert "degradations" not in ark_task_view(rec)
    assert "degradations" not in ark_task_view(make_rec())
