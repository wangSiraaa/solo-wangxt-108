"""数据写入/修正服务：合成播种与人工事件修正（保留来源与版本轨迹）。"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from . import analytics
from .db import get_conn
from .synth import build_profiles, generate_samples


def reseed(seed: int = 7) -> dict[str, int]:
    """删除旧数据并写入合成批次（采样 + 事件 + 对比备注 + 复盘标签）。幂等。"""
    profiles = build_profiles()
    counts = {"batches": 0, "samples": 0, "events": 0, "notes": 0, "tags": 0}
    batch_ids: list[int] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE event_tags, note_status_revisions, comparison_notes, comparison_reports,"
            " event_candidates, event_revisions, events, samples, batches RESTART IDENTITY"
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
            batch_ids.append(bid)
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

        # 两条合成对比备注：相位对齐（待跟进）与物理对齐（已确认），均绑定当时事件版本
        def _ev_versions(cur, bid):
            cur.execute(
                "SELECT id, kind, t_s, value, source, created_by, created_at"
                " FROM events WHERE batch_id=%s AND revoked_at IS NULL ORDER BY t_s, id",
                (bid,),
            )
            return [
                {**r, "created_at": r["created_at"].isoformat()} for r in cur.fetchall()
            ]

        def _anchor_summary(cur, ba, bb):
            cur.execute(
                "SELECT kind, t_s FROM events WHERE batch_id=%s AND revoked_at IS NULL"
                " AND kind IN ('charge','turnaround','yellow','first_crack','drop')",
                (ba,),
            )
            aev = {r["kind"]: r["t_s"] for r in cur.fetchall()}
            cur.execute(
                "SELECT kind, t_s FROM events WHERE batch_id=%s AND revoked_at IS NULL"
                " AND kind IN ('charge','turnaround','yellow','first_crack','drop')",
                (bb,),
            )
            bev = {r["kind"]: r["t_s"] for r in cur.fetchall()}
            common = [k for k in ("charge", "turnaround", "yellow", "first_crack", "drop")
                      if k in aev and k in bev]
            return {"anchors": common, "t_a": {k: aev[k] for k in common},
                    "t_b": {k: bev[k] for k in common}}

        id_a, id_b = batch_ids[0], batch_ids[1]
        seed_notes = [
            {
                "alignment": "phase", "status": "followup",
                "conclusion": ("相位对齐下两批发展段进度相近，但大锅量批次回温点→变黄真实用时更长，"
                               "下批次尝试降低初火以缩短该段；待复测确认。"),
                "owner": "zhang", "due": "2026-09-21", "by": "li",
            },
            {
                "alignment": "physical", "status": "confirmed",
                "conclusion": ("相同物理秒数（出豆前 120s）两批豆温差 <3℃，风门提前 40s 未造成明显温度偏移；"
                               "本结论仅为描述性对比，不表示风门与温度的因果关系。"),
                "owner": "wang", "due": None, "by": "li",
            },
        ]
        for sn in seed_notes:
            cur.execute(
                "INSERT INTO comparison_notes(batch_a_id, batch_b_id, alignment,"
                " smooth_window_s, ror_window_s, conclusion, status, owner, due_date,"
                " event_version_a, event_version_b, anchor_snapshot, created_by)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (id_a, id_b, sn["alignment"], 21.0, 45.0, sn["conclusion"],
                 sn["status"], sn["owner"], sn["due"],
                 Jsonb(_ev_versions(cur, id_a)), Jsonb(_ev_versions(cur, id_b)),
                 Jsonb(_anchor_summary(cur, id_a, id_b)), sn["by"]),
            )
            nid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO note_status_revisions(note_id, old_status, new_status, reason, changed_by)"
                " VALUES (%s,NULL,%s,'播种样例：初始状态',%s)",
                (nid, sn["status"], sn["by"]),
            )
            counts["notes"] += 1

        # 三条阶段复盘标签样例（批次1）：分别覆盖一爆、出豆、变黄，绑定创建时事件版本
        def _seed_tag(cur, bid, kind, label, desc, by):
            cur.execute(
                "SELECT id, t_s, source FROM events WHERE batch_id=%s AND kind=%s"
                " AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
                (bid, kind),
            )
            ev = cur.fetchone()
            cur.execute(
                "INSERT INTO event_tags(batch_id, event_kind, label, description,"
                " bound_event_id, event_t_s, event_source, created_by)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (bid, kind, label, desc, ev["id"], ev["t_s"], ev["source"], by),
            )

        for kind, label, desc in [
            ("first_crack", "一爆判断偏晚", "听声复核，一爆起点比标记早约 10s，下批注意提前观察。"),
            ("drop", "尾段火力过强", "出豆前豆温升率偏高，末段应再降火，避免豆表过烘。"),
            ("yellow", "回黄正常", "变黄温度与时间符合本配方预期，可作为参考基准。"),
        ]:
            _seed_tag(cur, batch_ids[0], kind, label, desc, "li")
            counts["tags"] += 1
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


# ---------------- 对比结论备注与待办 ----------------

NOTE_STATUSES = ("followup", "confirmed", "discarded")
NOTE_STATUS_LABEL = {"followup": "待跟进", "confirmed": "已确认", "discarded": "已废弃"}


def _serialize_note(row: dict[str, Any]) -> dict[str, Any]:
    row["created_at"] = row["created_at"].isoformat()
    row["updated_at"] = row["updated_at"].isoformat()
    if row.get("due_date") is not None:
        row["due_date"] = row["due_date"].isoformat()
    return row


def list_notes(
    bid: int | None = None,
    other_id: int | None = None,
    status: str | None = None,
    include_pair_both_directions: bool = True,
) -> list[dict[str, Any]]:
    """列出备注。

    bid 给定时返回“与该批次相关”的备注（不分 A/B 方向，便于批次详情页展示）；
    同时给 other_id 时可限定为这一对批次。status 过滤状态。
    """
    sql = (
        "SELECT n.*, ba.name AS batch_a_name, bb.name AS batch_b_name,"
        " (SELECT count(*) FROM note_status_revisions r WHERE r.note_id=n.id) AS n_revisions"
        " FROM comparison_notes n"
        " JOIN batches ba ON ba.id=n.batch_a_id JOIN batches bb ON bb.id=n.batch_b_id"
    )
    where, params = [], []
    if bid is not None:
        if other_id is not None and include_pair_both_directions:
            where.append(
                "((n.batch_a_id=%s AND n.batch_b_id=%s)"
                " OR (n.batch_a_id=%s AND n.batch_b_id=%s))"
            )
            params += [bid, other_id, other_id, bid]
        elif other_id is not None:
            where.append("n.batch_a_id=%s AND n.batch_b_id=%s")
            params += [bid, other_id]
        else:
            where.append("(%s IN (n.batch_a_id, n.batch_b_id))")
            params.append(bid)
    if status:
        where.append("n.status=%s")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY n.created_at DESC, n.id DESC"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_serialize_note(r) for r in rows]


def get_note(nid: int) -> dict[str, Any] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT n.*, ba.name AS batch_a_name, bb.name AS batch_b_name"
            " FROM comparison_notes n"
            " JOIN batches ba ON ba.id=n.batch_a_id JOIN batches bb ON bb.id=n.batch_b_id"
            " WHERE n.id=%s",
            (nid,),
        )
        row = cur.fetchone()
    return _serialize_note(row) if row else None


def list_note_revisions(nid: int) -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM note_status_revisions WHERE note_id=%s ORDER BY changed_at, id",
            (nid,),
        )
        rows = cur.fetchall()
    for r in rows:
        r["changed_at"] = r["changed_at"].isoformat()
    return rows


def create_note(
    bid_a: int, bid_b: int, alignment: str, smooth_window_s: float,
    ror_window_s: float, conclusion: str, status: str, owner: str | None,
    due_date: str | None, created_by: str, anchor_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建备注：同时快照两批当前有效事件版本，之后事件修正不影响本备注。"""
    if alignment not in ("physical", "phase"):
        raise ValueError("alignment 必须是 physical 或 phase")
    if status not in NOTE_STATUSES:
        raise ValueError(f"status 必须是 {NOTE_STATUSES} 之一")
    if not conclusion or not conclusion.strip():
        raise ValueError("结论内容不能为空")
    va, vb = _event_version(bid_a), _event_version(bid_b)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO comparison_notes(batch_a_id, batch_b_id, alignment,"
            " smooth_window_s, ror_window_s, conclusion, status, owner, due_date,"
            " event_version_a, event_version_b, anchor_snapshot, created_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (bid_a, bid_b, alignment, float(smooth_window_s), float(ror_window_s),
             conclusion.strip(), status, owner, due_date,
             Jsonb(va), Jsonb(vb), Jsonb(anchor_snapshot or {}), created_by),
        )
        nid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO note_status_revisions(note_id, old_status, new_status, reason, changed_by)"
            " VALUES (%s,NULL,%s,'创建备注时的初始状态',%s)",
            (nid, status, created_by),
        )
        conn.commit()
    note = get_note(nid)
    assert note is not None
    return note


def update_note_status(
    nid: int, new_status: str, reason: str | None, changed_by: str
) -> dict[str, Any]:
    """更新备注状态：旧状态、原因、操作人写入审计轨迹；快照字段不变。"""
    if new_status not in NOTE_STATUSES:
        raise ValueError(f"status 必须是 {NOTE_STATUSES} 之一")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM comparison_notes WHERE id=%s FOR UPDATE", (nid,))
        row = cur.fetchone()
        if row is None:
            raise KeyError("备注不存在")
        old = row["status"]
        if old == new_status:
            raise ValueError(f"备注已是 {NOTE_STATUS_LABEL[new_status]} 状态，无需更新")
        cur.execute(
            "UPDATE comparison_notes SET status=%s, updated_at=now() WHERE id=%s",
            (new_status, nid),
        )
        cur.execute(
            "INSERT INTO note_status_revisions(note_id, old_status, new_status, reason, changed_by)"
            " VALUES (%s,%s,%s,%s,%s)",
            (nid, old, new_status, reason or f"状态由{NOTE_STATUS_LABEL[old]}改为{NOTE_STATUS_LABEL[new_status]}",
             changed_by),
        )
        conn.commit()
    note = get_note(nid)
    assert note is not None
    return note


# ---------------- 阶段复盘标签（绑定事件版本快照） ----------------

def _serialize_tag(row: dict[str, Any]) -> dict[str, Any]:
    row["created_at"] = row["created_at"].isoformat()
    return row


def list_tags(bid: int, event_kind: str | None = None) -> list[dict[str, Any]]:
    """列出批次标签，可按事件类型过滤。并标记当前事件是否已相对标签快照变更。"""
    sql = (
        "SELECT t.* FROM event_tags t WHERE t.batch_id=%s"
    )
    params: list[Any] = [bid]
    if event_kind:
        sql += " AND t.event_kind=%s"
        params.append(event_kind)
    sql += " ORDER BY t.created_at DESC, t.id DESC"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        tags = [_serialize_tag(r) for r in cur.fetchall()]
        # 当前有效事件
        cur.execute(
            "SELECT kind, id, t_s, source FROM events"
            " WHERE batch_id=%s AND revoked_at IS NULL",
            (bid,),
        )
        current = {r["kind"]: r for r in cur.fetchall()}
    for t in tags:
        cur_ev = current.get(t["event_kind"])
        if cur_ev is None:
            t["current_state"] = "event_missing"
            t["current_event"] = None
            t["changed"] = True
        else:
            t["current_event"] = {
                "id": cur_ev["id"], "t_s": cur_ev["t_s"], "source": cur_ev["source"]
            }
            changed = (
                cur_ev["id"] != t["bound_event_id"]
                or float(cur_ev["t_s"]) != float(t["event_t_s"])
            )
            t["changed"] = changed
            t["current_state"] = "changed" if changed else "current"
    return tags


def create_tag(
    bid: int, event_kind: str, label: str, description: str | None, created_by: str
) -> dict[str, Any]:
    """为当前有效事件创建标签：保存当时事件 id/时间/来源作为快照。"""
    valid = analytics.THERMAL_KINDS + ("damper", "gas")
    if event_kind not in valid:
        raise ValueError(f"event_kind 必须是 {valid} 之一")
    if not label or not label.strip():
        raise ValueError("标签内容不能为空")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM events WHERE batch_id=%s AND kind=%s AND revoked_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (bid, event_kind),
        )
        ev = cur.fetchone()
        if ev is None:
            raise LookupError(f"批次没有已确认的 {event_kind} 事件，无法绑定标签")
        cur.execute(
            "INSERT INTO event_tags(batch_id, event_kind, label, description,"
            " bound_event_id, event_t_s, event_source, created_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (bid, event_kind, label.strip(), description, ev["id"], ev["t_s"],
             ev["source"], created_by),
        )
        row = cur.fetchone()
        conn.commit()
    return _serialize_tag(dict(row))


def delete_tag(tid: int) -> None:
    """只删除标签本身；不触碰 events / event_revisions。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM event_tags WHERE id=%s", (tid,))
        if cur.rowcount == 0:
            raise KeyError("标签不存在")
        conn.commit()
