"""富媒体图片上传 + 发送 探针：完整走一遍，打印每步响应。

用法：python scripts/qq_media_probe.py <group_openid> [图片路径]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

API = "https://api.sgroup.qq.com"


def load_env() -> dict:
    env = {}
    f = Path("/opt/huanmeng/v3-test/config/secrets.env")
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def post(url: str, payload: dict, token: str, timeout: int = 60):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"QQBot {token}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_token(env: dict) -> str:
    req = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=json.dumps({"appId": env["QQ_APPID"],
                         "clientSecret": env["QQ_SECRET"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["access_token"]


def main() -> int:
    group = sys.argv[1] if len(sys.argv) > 1 else "6F879C84D5478369F4F8FC799C394648"
    img = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if img is None:
        cards = ROOT / "data" / "store" / "cards"
        img = max(cards.glob("*.png"), key=lambda p: p.stat().st_mtime)
    raw = img.read_bytes()
    print(f"[图片] {img.name}  {len(raw)} bytes")

    env = load_env()
    token = get_token(env)
    print(f"[token] 已获取")

    # ── 1. JSON + base64 上传 ────────────────────────────
    payload = {
        "file_type": 1,
        "file_data": base64.b64encode(raw).decode("ascii"),
        "file_name": img.name,
        "srv_send_msg": False,
    }
    st, body = post(f"{API}/v2/groups/{group}/files", payload, token)
    print(f"[上传] HTTP {st}  返回: {str(body)[:400]}")

    info = None
    if isinstance(body, dict):
        info = body.get("file_info") or (body.get("data") or {}).get("file_info")
    if not info:
        print("[结论] 上传没拿到 file_info")
        return 1
    print(f"[file_info] 长度={len(info)}  头 60 字符: {info[:60]}")

    # ── 2. 两种发送体对比 ────────────────────────────────
    variant = os.environ.get("VARIANT", "A")
    if variant == "A":
        msg = {"msg_type": 7, "media": {"file_info": info}, "msg_seq": 1}
    else:                                   # B：显式给空 content
        msg = {"msg_type": 7, "content": "", "media": {"file_info": info},
               "msg_seq": 1}
    print(f"[发送] variant={variant}  body={json.dumps({k: v for k, v in msg.items() if k != 'media'})}")
    st, body = post(f"{API}/v2/groups/{group}/messages", msg, token)
    print(f"[发送] HTTP {st}  返回: {str(body)[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
