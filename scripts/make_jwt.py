#!/usr/bin/env python3
"""造一张 JWT 门卡（qwen access token **形状**）。

用途：本地实测与 CI 冒烟 —— 服务侧只校验**形状 + `type=access_token` + `exp` 未过期**
（**不验签**，签名校验只有上游能做），所以这里不需要真凭据，也不需要上游配合。

用法：
    python scripts/make_jwt.py [TTL_SECONDS]     # 默认 3600
"""
from __future__ import annotations

import base64
import json
import sys
import time


def make_jwt(payload: dict) -> str:
    """最小 JWT 形状：`header.payload.signature`（签名段固定占位，服务不看）。"""
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return (f"{b64({'alg': 'HS256', 'kid': 'legacy', 'typ': 'JWT'})}."
            f"{b64(payload)}.sig")


def access_token(ttl: float = 3600.0) -> str:
    now = int(time.time())
    return make_jwt({"type": "access_token", "exp": int(now + ttl), "iat": now})


if __name__ == "__main__":
    print(access_token(float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0))
