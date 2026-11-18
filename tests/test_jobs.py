"""任务账本：幂等、租约回收（模拟 kill -9）、重试上限。"""
import time

from kernel.storage.db import JobLedger, Store


def make(tmp_path):
    store = Store(tmp_path / "t.db")
    return JobLedger(store)


def test_idem_key_dedupe(tmp_path):
    led = make(tmp_path)
    a = led.enqueue("scan", {"p": 1}, idem_key="scan:1")
    b = led.enqueue("scan", {"p": 1}, idem_key="scan:1")
    assert a == b
    assert led.stats() == {"pending": 1}


def test_claim_and_complete(tmp_path):
    led = make(tmp_path)
    jid = led.enqueue("scan")
    job = led.claim("w1", 30)
    assert job and job["id"] == jid and job["state"] == "running"
    assert led.claim("w2", 30) is None          # 已被占住
    led.complete(jid)
    assert led.stats() == {"done": 1}


def test_lease_expiry_recovery(tmp_path):
    """模拟 kill -9：进程没来得及 complete，租约到期后必须能重跑。"""
    led = make(tmp_path)
    jid = led.enqueue("scan")
    led.claim("w1", 0.01)                        # 极短租约
    time.sleep(0.05)
    assert led.recover_expired() == 1
    job = led.claim("w2", 30)
    assert job and job["id"] == jid
    assert job["attempts"] == 2                  # 第三次尝试


def test_retry_limit_goes_dead(tmp_path):
    led = make(tmp_path)
    jid = led.enqueue("scan", max_attempts=1)
    led.claim("w1", 30)
    led.fail(jid, "boom", retry=True)
    assert led.stats() == {"dead": 1}


def test_retry_backoff_requeues(tmp_path):
    led = make(tmp_path)
    jid = led.enqueue("scan", max_attempts=3)
    led.claim("w1", 30)
    led.fail(jid, "boom")
    row = led.store.one("SELECT state, run_after FROM job WHERE id=?", (jid,))
    assert row["state"] == "pending"
    assert row["run_after"] > time.time()        # 退避后才可再取
