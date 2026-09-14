"""数据写入/修正服务：合成播种与人工事件修正（保留来源与版本轨迹）。"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from . import analytics
from .db import get_conn
from .synth import build_profiles, generate_samples


def reseed(seed: int = 7) -> dict[str, int]:
    """删除旧数据并写入两批合成数据（原始采样 + auto 事件）。幂等。"""
    profiles = build_profiles()
    counts = {"batches": 0, "samples": 0, "events": 0}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE comparison_reports, event_candidates, event_revisions, events, samples, batches RESTART IDENTITY"
        )
        for pi, prof in enumerate(profiles):
            cur.execute(
                "INSERT INTO batches(name, variety, recipe, charge_g, probe_position, probe_offset_c, note)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    prof.name,
                    prof.variety,
                    prof.recipe,
                    prof.charge_g,
                    prof.probe_position,
                    prof.probe_offset_c,
                    "合成数据（不连接真实烘焙机），含非均匀采样、测量噪声与探针失联缺测。"
                    + (prof.seed_note or ""),
                ),
            )
            bid = cur.fetchone()["id"]
            counts["batches"] += 1

            rows = generate_samples(prof, seed=seed + pi)
            cur.executemany(
                "INSERT INTO samples(batch_id, t_s, bean_temp_c, env_temp_c, is_missing)"
                " VALUES (%s,%s,%s,%s,%s)",
                [
                    (bid, r["t_s"], r["bean_temp_c"], r["env_temp_c"], r["is_missing"])
                    for r in rows
                ],
            )
            counts["samples"] += len(rows)

            for kind, ts in prof.true_events.items():
                if kind in prof.missing_seeded_events:
                    continue  # 故意不创建该“已确认事件”，模拟尚未标记
                cur.execute(
                    "INSERT INTO events(batch_id, kind, t_s, source, created_by, note)"
                    " VALUES (%s,%s,%s,'auto','generator',%s)",
                    (bid, kind, ts, "合成生成器按真值事件时间播种，允许人工修正"),
                )
                counts["events"] += 1
            for d in prof.dampers:
                cur.execute(
                    "INSERT INTO events(batch_id, kind, t_s, value, source, created_by)"
                    " VALUES (%s,'damper',%s,%s,'auto','generator')",
                    (bid, d.t_s, d.value),
                )
                counts["events"] += 1
            for g in prof.gas:
                cur.execute(
                    "INSERT INTO events(batch_id, kind, t_s, value, source, created_by)"
                    " VALUES (%s,'gas',%s,%s,'auto','generator')",
                    (bid, g.t_s, g.value),
                )
                counts["events"] += 1
        conn.commit()
    return counts


def list_batches() -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT b.id, b.name, b.variety, b.recipe, b.charge_g, b.probe_position,"
            " b.probe_offset_c, b.started_at, b.note,"
            " (SELECT count(*) FROM samples s WHERE s.batch_id=b.id) AS n_samples,"
            " (SELECT count(*) FROM samples s WHERE s.batch_id=b.id AND s.is_missing) AS n_missing,"
            " (SELECT min(t_s) FROM samples s WHERE s.batch_id=b.id) AS t_min,"
            " (SELECT max(t_s) FROM samples s WHERE s.batch_id=b.id) AS t_max"
            " FROM batches b ORDER BY b.id"
        )
        rows = cur.fetchall()
    for r in rows:
        r["started_at"] = r["started_at"].isoformat()
    return rows


def get_batch(bid: int) -> dict[str, Any] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM batches WHERE id=%s", (bid,))
        row = cur.fetchone()
    if row:
        row["started_at"] = row["started_at"].isoformat()
        row["created_at"] = row["created_at"].isoformat()
    return row


def load_samples(bid: int) -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t_s, bean_temp_c, env_temp_c, is_missing FROM samples"
            " WHERE batch_id=%s ORDER BY t_s",
            (bid,),
        )
        return cur.fetchall()


def load_events(bid: int, include_revoked: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE batch_id=%s"
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY t_s, id"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (bid,))
        rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
        if r.get("revoked_at") is not None:
            r["revoked_at"] = r["revoked_at"].isoformat()
    return rows


def load_revisions(bid: int) -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM event_revisions WHERE batch_id=%s ORDER BY revised_at, id",
            (bid,),
        )
        rows = cur.fetchall()
    for r in rows:
        r["revised_at"] = r["revised_at"].isoformat()
    return rows


def revise_event(
    event_id: int, new_t_s: float, reason: str | None, revised_by: str,
    new_value: float | None = None,
) -> dict[str, Any]:
    """人工修正：旧版本作废保留，新版本 source='manual'，并写审计轨迹。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM events WHERE id=%s FOR UPDATE", (event_id,))
        old = cur.fetchone()
        if old is None:
            raise KeyError(f"event {event_id} not found")
        if old["revoked_at"] is not None:
            raise ValueError("该事件版本已作废，请基于当前有效版本修正")
        if new_t_s < 0:
            raise ValueError("事件时间不能为负")

        cur.execute(
            "INSERT INTO events(batch_id, kind, t_s, value, source, note, created_by)"
            " VALUES (%s,%s,%s,%s,'manual',%s,%s) RETURNING *",
            (
                old["batch_id"],
                old["kind"],
                new_t_s,
                new_value if old["kind"] in ("damper", "gas") else None,
                reason or "人工修正",
                revised_by,
            ),
        )
        new = cur.fetchone()
        cur.execute("UPDATE events SET revoked_at=now() WHERE id=%s", (event_id,))
        cur.execute(
            "INSERT INTO event_revisions(batch_id, event_id, kind, old_t_s, old_source,"
            " new_t_s, new_value, new_source, reason, revised_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',%s,%s)",
            (
                old["batch_id"],
                event_id,
                old["kind"],
                old["t_s"],
                old["source"],
                new_t_s,
                new_value if old["kind"] in ("damper", "gas") else None,
                reason,
                revised_by,
            ),
        )
        conn.commit()
    new["created_at"] = new["created_at"].isoformat()
    return new


def add_manual_event(
    bid: int, kind: str, t_s: float, value: float | None, note: str | None, created_by: str
) -> dict[str, Any]:
    if kind in analytics.THERMAL_KINDS:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM events WHERE batch_id=%s AND kind=%s AND revoked_at IS NULL",
                (bid, kind),
            )
            exists = cur.fetchone()
        if exists:
            raise ValueError(f"{kind} 已存在有效标记，请对其进行修正而非重复创建")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events(batch_id, kind, t_s, value, source, note, created_by)"
            " VALUES (%s,%s,%s,%s,'manual',%s,%s) RETURNING *",
            (bid, kind, t_s, value, note, created_by),
        )
        row = cur.fetchone()
        conn.commit()
    row["created_at"] = row["created_at"].isoformat()
    return row


# ---------------- 候选修订（不改变当前有效事件） ----------------

def list_candidates(bid: int, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM event_candidates WHERE batch_id=%s"
    params: list[Any] = [bid]
    if status:
        sql += " AND status=%s"
        params.append(status)
    sql += " ORDER BY created_at, id"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
        if r.get("decided_at"):
            r["decided_at"] = r["decided_at"].isoformat()
    return rows


def create_candidate(
    bid: int, kind: str, proposed_t_s: float, reason: str | None,
    proposed_by: str, proposed_value: float | None = None,
) -> dict[str, Any]:
    """建立候选修订：仅记录建议，绝不改动 events；等待负责人接受/拒绝。"""
    valid = analytics.THERMAL_KINDS + ("damper", "gas")
    if kind not in valid:
        raise ValueError(f"kind 必须是 {valid} 之一")
    if proposed_t_s < 0:
        raise ValueError("候选时间不能为负")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO event_candidates(batch_id, kind, proposed_t_s, proposed_value,"
            " reason, status, proposed_by) VALUES (%s,%s,%s,%s,%s,'proposed',%s) RETURNING *",
            (bid, kind, proposed_t_s, proposed_value, reason, proposed_by),
        )
        row = cur.fetchone()
        conn.commit()
    row["created_at"] = row["created_at"].isoformat()
    return row


def decide_candidate(cid: int, accept: bool, decided_by: str) -> dict[str, Any]:
    """接受候选 → 落成一次正式人工修正（进入事件版本与审计轨迹）；拒绝 → 仅置状态。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM event_candidates WHERE id=%s FOR UPDATE", (cid,))
        cand = cur.fetchone()
        if cand is None:
            raise KeyError("候选不存在")
        if cand["status"] != "proposed":
            raise ValueError(f"候选已处理（{cand['status']}），不能重复决定")
        if accept:
            cur.execute(
                "SELECT * FROM events WHERE batch_id=%s AND kind=%s AND revoked_at IS NULL"
                " ORDER BY id DESC LIMIT 1",
                (cand["batch_id"], cand["kind"]),
            )
            current = cur.fetchone()
            if current is not None:
                new = revise_event_conn(
                    cur, current["id"], float(cand["proposed_t_s"]),
                    cand.get("reason") or "接受候选修订", decided_by,
                    new_value=cand.get("proposed_value"),
                )
            else:
                cur.execute(
                    "INSERT INTO events(batch_id, kind, t_s, value, source, note, created_by)"
                    " VALUES (%s,%s,%s,%s,'manual',%s,%s) RETURNING *",
                    (
                        cand["batch_id"], cand["kind"], cand["proposed_t_s"],
                        cand.get("proposed_value"),
                        cand.get("reason") or "接受候选修订（该事件此前缺失）",
                        decided_by,
                    ),
                )
                new = cur.fetchone()
            cur.execute(
                "UPDATE event_candidates SET status='accepted', decided_at=now() WHERE id=%s",
                (cid,),
            )
            result = {"candidate_id": cid, "status": "accepted", "applied_event": new}
        else:
            cur.execute(
                "UPDATE event_candidates SET status='rejected', decided_at=now() WHERE id=%s"
                " RETURNING *",
                (cid,),
            )
            result = {"candidate_id": cid, "status": "rejected", "applied_event": cur.fetchone()}
        conn.commit()
    return result


def revise_event_conn(
    cur, event_id: int, new_t_s: float, reason: str | None, revised_by: str,
    new_value: float | None = None,
) -> dict[str, Any]:
    """在已有事务/游标内执行正式修正（供接受候选复用）。"""
    cur.execute("SELECT * FROM events WHERE id=%s FOR UPDATE", (event_id,))
    old = cur.fetchone()
    if old is None:
        raise KeyError(f"event {event_id} not found")
    if old["revoked_at"] is not None:
        raise ValueError("该事件版本已作废")
    cur.execute(
        "INSERT INTO events(batch_id, kind, t_s, value, source, note, created_by)"
        " VALUES (%s,%s,%s,%s,'manual',%s,%s) RETURNING *",
        (
            old["batch_id"], old["kind"], new_t_s,
            new_value if old["kind"] in ("damper", "gas") else None,
            reason or "人工修正", revised_by,
        ),
    )
    new = cur.fetchone()
    cur.execute("UPDATE events SET revoked_at=now() WHERE id=%s", (event_id,))
    cur.execute(
        "INSERT INTO event_revisions(batch_id, event_id, kind, old_t_s, old_source,"
        " new_t_s, new_value, new_source, reason, revised_by)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',%s,%s)",
        (
            old["batch_id"], event_id, old["kind"], old["t_s"], old["source"],
            new_t_s, new_value if old["kind"] in ("damper", "gas") else None,
            reason, revised_by,
        ),
    )
    return new


# ---------------- 比较报告（永久绑定创建时的事件版本） ----------------

def _event_version(bid: int) -> list[dict[str, Any]]:
    """报告创建时的事件版本快照：只取当时有效事件及其 id/来源/时间。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, t_s, value, source, created_by, created_at"
            " FROM events WHERE batch_id=%s AND revoked_at IS NULL ORDER BY t_s, id",
            (bid,),
        )
        rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return rows


def create_report(
    bid_a: int, bid_b: int, alignment_mode: str, smooth_window_s: float,
    ror_window_s: float, snapshot: dict[str, Any], created_by: str = "operator",
    note: str | None = None,
) -> dict[str, Any]:
    va, vb = _event_version(bid_a), _event_version(bid_b)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO comparison_reports(batch_a_id, batch_b_id, alignment,"
            " smooth_window_s, ror_window_s, snapshot, event_version_a, event_version_b,"
            " note, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (bid_a, bid_b, alignment_mode, smooth_window_s, ror_window_s,
             Jsonb(snapshot), Jsonb(va), Jsonb(vb), note, created_by),
        )
        row = cur.fetchone()
        conn.commit()
    row["created_at"] = row["created_at"].isoformat()
    return row


def list_reports() -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.id, r.batch_a_id, r.batch_b_id, r.alignment, r.smooth_window_s,"
            " r.ror_window_s, r.note, r.created_by, r.created_at,"
            " ba.name AS batch_a_name, bb.name AS batch_b_name,"
            " jsonb_array_length(r.event_version_a) AS n_events_a,"
            " jsonb_array_length(r.event_version_b) AS n_events_b"
            " FROM comparison_reports r"
            " JOIN batches ba ON ba.id=r.batch_a_id JOIN batches bb ON bb.id=r.batch_b_id"
            " ORDER BY r.id DESC"
        )
        rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return rows


def get_report(rid: int) -> dict[str, Any] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM comparison_reports WHERE id=%s", (rid,))
        row = cur.fetchone()
    if row:
        row["created_at"] = row["created_at"].isoformat()
    return row
