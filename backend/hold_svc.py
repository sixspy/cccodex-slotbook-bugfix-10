"""占位：容量校验、并发锁、候补分流。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import (
    HOLD_ORDER_SCHEME,
    connect,
    materialize_hold_order,
    select_ordered_holds,
    write_audit,
)
from locks import release, require_lock, try_acquire

router = APIRouter(tags=["holds"])


class HoldIn(BaseModel):
    slot_code: str
    party_key: str
    seats: int = Field(ge=1, le=50)


class RenewIn(BaseModel):
    hold_id: int
    minutes: int = Field(default=30, ge=1, le=1440)


def _reorder_or_rollback(conn, user: str) -> bool:
    """甲路径：读前同事务重排物化序键；失败则回滚，列表滚回旧序。"""
    try:
        materialize_hold_order(conn)
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — 物化失败必须滚回旧序并留痕
        conn.rollback()
        write_audit(conn, user, "holds_reorder_fail", "hold_order", {"error": str(exc)})
        conn.commit()
        return False


@router.get("/api/holds")
def list_holds(authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        # 甲路径：先抢档锁再同事务重排并物化，列表按过期时刻（ORDER BY expire_at, id）
        lock_keys = [
            f"slot:{r['code']}"
            for r in conn.execute("SELECT code FROM slots ORDER BY code").fetchall()
        ]
        contended = []
        held = []
        for key in lock_keys:
            if try_acquire(conn, key, user):
                held.append(key)
            else:
                contended.append(key.removeprefix("slot:"))
        # 档锁先落库：随后重排失败回滚时，已抢到的锁不能被一并回滚掉
        conn.commit()
        try:
            fresh = _reorder_or_rollback(conn, user) if not contended else False
        finally:
            for key in held:
                release(conn, key, user)
            conn.commit()
        rows = select_ordered_holds(conn)
        return {
            "items": [dict(r) for r in rows],
            "sort": "expire_at",
            "scheme": HOLD_ORDER_SCHEME,
            "fresh": fresh,
            "contended": contended,
        }


@router.post("/api/holds/renew")
def renew_hold(body: RenewIn, authorization: AuthHeader = None):
    """续期入口：抢档锁后推进过期时刻，同事务重排物化；后到只留争用痕。"""
    user = require_writer(authorization)
    with connect() as conn:
        probe = conn.execute("SELECT slot_id FROM holds WHERE id=?", (body.hold_id,)).fetchone()
        if not probe:
            raise HTTPException(404, "占位不存在")
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (probe["slot_id"],)).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"
        require_lock(conn, lock_key, user)
        # 先落锁，重排失败回滚时不会把已抢到的档锁一并回滚
        conn.commit()
        try:
            hold = conn.execute("SELECT * FROM holds WHERE id=?", (body.hold_id,)).fetchone()
            if hold["status"] != "占位中":
                raise HTTPException(409, "占位非占位中，禁止续期")
            now = datetime.now()
            if hold["expire_at"] < now.isoformat(timespec="seconds"):
                raise HTTPException(409, "占位已过期，禁止续期")
            # 续期在现有过期时刻之上顺延；已过期前的不动，避免“续期反而提前”
            base = max(now, datetime.fromisoformat(hold["expire_at"]))
            expire = (base + timedelta(minutes=body.minutes)).isoformat(timespec="seconds")
            conn.execute("UPDATE holds SET expire_at=? WHERE id=?", (expire, hold["id"]))
            try:
                materialize_hold_order(conn)
            except Exception as exc:  # noqa: BLE001 — 重排失败整笔续期回滚
                conn.rollback()
                write_audit(
                    conn, user, "holds_reorder_fail", "hold_order", {"error": str(exc)}
                )
                conn.commit()
                raise HTTPException(500, "重排失败，续期已回滚") from exc
            write_audit(
                conn,
                user,
                "hold_renew",
                hold["party_key"],
                {"hold_id": hold["id"], "expire_at": expire, "minutes": body.minutes},
            )
            conn.commit()
            return {
                "ok": True,
                "hold_id": hold["id"],
                "expire_at": expire,
                "sort": "expire_at",
                "scheme": HOLD_ORDER_SCHEME,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/holds")
def create_hold(body: HoldIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"slot:{body.slot_code}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        # 先落锁：争用痕已在 try_acquire 内提交，这里确保抢到的锁不随后续回滚丢失
        conn.commit()
        try:
            slot = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.slot_code,)
            ).fetchone()
            if not slot:
                raise HTTPException(404, "档期不存在")
            if slot["status"] != "开放":
                raise HTTPException(409, "档期未开放，禁止占位")
            remain = int(slot["capacity"]) - int(slot["booked"])
            pending = conn.execute(
                "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
                (slot["id"],),
            ).fetchone()["s"]
            free = remain - int(pending)
            now = datetime.now().isoformat(timespec="seconds")
            expire = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
            if body.seats > free:
                conn.execute(
                    """INSERT INTO waitlist(slot_id,party_key,seats,reason,status,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (slot["id"], body.party_key, body.seats, "容量不足", "候补中", now),
                )
                write_audit(
                    conn,
                    user,
                    "hold_to_waitlist",
                    body.party_key,
                    {"slot": body.slot_code, "free": free, "want": body.seats},
                )
                conn.commit()
                return {"disposition": "候补", "free": free}
            cur = conn.execute(
                """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (slot["id"], body.party_key, body.seats, "占位中", expire, now),
            )
            materialize_hold_order(conn)
            write_audit(
                conn,
                user,
                "hold_create",
                body.party_key,
                {"slot": body.slot_code, "seats": body.seats, "hold_id": cur.lastrowid},
            )
            conn.commit()
            return {"disposition": "占位", "hold_id": cur.lastrowid, "expire_at": expire}
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
