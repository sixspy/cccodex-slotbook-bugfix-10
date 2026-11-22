"""对账：档期 booked 与正式册 seats 同源校验。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import HOLD_ORDER_SCHEME, connect, select_ordered_holds

router = APIRouter(tags=["reconcile"])


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = []
        for s in slots:
            booked_sum = conn.execute(
                """SELECT COALESCE(SUM(seats),0) s FROM bookings
                   WHERE slot_id=? AND status='已确认'""",
                (s["id"],),
            ).fetchone()["s"]
            ok = int(booked_sum) == int(s["booked"])
            items.append(
                {
                    "slot_code": s["code"],
                    "slot_booked": s["booked"],
                    "booking_seats": booked_sum,
                    "generation": s["generation"],
                    "ok": ok,
                    "status": s["status"],
                }
            )
        return {
            "items": items,
            "all_ok": all(i["ok"] for i in items),
            "holds": [dict(r) for r in select_ordered_holds(conn)],
            "order_ok": _order_ok(conn),
            "sort": "expire_at",
            "scheme": HOLD_ORDER_SCHEME,
        }


def _order_ok(conn) -> bool:
    rows = select_ordered_holds(conn)
    expires = [r["expire_at"] for r in rows]
    ids = [r["id"] for r in rows]
    return expires == sorted(expires) and ids == [
        r["id"] for r in conn.execute(
            "SELECT id FROM holds ORDER BY expire_at ASC, id ASC LIMIT 200"
        ).fetchall()
    ]


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        booked_sum = conn.execute(
            """SELECT COALESCE(SUM(seats),0) s FROM bookings
               WHERE slot_id=? AND status='已确认'""",
            (s["id"],),
        ).fetchone()["s"]
        holds = conn.execute(
            "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
            (s["id"],),
        ).fetchone()["c"]
        return {
            "slot_code": code,
            "slot_booked": s["booked"],
            "booking_seats": booked_sum,
            "open_holds": holds,
            "ok": int(booked_sum) == int(s["booked"]),
            "generation": s["generation"],
        }
