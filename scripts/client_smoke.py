#!/usr/bin/env python3
"""qwen-service 客户端冒烟脚本 —— 把线上服务当**黑盒**，走完整方舟契约流程。

与 `live_smoke.py` 的分工：那个驱动**上游侧**（signin/翻译层），本脚本只走**对外契约**
（`GET /v1/models` → dry-run → 创建 → 轮询 → 下载产物），可对任何部署实例使用。

零第三方依赖（纯标准库），任意 python3.9+ 直接跑。

⚠️ 默认**绕过环境代理**（`ProxyHandler({})`）：实测工作站的本机代理会**静默吞掉带
`Authorization` 的 POST**（GET 与无鉴权请求都能过、服务器两侧零日志、挂起 30s～90s）。
确需走代理时加 `--use-env-proxy`。

用法：
  # 1) 零消耗探测：/v1/models + dry-run（不落任务、不耗额度）
  python3 scripts/client_smoke.py --base-url https://<host> --key <KEY> --mode probe

  # 2) 真实出片（**消耗 1 次额度**）：创建 → 轮询 → 下载
  python3 scripts/client_smoke.py --base-url https://<host> --key <KEY> --mode run \
      --prompt "一只橘猫在草地上追蝴蝶，电影感" --ratio 16:9 --out out.mp4

Key 也可放环境变量 `QWEN_API_KEY`。真实域名/Key 一律走参数，**别写进本文件**
（本仓是公开仓，凭据门禁见 `tests/test_public_surface.py`）。

退出码：0 成功；1 流程失败；2 用法错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

TERMINAL = {"succeeded", "failed", "expired"}


def _opener(use_env_proxy: bool) -> urllib.request.OpenerDirector:
    if use_env_proxy:
        return urllib.request.build_opener()
    # 空 ProxyHandler = 无视 http_proxy/https_proxy 环境变量，直连
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(opener, method, url, *, key=None, body=None, headers=None, timeout=60):
    hdrs = {"Accept": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def step_models(opener, base, timeout) -> bool:
    log("① GET /v1/models（能力探测，免 Key）")
    code, data = request(opener, "GET", f"{base}/v1/models", timeout=timeout)
    if code != 200:
        log(f"   ❌ HTTP {code}: {json.dumps(data, ensure_ascii=False)[:300]}")
        return False
    models = [m.get("id") for m in data.get("data", [])]
    log(f"   ✅ 200，models={models}")
    return True


def step_dryrun(opener, base, key, payload, timeout) -> bool:
    log("② dry-run（X-Avm-Dry-Run: 1，翻译层自检，零成本）")
    code, data = request(opener, "POST", f"{base}/api/v3/contents/generations/tasks",
                         key=key, body=payload, headers={"X-Avm-Dry-Run": "1"}, timeout=timeout)
    if code != 200 or not data.get("dry_run"):
        log(f"   ❌ HTTP {code}: {json.dumps(data, ensure_ascii=False)[:300]}")
        return False
    up = data.get("upstream", {})
    hdr = up.get("headers", {})
    ok_shape = hdr.get("version") == "0.2.0" and "token=" in str(hdr.get("Cookie", ""))
    log(f"   ✅ 翻译形状：{up.get('method')} {up.get('url')} · version 头/Cookie {'✅' if ok_shape else '⚠️ 异常'}")
    return True


def step_run(opener, base, key, payload, timeout, poll_interval, out: Path) -> int:
    log("③ 创建任务（真实，耗 1 次额度）")
    code, data = request(opener, "POST", f"{base}/api/v3/contents/generations/tasks",
                         key=key, body=payload, timeout=timeout)
    if code != 200 or "id" not in data:
        log(f"   ❌ HTTP {code}: {json.dumps(data, ensure_ascii=False)[:300]}")
        return 1
    task_id = data["id"]
    log(f"   ✅ 受理 id={task_id}")

    log("④ 轮询到终态")
    deadline = time.monotonic() + timeout
    last = None
    started = time.monotonic()
    while time.monotonic() < deadline:
        code, data = request(opener, "GET", f"{base}/api/v3/contents/generations/tasks/{task_id}",
                             key=key, timeout=timeout)
        if code != 200:
            log(f"   ⚠️ 查询 HTTP {code}: {json.dumps(data, ensure_ascii=False)[:200]}")
        else:
            st = data.get("status")
            if st != last:
                log(f"   status={st}（已 {time.monotonic() - started:.0f}s）")
                last = st
            if st in TERMINAL:
                break
        time.sleep(poll_interval)
    else:
        log(f"   ❌ 超时（{timeout}s 内未到终态）")
        return 1

    if last != "succeeded":
        log(f"   ❌ 终态={last}: {json.dumps(data, ensure_ascii=False)[:400]}")
        return 1
    log(f"   ✅ 出片耗时 {time.monotonic() - started:.0f}s")

    video_url = (data.get("content") or {}).get("video_url")
    if not video_url:
        log("   ❌ succeeded 但没有 video_url")
        return 1
    log("⑤ 下载产物")
    req = urllib.request.Request(video_url, method="GET")
    with opener.open(req, timeout=300) as resp, open(out, "wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)
    size = out.stat().st_size
    log(f"   ✅ {out}（{size / 1e6:.1f} MB）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="服务入口，如 https://<host>（真实域名走参数，别入库）")
    ap.add_argument("--key", default=os.environ.get("QWEN_API_KEY"), help="API Key（或环境变量 QWEN_API_KEY）")
    ap.add_argument("--mode", choices=["probe", "run"], default="probe",
                    help="probe=零消耗探测（默认）；run=真实出片，耗 1 次额度")
    ap.add_argument("--model", default="qwen/video")
    ap.add_argument("--prompt", default="一只橘猫在阳光洒落的草地上追蝴蝶，镜头缓慢推进，电影感")
    ap.add_argument("--ratio", default="16:9", help="1:1/3:4/4:3/16:9/9:16，枚举外落 1:1")
    ap.add_argument("--duration", type=int, default=5, help="上游固定 ~5s：<5 会被 400，>5 吸附到 5")
    ap.add_argument("--timeout", type=int, default=900, help="轮询上限秒（出片实测 93~343s）")
    ap.add_argument("--poll-interval", type=int, default=10)
    ap.add_argument("--out", default="", help="产物保存路径（默认 qwen-video-<id>.mp4）")
    ap.add_argument("--use-env-proxy", action="store_true", help="走环境代理（默认绕过，见文件头说明）")
    args = ap.parse_args()

    if not args.key:
        print("缺少 Key：--key 或环境变量 QWEN_API_KEY", file=sys.stderr)
        return 2
    base = args.base_url.rstrip("/")
    payload = {"model": args.model,
               "content": [{"type": "text", "text": args.prompt}],
               "ratio": args.ratio, "duration": args.duration}
    opener = _opener(args.use_env_proxy)

    ok = step_models(opener, base, args.timeout) and step_dryrun(opener, base, args.key, payload, args.timeout)
    if not ok:
        return 1
    if args.mode == "probe":
        log("probe 完成（零消耗）")
        return 0
    out_path = Path(args.out) if args.out else Path(f"qwen-video-{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4")
    return step_run(opener, base, args.key, payload, args.timeout, args.poll_interval, out_path)


if __name__ == "__main__":
    sys.exit(main())
