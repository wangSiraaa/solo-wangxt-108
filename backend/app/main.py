"""FastAPI 应用：批次曲线、温升率、阶段指标、双批次对比、事件修正、可复现导出。

本服务不连接真实烘焙机，数据为合成 + 人工标记。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import analytics, services
from . import alignment as align
from .analytics import ComputeParams
from .config import settings
from .db import init_schema

app = FastAPI(title="Roast Batch Compare", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.on_event("startup")
def _startup() -> None:
    init_schema()
    # 首次启动若无数据则自动播种（合成数据）
    if not services.list_batches():
        services.reseed()


class ReviseIn(BaseModel):
    new_t_s: float = Field(ge=0, description="修正后的事件时间（相对下豆点秒）")
    new_value: float | None = Field(None, ge=0, le=100, description="风门/燃气档位 0-100")
    reason: str | None = None
    revised_by: str = "operator"


class CreateEventIn(BaseModel):
    kind: str
    t_s: float = Field(ge=0)
    value: float | None = Field(None, ge=0, le=100)
    note: str | None = None
    created_by: str = "operator"


def _params(smooth: float | None, ror: float | None) -> ComputeParams:
    sw = settings.default_smooth_window_s if smooth is None else smooth
    rw = settings.default_ror_window_s if ror is None else ror
    if sw < 2 or rw < 2 or sw > 600 or rw > 900:
        raise HTTPException(422, "窗口参数超出合理范围（2–900s）")
    return ComputeParams(sw, rw, settings.max_interp_gap_s)


def _build_detail(bid: int, params: ComputeParams) -> dict[str, Any]:
    batch = services.get_batch(bid)
    if batch is None:
        raise HTTPException(404, f"批次 {bid} 不存在")
    samples = services.load_samples(bid)
    events = services.load_events(bid)
    bean = analytics.analyze_channel(samples, "bean_temp_c", params)
    env = analytics.analyze_channel(samples, "env_temp_c", params)
    return {
        "batch": batch,
        "params": params.as_dict(),
        "bean": bean,
        "env": env,
        "events": events,
        "metrics": analytics.phase_metrics(bean, events),
        "operation_windows": analytics.operation_before_after(bean, events),
        "n_samples": len(samples),
        "n_missing": sum(1 for s in samples if s["is_missing"]),
        "caveats": [
            "全部数据为合成数据，未连接真实烘焙机",
            "实线=原始实测；虚线=对短暂失联的线性插值；灰带=缺测过长未插值",
            "温升率为居中窗口派生量，窗口参数随结果给出；平滑不修改任何原始温度",
            "风门/燃气前后对比仅为描述性统计，不表示因果关系",
        ],
        "notes": services.list_notes(bid=bid),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_link": "not connected (synthetic data only)"}


@app.post("/api/admin/reseed")
def admin_reseed(seed: int = 7) -> dict[str, Any]:
    return {"seed": seed, "counts": services.reseed(seed=seed)}


@app.get("/api/batches")
def batches() -> list[dict[str, Any]]:
    return services.list_batches()


@app.get("/api/batches/{bid}")
def batch_detail(
    bid: int, smooth_window_s: float | None = None, ror_window_s: float | None = None
) -> dict[str, Any]:
    return _build_detail(bid, _params(smooth_window_s, ror_window_s))


@app.get("/api/batches/{bid}/events/revisions")
def batch_revisions(bid: int) -> list[dict[str, Any]]:
    if services.get_batch(bid) is None:
        raise HTTPException(404, "批次不存在")
    return services.load_revisions(bid)


@app.post("/api/events")
def create_event(bid: int, body: CreateEventIn) -> dict[str, Any]:
    if services.get_batch(bid) is None:
        raise HTTPException(404, "批次不存在")
    valid = analytics.THERMAL_KINDS + ("damper", "gas")
    if body.kind not in valid:
        raise HTTPException(422, f"kind 必须是 {valid} 之一")
    try:
        return services.add_manual_event(
            bid, body.kind, body.t_s, body.value, body.note, body.created_by
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.put("/api/events/{event_id}")
def revise_event(event_id: int, body: ReviseIn) -> dict[str, Any]:
    try:
        return services.revise_event(
            event_id, body.new_t_s, body.reason, body.revised_by, body.new_value
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/compare")
def compare(
    a: int,
    b: int,
    smooth_window_s: float | None = None,
    ror_window_s: float | None = None,
    alignment: str = "physical",
) -> dict[str, Any]:
    """双批次比较。

    alignment=physical：两批各自下豆点为 t=0 的真实时间叠加（回答“相同物理时长”）。
    alignment=phase：在共同、有序、已确认事件锚点间做分段线性对齐（回答“相同阶段进度”）。
        事件缺失/顺序矛盾时只对齐共同前缀，不拉伸整条曲线；RoR 仅位置重映射，不在变形轴求导。
    """
    if alignment not in ("physical", "phase"):
        raise HTTPException(422, "alignment 必须是 physical 或 phase")
    params = _params(smooth_window_s, ror_window_s)
    da, db = _build_detail(a, params), _build_detail(b, params)
    cmp_info = align.comparability(da["batch"], db["batch"])
    anchors, issues = align.build_anchors(da["events"], db["events"])

    payload: dict[str, Any] = {
        "params": params.as_dict(),
        "alignment_requested": alignment,
        "a": da,
        "b": db,
        "comparability": cmp_info,
        "anchor_issues": issues,
        "anchors": [
            {"kind": x.kind, "label": x.label, "t_a_s": x.ta, "t_b_s": x.tb}
            for x in anchors
        ],
        "physical_table": align.physical_table(da, db),
        # 与这一对批次相关的备注（含两种对齐模式），前端按当前模式过滤
        "notes": services.list_notes(bid=a, other_id=b),
        "note": (
            "物理对齐=相同真实秒数；相位对齐=相同阶段进度，只是可视化归一化，不代表工艺等效。"
            "风门前后差异不构成因果结论。"
        ),
    }
    if alignment == "phase":
        if not anchors:
            payload["phase"] = {
                "available": False,
                "reason": "没有任何共同锚点（可能缺下豆点），无法分段对齐；请先确认事件或建立候选修订",
            }
        else:
            phase_series = align.build_phase_series(da, db, anchors)
            payload["phase"] = {
                "available": True,
                "version": align.ALIGN_VERSION,
                "canonical_anchors": phase_series["canonical_anchors"],
                "segments": phase_series["segments"],
                "phase_table": align.phase_table(da, db, anchors, phase_series["canonical"]),
                "series": {
                    k: v for k, v in phase_series.items()
                    if k not in ("canonical", "canonical_anchors", "segments")
                },
                "coverage": _phase_coverage(anchors, da, db),
                "ror_rule": "RoR 取自 analytics 在真实时间上的差分结果，相位视图只移动其横坐标，绝不沿变形时间重新求导",
                "not_equivalence": (
                    "分段对齐把阶段速度差异归一化掉了；真实快慢请看 segments 的 a/b 时长与 physical_table，"
                    "对齐本身不构成工艺等效"
                ),
            }
    return payload


def _phase_coverage(anchors, da, db) -> dict[str, Any]:
    """相位对齐覆盖范围；共同锚点之外保持真实时间、不拉伸。"""
    first = anchors[0]
    last = anchors[-1]
    return {
        "aligned_real_range_a_s": [first.ta, last.ta],
        "aligned_real_range_b_s": [first.tb, last.tb],
        "tail_unaligned": (
            "共同锚点仅覆盖到 " + last.label + "；其后的尾段（如出豆）若只一侧有标记，"
            "保持各自真实时间，不在相位视图中强行拉伸"
            if last.kind != "drop" else "全段（下豆→出豆）均在共同锚点内"
        ),
    }


class CandidateIn(BaseModel):
    kind: str
    proposed_t_s: float = Field(ge=0)
    proposed_value: float | None = Field(None, ge=0, le=100)
    reason: str | None = None
    proposed_by: str = "operator"


class CandidateDecisionIn(BaseModel):
    accept: bool
    decided_by: str = "operator"


@app.get("/api/batches/{bid}/candidates")
def list_candidates(bid: int, status: str | None = None) -> list[dict[str, Any]]:
    if services.get_batch(bid) is None:
        raise HTTPException(404, "批次不存在")
    return services.list_candidates(bid, status)


@app.post("/api/batches/{bid}/candidates")
def create_candidate(bid: int, body: CandidateIn) -> dict[str, Any]:
    if services.get_batch(bid) is None:
        raise HTTPException(404, "批次不存在")
    try:
        return services.create_candidate(
            bid, body.kind, body.proposed_t_s, body.reason,
            body.proposed_by, body.proposed_value,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/candidates/{cid}/decision")
def decide_candidate(cid: int, body: CandidateDecisionIn) -> dict[str, Any]:
    """接受候选会落成一次正式人工修正（产生事件新版本+审计行）；拒绝只改候选状态。"""
    try:
        return services.decide_candidate(cid, body.accept, body.decided_by)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


class ReportIn(BaseModel):
    a: int
    b: int
    alignment: str = "physical"
    smooth_window_s: float | None = None
    ror_window_s: float | None = None
    note: str | None = None
    created_by: str = "operator"


@app.post("/api/reports")
def create_report(body: ReportIn) -> dict[str, Any]:
    """把当前比较（含当时两侧事件版本）固化为报告；之后事件再修正也不影响本报告。"""
    if services.get_batch(body.a) is None or services.get_batch(body.b) is None:
        raise HTTPException(404, "批次不存在")
    params = _params(body.smooth_window_s, body.ror_window_s)
    snapshot = compare(body.a, body.b, params.smooth_window_s,
                       params.ror_window_s, body.alignment)
    # 报告快照不必回传体积大的原始数组之外的内容；这里保留完整以便重放
    row = services.create_report(
        body.a, body.b, body.alignment, params.smooth_window_s,
        params.ror_window_s, snapshot, body.created_by, body.note,
    )
    return {"report_id": row["id"], "created_at": row["created_at"],
            "bound_event_version_a": row["event_version_a"],
            "bound_event_version_b": row["event_version_b"]}


@app.get("/api/reports")
def list_reports() -> list[dict[str, Any]]:
    return services.list_reports()


@app.get("/api/reports/{rid}")
def get_report(rid: int) -> dict[str, Any]:
    row = services.get_report(rid)
    if row is None:
        raise HTTPException(404, "报告不存在")
    row["binding_warning"] = (
        "本报告永久绑定创建时两侧的事件版本(event_version_a/b)；"
        "若这些事件此后被人工修正，当前批次视图会不同，但本报告快照不变。"
    )
    return row


# ---------------- 对比结论备注与待办 ----------------

class NoteIn(BaseModel):
    a: int
    b: int
    alignment: str = "physical"
    smooth_window_s: float | None = None
    ror_window_s: float | None = None
    conclusion: str = Field(min_length=1)
    status: str = "followup"
    owner: str | None = None
    due_date: str | None = Field(None, description="截止日期 YYYY-MM-DD")
    created_by: str = "operator"


class NoteStatusIn(BaseModel):
    status: str
    reason: str | None = None
    changed_by: str = "operator"


def _note_anchor_summary(a: int, b: int) -> dict[str, Any]:
    """创建备注时锚点/可比性的精简快照（完整事件版本另存）。"""
    da, db = services.get_batch(a), services.get_batch(b)
    ev_a, ev_b = services.load_events(a), services.load_events(b)
    anchors, issues = align.build_anchors(ev_a, ev_b)
    return {
        "anchors": [
            {"kind": x.kind, "t_a_s": x.ta, "t_b_s": x.tb} for x in anchors
        ],
        "issues": issues,
        "comparability": align.comparability(da, db) if da and db else None,
    }


@app.get("/api/notes")
def list_notes(
    batch_id: int | None = None,
    other_id: int | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in services.NOTE_STATUSES:
        raise HTTPException(422, f"status 必须是 {services.NOTE_STATUSES} 之一")
    return services.list_notes(bid=batch_id, other_id=other_id, status=status)


@app.post("/api/notes")
def create_note(body: NoteIn) -> dict[str, Any]:
    if services.get_batch(body.a) is None or services.get_batch(body.b) is None:
        raise HTTPException(404, "批次不存在")
    if body.alignment not in ("physical", "phase"):
        raise HTTPException(422, "alignment 必须是 physical 或 phase")
    params = _params(body.smooth_window_s, body.ror_window_s)
    due = None
    if body.due_date:
        try:
            from datetime import date
            due = date.fromisoformat(body.due_date).isoformat()
        except ValueError as e:
            raise HTTPException(422, "due_date 必须是 YYYY-MM-DD") from e
    try:
        return services.create_note(
            body.a, body.b, body.alignment, params.smooth_window_s,
            params.ror_window_s, body.conclusion, body.status, body.owner,
            due, body.created_by, anchor_snapshot=_note_anchor_summary(body.a, body.b),
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.put("/api/notes/{nid}/status")
def update_note_status(nid: int, body: NoteStatusIn) -> dict[str, Any]:
    try:
        return services.update_note_status(nid, body.status, body.reason, body.changed_by)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/notes/{nid}/revisions")
def note_revisions(nid: int) -> list[dict[str, Any]]:
    if services.get_note(nid) is None:
        raise HTTPException(404, "备注不存在")
    return services.list_note_revisions(nid)


@app.get("/api/batches/{bid}/export")
def export_batch(
    bid: int, smooth_window_s: float | None = None, ror_window_s: float | None = None
) -> dict[str, Any]:
    """导出可复现包：原始采样、事件及版本、算法与参数、全部派生阶段指标。

    任何人拿到本 JSON，可用相同参数重算得到相同的温升率与阶段指标；
    raw_samples 原样导出，派生数据与原始数据分开存放。
    """
    params = _params(smooth_window_s, ror_window_s)
    detail = _build_detail(bid, params)
    samples = services.load_samples(bid)
    raw_serialized = [
        {
            "t_s": s["t_s"],
            "bean_temp_c": s["bean_temp_c"],
            "env_temp_c": s["env_temp_c"],
            "is_missing": s["is_missing"],
        }
        for s in samples
    ]
    bundle = {
        "export_schema": "roast-export-v1",
        "machine_connected": False,
        "machine_note": "合成数据；原始采样中的 NULL 表示探针失联/缺测，不得填值后冒充实测",
        "batch": detail["batch"],
        "compute": {
            **params.as_dict(),
            "interpolation_rule": (
                f"仅对两侧均有实测锚点且跨度 ≤ {params.max_interp_gap_s:g}s 的内部缺口线性插值；"
                "插值点 origin=interpolated；其余缺测保持空缺"
            ),
            "ror_rule": (
                f"豆温先做 {params.smooth_window_s:g}s 居中窗均值平滑，再以 "
                f"{params.ror_window_s:g}s 居中窗口对平滑曲线端点差分，单位 ℃/min"
            ),
            "development_ratio_rule": "(drop-first_crack)/(drop-turnaround)",
        },
        "raw_samples": raw_serialized,
        "events_active": services.load_events(bid),
        "event_revisions": services.load_revisions(bid),
        "derived": {
            "bean": detail["bean"],
            "env": detail["env"],
            "metrics": detail["metrics"],
            "operation_windows": detail["operation_windows"],
        },
    }
    out = Path("/workspace/backend/exports")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"batch_{bid}_export.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle["exported_file"] = str(path)
    return bundle
