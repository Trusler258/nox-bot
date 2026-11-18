"""真实 kill -9 的崩溃恢复验证（M0 验收项）。

流程：
  1) 父进程入队一条任务
  2) 起子进程 A 领任务后卡住（模拟真实工作被 -9 打断）
  3) 父进程 kill -9 子进程 A
  4) 起子进程 B：应回收过期租约，把任务跑完
  5) 校验：state=done，attempts=2

用法：python scripts/crash_recovery_demo.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "crash_demo.db"
JOB = "crash.demo"


def _parts(db: Path, lease: float):
    from kernel.scheduler.runner import Scheduler
    from kernel.storage.db import JobLedger, Store

    ledger = JobLedger(Store(db))
    return ledger, Scheduler(ledger, tick=0.2, lease_seconds=lease)


def worker(stuck: bool) -> int:
    """子进程：领任务并执行。stuck=True 时卡住不动，等被 kill。"""
    from kernel.obs.logger import setup_logging

    setup_logging("INFO", None, json_mode=False)
    ledger, sched = _parts(DB, lease=2.0)

    async def handler(payload: dict) -> None:
        print(f"[worker] 领到任务 {payload}，开始干活", flush=True)
        if stuck:
            await asyncio.sleep(600)          # 永远干不完，等待被 kill -9
        print("[worker] 干完了", flush=True)

    sched.register(JOB, handler)

    async def main() -> None:
        sched.ledger.recover_expired()
        for _ in range(100):                  # 最多等 10 秒
            if await sched.run_once():
                return
            await asyncio.sleep(0.1)
        print("[worker] 没有可领的任务", flush=True)

    asyncio.run(main())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", choices=["stuck", "quick"], help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        return worker(stuck=(args.child == "stuck"))

    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()

    ledger, _ = _parts(DB, lease=2.0)
    job_id = ledger.enqueue(JOB, {"msg": "hello"})
    print(f"[parent] 入队 {job_id}，状态 {ledger.stats()}")

    py = sys.executable
    child = subprocess.Popen([py, str(Path(__file__)), "--child", "stuck"])
    deadline = time.time() + 15
    claimed = False
    while time.time() < deadline:
        row = ledger.store.one("SELECT state FROM job WHERE id=?", (job_id,))
        if row and row["state"] == "running":
            claimed = True
            break
        time.sleep(0.1)
    if not claimed:
        child.kill()
        print("[parent] 子进程没能领到任务，测试无效")
        return 1

    print("[parent] 子进程已领任务，执行 kill -9（模拟进程被强杀）")
    child.kill()                              # Windows: TerminateProcess；POSIX: SIGKILL
    child.wait(timeout=10)

    row = ledger.store.one("SELECT state, attempts FROM job WHERE id=?", (job_id,))
    print(f"[parent] 强杀后：state={row['state']} attempts={row['attempts']}")

    print("[parent] 等租约过期（2 秒）…")
    time.sleep(2.5)

    subprocess.run([py, str(Path(__file__)), "--child", "quick"], check=False)

    row = ledger.store.one(
        "SELECT state, attempts, result FROM job WHERE id=?", (job_id,))
    print(f"[parent] 恢复后：state={row['state']} attempts={row['attempts']} result={row['result']}")
    ok = row["state"] == "done" and row["attempts"] == 2
    print("[parent] 结论：" + ("PASS —— 任务被回收并跑完" if ok else "FAIL"))
    try:
        DB.unlink(missing_ok=True)
    except OSError:
        pass                                   # 某些环境拦截删除，不影响结论
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
