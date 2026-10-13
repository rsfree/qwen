#!/usr/bin/env python3
"""活体冒烟驱动：对**真实上游**做阶梯测试（signin → dryrun → 创建 → 轮询 → 下载产物）。

用法（仓库根目录，先 `set -a; source .env; set +a`）：
    python scripts/live_smoke.py signin [--account EMAIL]
    python scripts/live_smoke.py dryrun [--kind t2v|i2v] [--base http://127.0.0.1:8400]
    python scripts/live_smoke.py run    [--kind t2v|i2v] [--image URL]
                                        [--interval 8] [--timeout 600] [--out var/live]
    python scripts/live_smoke.py chat   [--prompt TEXT] [--model ID] [--stream]
                                        [--key KEY] [--base http://127.0.0.1:8400]

纪律：
  · `signin` 免费但**必须走轮换出口**（本脚本直接调 signin 模块，不经服务）；
  · `dryrun` / chat 门的 `X-Avm-Dry-Run` 零成本（不触上游）；
  · `run` **每次真实消耗 1 次视频额度**（3 次/天/账号）——发之前想清楚；
  · `chat` 真实一发 **t2t 文本对话**（免费、不消耗视频额度，但仍是真实上游写请求）——
    首次运行请回填 `docs/UPSTREAM.md` U-12/U-13（流式响应形态 / chats/new 参数影响）。
本脚本刻意用 `trust_env=False`（与服务同口径），避免沙箱透明代理接管。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings  # noqa: E402

EXAMPLE_IMAGE = "https://qwen-chat.oss-ap-southeast-1.aliyuncs.com/resources/i2v/1762498392.png"
PROMPT_T2V = "一只橘猫坐在窗台上看雨，窗外霓虹灯光映在玻璃上，电影感镜头缓慢推进"
PROMPT_I2V = "潜水员在深海中缓缓转身，探照灯光束扫过沉船残骸，鱼群四散"
PROMPT_CHAT = "用一句话介绍你自己"
TASKS = "/api/v3/contents/generations/tasks"
CHAT = "/v1/chat/completions"


def mp4_duration_seconds(path: Path) -> float | None:
    # 注意：部分产物把 moov 放在文件**尾部**（非 faststart）⇒ 必须搜全文，不能只看头部
    data = path.read_bytes()
    index = data.find(b"mvhd")
    if index < 0:
        return None
    index += 4
    if data[index] == 0:
        timescale = int.from_bytes(data[index + 12:index + 16], "big")
        duration = int.from_bytes(data[index + 16:index + 20], "big")
    else:
        timescale = int.from_bytes(data[index + 20:index + 24], "big")
        duration = int.from_bytes(data[index + 24:index + 28], "big")
    return duration / timescale if timescale else None


def build_payload(kind: str, image: str | None) -> dict:
    content = [{"type": "text", "text": PROMPT_I2V if kind == "i2v" else PROMPT_T2V}]
    if kind == "i2v":
        content.append({"type": "image_url",
                        "image_url": {"url": image or EXAMPLE_IMAGE},
                        "role": "first_frame"})
    return {"model": "qwen/video", "content": content, "ratio": "16:9", "duration": 5}


def cmd_signin(args) -> int:
    settings = Settings.from_env()
    if not settings.accounts:
        print("[signin] FAIL: 没有账号（QWEN_ACCOUNTS 未设置）")
        return 2
    email = args.account or next(iter(settings.accounts))

    from app.upstream.qwen.accounts import AccountPool

    pool = AccountPool(settings)
    started = time.time()
    try:
        token = pool.token_for(email)
    except Exception as exc:  # noqa: BLE001
        print(f"[signin] FAIL account={email} {type(exc).__name__}: {exc}")
        return 1
    print(f"[signin] OK account={email} token_len={len(token)} prefix={token[:8]}... "
          f"耗时={time.time() - started:.1f}s（token 全文不打印、不落盘）")
    return 0


def cmd_dryrun(args) -> int:
    with httpx.Client(base_url=args.base, timeout=30, trust_env=False) as client:
        resp = client.post(TASKS, json=build_payload(args.kind, args.image),
                           headers={"X-Avm-Dry-Run": "1"})
    print(f"[dryrun] HTTP {resp.status_code}")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2)[:1600])
    return 0 if resp.status_code == 200 else 1


def cmd_run(args) -> int:
    with httpx.Client(base_url=args.base, timeout=60, trust_env=False) as client:
        print(f"[health] {client.get('/healthz').json()} ready={client.get('/readyz').json()}")
        resp = client.post(TASKS, json=build_payload(args.kind, args.image))
        print(f"[create] HTTP {resp.status_code} {resp.text[:300]}")
        if resp.status_code != 200:
            return 1
        task_id = resp.json()["id"]

        deadline = time.time() + args.timeout
        last_status = None
        view: dict = {}
        while time.time() < deadline:
            got = client.get(f"{TASKS}/{task_id}")
            if got.status_code != 200:
                print(f"[poll] HTTP {got.status_code} {got.text[:300]}")
                return 1
            view = got.json()
            if view["status"] != last_status:
                brief = {k: view.get(k) for k in ("error", "duration", "ratio", "degradations")}
                print(f"[poll] {time.strftime('%H:%M:%S')} status={view['status']} "
                      f"{json.dumps(brief, ensure_ascii=False)}")
                last_status = view["status"]
            if view["status"] in ("succeeded", "failed", "expired"):
                break
            time.sleep(args.interval)
        print(f"[final] {json.dumps(view, ensure_ascii=False)[:700]}")
        if view["status"] != "succeeded":
            return 1

        url = view["content"]["video_url"]
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{args.kind}-{task_id}.mp4"
        with httpx.Client(timeout=180, trust_env=False, follow_redirects=True) as downloader:
            blob = downloader.get(url)
            blob.raise_for_status()
            out.write_bytes(blob.content)
        duration = mp4_duration_seconds(out)
        print(f"[artifact] {out} size={out.stat().st_size / 1_000_000:.2f}MB "
              f"duration={duration:.3f}s" if duration else
              f"[artifact] {out} size={out.stat().st_size / 1_000_000:.2f}MB")
        print(f"[done] kind={args.kind} task={task_id}")
        return 0


def cmd_chat(args) -> int:
    """真实一发 t2t（免费、不耗视频额度）。验证 U-12（SSE 形态）/ U-13（chats/new 参数）。"""
    payload = {"model": args.model or "qwen3.7-plus", "stream": bool(args.stream),
               "messages": [{"role": "user", "content": args.prompt}]}
    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}
    started = time.time()
    with httpx.Client(base_url=args.base, timeout=120, trust_env=False) as client:
        if args.stream:
            pieces: list[str] = []
            with client.stream("POST", CHAT, json=payload, headers=headers) as resp:
                print(f"[chat] HTTP {resp.status_code} content-type={resp.headers.get('content-type')}")
                if resp.status_code != 200:
                    print(resp.read().decode("utf-8", "replace")[:600])
                    return 1
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    if chunk == "[DONE]":
                        print("\n[chat] [DONE]")
                        break
                    try:
                        event = json.loads(chunk)
                        delta = event["choices"][0].get("delta", {})
                        text = delta.get("content") or ""
                    except (ValueError, KeyError, IndexError):
                        text = ""
                        print(f"[chat] (未识别事件形态，原样) {chunk[:200]}")
                    if text:
                        pieces.append(text)
                        print(text, end="", flush=True)
            text = "".join(pieces)
        else:
            resp = client.post(CHAT, json=payload, headers=headers)
            print(f"[chat] HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(resp.text[:600])
                return 1
            data = resp.json()
            print(json.dumps({k: data.get(k) for k in ("id", "object", "model", "degradations")},
                             ensure_ascii=False))
            text = data["choices"][0]["message"]["content"]
    print(f"[chat] 回复 {len(text)} 字，耗时 {time.time() - started:.1f}s")
    print(f"[chat] content: {text[:400]}")
    print("[chat] 请把上游实际 SSE 形态回填 docs/UPSTREAM.md U-12（含原始事件样本，脱敏）")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_signin = sub.add_parser("signin", help="账号登录取 token（免费，走轮换出口）")
    p_signin.add_argument("--account", default="")
    p_signin.set_defaults(func=cmd_signin)

    for name, fn, help_text in (("dryrun", cmd_dryrun, "零成本预演（不触上游）"),
                                ("run", cmd_run, "真实创建（消耗 1 次额度）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--kind", choices=("t2v", "i2v"), default="t2v")
        p.add_argument("--image", default=None, help="i2v 首帧图 URL（默认用上游样例图）")
        p.add_argument("--base", default="http://127.0.0.1:8400")
        p.add_argument("--interval", type=float, default=8.0)
        p.add_argument("--timeout", type=float, default=600.0)
        p.add_argument("--out", default=str(REPO_ROOT / "var" / "live"))
        p.set_defaults(func=fn)

    p_chat = sub.add_parser("chat", help="真实一发 t2t 对话（免费，不耗视频额度）")
    p_chat.add_argument("--prompt", default=PROMPT_CHAT)
    p_chat.add_argument("--model", default="", help="默认 qwen3.7-plus（也可用 /v1/models 里注册的任一 chat 模型）")
    p_chat.add_argument("--stream", action="store_true", help="走流式（观察 SSE 增量）")
    p_chat.add_argument("--key", default="", help="调用方凭据 = qwen access_token（见 INTERFACE §1；可用 scripts/make_jwt.py 造）")
    p_chat.add_argument("--base", default="http://127.0.0.1:8400")
    p_chat.set_defaults(func=cmd_chat)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
