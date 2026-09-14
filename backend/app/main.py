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
) -> dict[str, Any]:
    params = _params(smooth_window_s, ror_window_s)
    return {
        "params": params.as_dict(),
        "time_reference": "两个批次的时间轴均以各自下豆点(charge)为 0，直接对齐",
        "a": _build_detail(a, params),
        "b": _build_detail(b, params),
        "note": "双批次叠加仅用于目视比较；风门前后差异不构成因果结论",
    }


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
