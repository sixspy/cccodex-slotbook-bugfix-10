"""过期排序：快过期不得沉底的失败测与双人续期刷表争用失败测。

先落夹具 fixtures/holds_expiry_skew.json（按 created_at 看像有序的假绿），
再落以下两测；修前必须双红，见 docs/过期排序说明.md。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
import db as dbmod  # noqa: E402
from db import init_db  # noqa: E402

SKEW = ROOT / "fixtures" / "holds_expiry_skew.json"
# 过期时刻升序，同刻按 id 升序；种子 P-HOLD1 过期 2026-12-31 必垫底
EXPECTED_ORDER = [
    "P-SKEW-SOON",   # 2026-11-23
    "P-SKEW-TIE-A",  # 2026-11-25T10（同刻 id 小）
    "P-SKEW-TIE-B",  # 2026-11-25T10
    "P-SKEW-MID",    # 2026-11-28
    "P-SKEW-FAR2",   # 2026-12-08
    "P-SKEW-FAR1",   # 2026-12-10
    "P-HOLD1",       # 2026-12-31（种子）
]


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_skew():
    rows = json.loads(SKEW.read_text(encoding="utf-8"))
    with dbmod.connect() as conn:
        for h in rows:
            slot = conn.execute(
                "SELECT id FROM slots WHERE code=?", (h["slot_code"],)
            ).fetchone()
            assert slot, h["slot_code"]
            conn.execute(
                """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    slot["id"],
                    h["party_key"],
                    h["seats"],
                    h["status"],
                    h["expire_at"],
                    h["created_at"],
                ),
            )
        conn.commit()
    return rows


def _hold_by_party(items, party):
    return next(x for x in items if x["party_key"] == party)


def test_fixture_is_false_green_under_created_at():
    """夹具本身：旧 created_at 序把最快过期的 P-SKEW-SOON 压到最后。"""
    rows = json.loads(SKEW.read_text(encoding="utf-8"))
    legacy = sorted(rows, key=lambda r: (r["created_at"], r["party_key"]))
    assert legacy[-1]["party_key"] == "P-SKEW-SOON"
    legacy_expire = [r["expire_at"] for r in legacy]
    assert legacy_expire != sorted(legacy_expire)  # 假绿：看起来有序，实则倒挂
    by_expiry = sorted(rows, key=lambda r: (r["expire_at"], r["party_key"]))
    assert by_expiry[0]["party_key"] == "P-SKEW-SOON"


def test_holds_list_orders_by_expire_at_and_materializes():
    """排序错乱失败测：列表、落库序键、对账页、审计页须同态按过期时刻。"""
    with TestClient(app) as c:
        h = _login(c)
        _seed_skew()

        r = c.get("/api/holds", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]

        # 1) 占位页列表序：按过期时刻，快过期不得沉底
        assert [x["party_key"] for x in items] == EXPECTED_ORDER
        assert body["sort"] == "expire_at"
        assert str(body["scheme"]).startswith("甲")
        # 过期时刻就是过期时刻，同刻按 id 稳定
        expires = [x["expire_at"] for x in items]
        assert expires == sorted(expires)
        assert _hold_by_party(items, "P-SKEW-TIE-A")["id"] < _hold_by_party(
            items, "P-SKEW-TIE-B"
        )["id"]

        # 2) 序键稳定落库可核
        seqs = [x["order_seq"] for x in items]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        with dbmod.connect() as conn:
            dbrows = conn.execute(
                "SELECT hold_id,order_seq,expire_at FROM hold_order ORDER BY order_seq"
            ).fetchall()
        assert [r["hold_id"] for r in dbrows] == [x["id"] for x in items]
        assert [r["expire_at"] for r in dbrows] == expires

        # 3) 对账页同态
        recon = c.get("/api/reconcile", headers=h).json()
        assert [x["id"] for x in recon["holds"]] == [x["id"] for x in items]
        assert [x["expire_at"] for x in recon["holds"]] == expires
        assert recon["order_ok"] is True

        # 4) 审计页同态
        audit = c.get("/api/audit", headers=h).json()
        assert [x["id"] for x in audit["holds_order"]] == [x["id"] for x in items]
        assert [x["expire_at"] for x in audit["holds_order"]] == expires


def test_reorder_failure_rolls_list_back_to_previous_order():
    """甲路径失败：物化失败则列表滚回上一次已物化的旧序，并留 holds_reorder_fail。"""
    with TestClient(app) as c:
        h = _login(c)
        _seed_skew()
        # 先成功物化一次，形成“旧序”
        first = c.get("/api/holds", headers=h).json()
        assert first["fresh"] is True
        old = [(x["id"], x["order_seq"], x["expire_at"]) for x in first["items"]]

        import hold_svc

        def boom(conn):
            raise RuntimeError("forced reorder failure")

        original = hold_svc.materialize_hold_order
        hold_svc.materialize_hold_order = boom
        try:
            second = c.get("/api/holds", headers=h).json()
        finally:
            hold_svc.materialize_hold_order = original

        assert second["fresh"] is False
        assert second["contended"] == []
        new = [(x["id"], x["order_seq"], x["expire_at"]) for x in second["items"]]
        assert new == old  # 滚回旧序，序零改

        audits = c.get("/api/audit", headers=h, params={"limit": 500}).json()["items"]
        assert any(a["action"] == "holds_reorder_fail" for a in audits)


def test_two_person_renew_and_refresh_contend_on_slot_lock():
    """双人续期刷表争用失败测：后到只留争用痕，不改过期时刻与序键。"""
    with TestClient(app) as c:
        h_admin = _login(c, "admin")
        h_clerk = _login(c, "clerk")
        _seed_skew()

        items = c.get("/api/holds", headers=h_clerk).json()["items"]
        soon = _hold_by_party(items, "P-SKEW-SOON")
        before_expire = soon["expire_at"]
        before_seq = soon["order_seq"]

        # 第一人续期窗口：显式持住 S-EVE-03 档锁
        r = c.post(
            "/api/locks/acquire",
            headers=h_admin,
            json={"lock_key": "slot:S-EVE-03"},
        )
        assert r.status_code == 200, r.text

        # 第二人续期：后到 → 409，只留争用痕
        r = c.post(
            "/api/holds/renew",
            headers=h_clerk,
            json={"hold_id": soon["id"], "minutes": 90},
        )
        assert r.status_code == 409, r.text
        with dbmod.connect() as conn:
            row = conn.execute(
                "SELECT expire_at FROM holds WHERE id=?", (soon["id"],)
            ).fetchone()
        assert row["expire_at"] == before_expire

        # 第二人刷列表：同样抢档锁，争用时滚回旧序，不重排
        r = c.get("/api/holds", headers=h_clerk)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["fresh"] is False
        assert "S-EVE-03" in body["contended"]
        after_refresh = _hold_by_party(body["items"], "P-SKEW-SOON")
        assert after_refresh["expire_at"] == before_expire
        assert after_refresh["order_seq"] == before_seq

        # 争用痕至少两条：续期 + 刷表；且后到者无续期审计
        cont = c.get("/api/contention", headers=h_clerk).json()["items"]
        mine = [
            x
            for x in cont
            if x["lock_key"] == "slot:S-EVE-03"
            and x["actor"] == "clerk"
            and x["action"] == "争用"
        ]
        assert len(mine) >= 2
        audits = c.get("/api/audit", headers=h_clerk, params={"limit": 500}).json()[
            "items"
        ]
        assert not any(
            a["action"] == "hold_renew" and a["actor"] == "clerk" for a in audits
        )

        # 释放后第二人续期成功，过期时刻推进，列表仍按过期时刻同态
        r = c.post(
            "/api/locks/release",
            headers=h_admin,
            json={"lock_key": "slot:S-EVE-03"},
        )
        assert r.status_code == 200, r.text
        r = c.post(
            "/api/holds/renew",
            headers=h_clerk,
            json={"hold_id": soon["id"], "minutes": 90},
        )
        assert r.status_code == 200, r.text
        assert r.json()["expire_at"] > before_expire

        body2 = c.get("/api/holds", headers=h_clerk).json()
        expires = [x["expire_at"] for x in body2["items"]]
        assert expires == sorted(expires)
        assert body2["sort"] == "expire_at"

        audits = c.get("/api/audit", headers=h_clerk, params={"limit": 500}).json()[
            "items"
        ]
        assert any(
            a["action"] == "hold_renew" and a["actor"] == "clerk" for a in audits
        )
        recon = c.get("/api/reconcile", headers=h_clerk).json()
        assert [x["expire_at"] for x in recon["holds"]] == expires
        assert recon["order_ok"] is True
