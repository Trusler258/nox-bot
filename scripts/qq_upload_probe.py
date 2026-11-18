"""单测官方富媒体上传：只上传、不发消息，验证 40093007 是否消失。

用法：python scripts/qq_upload_probe.py <group_openid> <图片路径>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.adapters.qq_official import QQOfficialAdapter  # noqa: E402
from kernel.obs.logger import setup_logging               # noqa: E402


async def main() -> int:
    setup_logging("INFO", None, console_format="text")
    if len(sys.argv) < 3:
        print("用法: qq_upload_probe.py <group_openid> <图片路径>")
        return 2
    openid, img = sys.argv[1], sys.argv[2]
    secrets = {}
    for line in (ROOT / "config" / "secrets.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            secrets[k.strip()] = v.strip()

    a = QQOfficialAdapter(secrets["QQ_APPID"], secrets["QQ_SECRET"])
    token = await a._get_token()
    try:
        info = await a._upload_image(token, openid, img, "group")
    except Exception as e:
        print(f"[结论] FAIL  {e}")
        return 1
    print(f"[结论] PASS  file_info 长度 {len(info)}  预览 {info[:40]}…")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
