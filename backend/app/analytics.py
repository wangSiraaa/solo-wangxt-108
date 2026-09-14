"""曲线分析（NumPy 实现）。

设计原则（对应需求）：
1. 只读取原始采样，绝不回写/修改原始温度；平滑结果是独立的派生序列。
2. 采样间隔不均匀：所有计算在真实时间戳上进行，不假设等间隔。
3. 探针短暂失联：
   - 缺口跨度 <= max_interp_gap_s：允许线性插值，插值点 origin="interpolated"，
     前端以虚线呈现，绝不与实测点混在一起；
   - 缺口更长：不插值，标为 missing band；派生序列在受影响区间也置空。
4. 温升率采用明确的居中窗口（端点差分），窗口长度随结果一起返回；
   窗口支撑数据不足（含不可跨越缺口、贴边）时该点不输出数值。
5. 阶段指标只按事件时间做区间算术；事件时间来自人工可修正的事件表。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

ALGO_VERSION = "ror-centered-v1"

THERMAL_KINDS = ("charge", "turnaround", "yellow", "first_crack", "drop")
KIND_LABEL = {
    "charge": "下豆",
    "turnaround": "回温点",
    "yellow": "变黄",
    "first_crack": "一爆",
    "drop": "出豆",
    "damper": "风门",
    "gas": "燃气",
}


@dataclass(frozen=True)
class ComputeParams:
    smooth_window_s: float
    ror_window_s: float
    max_interp_gap_s: float

    def as_dict(self) -> dict[str, float]:
        return {
            "smooth_window_s": self.smooth_window_s,
            "ror_window_s": self.ror_window_s,
            "max_interp_gap_s": self.max_interp_gap_s,
            "algo_version": ALGO_VERSION,
        }


def _channel_arrays(samples: list[dict[str, Any]], channel: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (全部时间戳, 观测值或NaN, 是否观测到)，按时间排序。"""
    rows = sorted(samples, key=lambda r: r["t_s"])
    t = np.array([r["t_s"] for r in rows], dtype=float)
    v = np.array(
        [np.nan if r.get(channel) is None else float(r[channel]) for r in rows],
        dtype=float,
    )
    observed = ~np.isnan(v)
    return t, v, observed


def _scan_gaps(t: np.ndarray, observed: np.ndarray, max_gap: float) -> list[dict[str, Any]]:
    """识别观测断档：连续缺测槽构成的缺口，跨度 = 两侧实测点时间差。"""
    gaps: list[dict[str, Any]] = []
    n = len(t)
    i = 0
    while i < n:
        if not observed[i]:
            j = i
            while j < n and not observed[j]:
                j += 1
            left = t[i - 1] if i > 0 and observed[i - 1] else None
            right = t[j] if j < n and observed[j] else None
            if left is not None and right is not None:
                span = float(right - left)
                gaps.append(
                    {
                        "start": float(left),
                        "end": float(right),
                        "span_s": round(span, 2),
                        "interpolated": span <= max_gap,
                        "reason": (
                            f"缺口 {span:.1f}s ≤ 上限 {max_gap:g}s，采用线性插值（虚线标注）"
                            if span <= max_gap
                            else f"缺口 {span:.1f}s 超过插值上限 {max_gap:g}s，不插值"
                        ),
                    }
                )
            else:
                gaps.append(
                    {
                        "start": float(t[i]),
                        "end": float(t[j - 1]),
                        "span_s": round(float(t[j - 1] - t[i]), 2),
                        "interpolated": False,
                        "reason": "缺口位于序列边缘，单侧无实测锚点，不插值",
                    }
                )
            i = j
        else:
            i += 1
    return gaps


def _interpolated_points(
    t_all: np.ndarray, v_obs: np.ndarray, observed: np.ndarray, gaps: list[dict[str, Any]]
) -> list[list[float]]:
    """仅对允许跨越的缺口生成内部插值点（含两端锚点，供画虚线段）。"""
    out: list[list[float]] = []
    if not observed.any():
        return out
    t_obs = t_all[observed]
    v_t = v_obs[observed]
    for g in gaps:
        if not g["interpolated"]:
            continue
        seg_t = [g["start"]]
        seg_t.extend(
            round(float(x), 2)
            for x in t_all[(t_all > g["start"]) & (t_all < g["end"])]
        )
        seg_t.append(g["end"])
        for ts in seg_t:
            out.append([float(ts), round(float(np.interp(ts, t_obs, v_t)), 2)])
    return out


def _support_regions(t_all: np.ndarray, observed: np.ndarray, gaps: list[dict[str, Any]]):
    """返回 (可支撑的时间区间列表, 含插值的时间区间列表)。

    可支撑区间 = 实测区间 ∪ 允许插值的小缺口；
    大缺口把序列切成多段。
    """
    supported: list[tuple[float, float]] = []
    interp_spans: list[tuple[float, float]] = [
        (g["start"], g["end"]) for g in gaps if g["interpolated"]
    ]
    if len(t_all) == 0:
        return supported, interp_spans
    blocked = sorted(
        [(g["start"], g["end"]) for g in gaps if not g["interpolated"]]
    )
    cursor = float(t_all[0])
    end_all = float(t_all[-1])
    for lo, hi in blocked:
        if lo > cursor:
            supported.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < end_all:
        supported.append((cursor, end_all))
    return supported, interp_spans


def _value_on_grid(
    eval_t: np.ndarray, t_all: np.ndarray, v_obs: np.ndarray, observed: np.ndarray,
    supported: list[tuple[float, float]], interp_spans: list[tuple[float, float]],
) -> tuple[np.ndarray, np.ndarray]:
    """在评估时刻取值；返回 (值, 来源标记数组)，来源 0=实测, 1=插值, -1=不可用。"""
    t_obs = t_all[observed]
    v_t = v_obs[observed]
    vals = np.full(eval_t.shape, np.nan)
    origin = np.full(eval_t.shape, -1, dtype=int)
    for idx, te in enumerate(eval_t):
        in_supported = any(lo - 1e-9 <= te <= hi + 1e-9 for lo, hi in supported)
        if not in_supported:
            continue
        vals[idx] = float(np.interp(te, t_obs, v_t))
        in_interp = any(lo - 1e-9 <= te <= hi + 1e-9 for lo, hi in interp_spans)
        origin[idx] = 1 if in_interp else 0
    return vals, origin


def _centered_window_smooth(
    eval_t: np.ndarray, t_all: np.ndarray, v_obs: np.ndarray, observed: np.ndarray,
    supported: list[tuple[float, float]], interp_spans: list[tuple[float, float]],
    window_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    """居中时间窗均值平滑。

    仅当 [t-w/2, t+w/2] 整段落在可支撑区间内才输出；
    窗内若含插值段，origin 降级为 1（插值派生）。这是派生序列，不碰原始值。
    """
    half = window_s / 2.0
    vals = np.full(eval_t.shape, np.nan)
    origin = np.full(eval_t.shape, -1, dtype=int)
    t_obs = t_all[observed]
    v_t = v_obs[observed]
    for idx, te in enumerate(eval_t):
        lo, hi = te - half, te + half
        if not any(a - 1e-9 <= lo and hi <= b + 1e-9 for a, b in supported):
            continue
        # 时间加权（梯形）窗均值：非均匀采样下对线性斜坡无偏；
        # 窗两端各补一个边界插值点，再用积分面积除以窗宽。
        m = (t_obs >= lo - 1e-9) & (t_obs <= hi + 1e-9)
        ts = t_obs[m]
        vs = v_t[m]
        if ts.size < 3:
            continue
        if ts[0] > lo:
            vs = np.concatenate([[float(np.interp(lo, t_obs, v_t))], vs])
            ts = np.concatenate([[lo], ts])
        if ts[-1] < hi:
            vs = np.concatenate([vs, [float(np.interp(hi, t_obs, v_t))]])
            ts = np.concatenate([ts, [hi]])
        area = float(np.trapezoid(vs, ts))
        vals[idx] = area / (hi - lo)
        touches_interp = any(
            lo <= b + 1e-9 and hi >= a - 1e-9 for a, b in interp_spans
        )
        origin[idx] = 1 if touches_interp else 0
    return vals, origin


def _ror(
    eval_t: np.ndarray, smooth_vals: np.ndarray, smooth_origin: np.ndarray,
    window_s: float, interp_spans: list[tuple[float, float]] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """温升率（℃/min）：对平滑曲线做居中端点差分。

    RoR(t) = (T_smooth(t+W/2) - T_smooth(t-W/2)) / W * 60
    两个端点都必须有平滑值；origin 取两端较弱者，且只要差分窗口与任何
    插值区间重叠（即该 RoR 的"视线"穿过缺口），也降级为 interpolated。
    """
    interp_spans = interp_spans or []
    half = window_s / 2.0
    ror = np.full(eval_t.shape, np.nan)
    origin = np.full(eval_t.shape, -1, dtype=int)
    for idx, te in enumerate(eval_t):
        lo_t, hi_t = te - half, te + half
        # 两端在各自名义边界附近取最近网格点（只在相邻两个候选里挑，不能全局搜索）
        ins = int(np.searchsorted(eval_t, lo_t, side="left"))
        cand_lo = [i for i in (ins - 1, ins, ins + 1) if 0 <= i < len(eval_t)]
        i_lo = min(cand_lo, key=lambda i: abs(eval_t[i] - lo_t))
        ins = int(np.searchsorted(eval_t, hi_t, side="left"))
        cand_hi = [i for i in (ins - 1, ins, ins + 1) if 0 <= i < len(eval_t)]
        i_hi = min(cand_hi, key=lambda i: abs(eval_t[i] - hi_t))
        if abs(eval_t[i_lo] - lo_t) > 0.55 or abs(eval_t[i_hi] - hi_t) > 0.55:
            continue
        if i_hi <= i_lo:
            continue
        a, b = smooth_vals[i_lo], smooth_vals[i_hi]
        if math.isnan(a) or math.isnan(b):
            continue
        # 网格端点可能落在名义边界之间（1s 网格对奇数半窗），
        # 分母必须用两端实际时间差，而不是名义窗口长度。
        actual_dt = float(eval_t[i_hi] - eval_t[i_lo])
        if actual_dt <= 0:
            continue
        ror[idx] = (b - a) / actual_dt * 60.0
        # 来源取两端较弱者：任一端点依赖插值，则该 RoR 也标为 interpolated
        origin[idx] = max(smooth_origin[i_lo], smooth_origin[i_hi])
        # 差分窗口"视线穿过"插值缺口时同样降级
        if origin[idx] == 0 and any(
            lo_t <= b2 + 1e-9 and hi_t >= a2 - 1e-9 for a2, b2 in interp_spans
        ):
            origin[idx] = 1
    return ror, origin


_ORIGIN_NAME = {0: "observed", 1: "interpolated", -1: None}


def analyze_channel(
    samples: list[dict[str, Any]], channel: str, params: ComputeParams
) -> dict[str, Any]:
    t_all, v_obs, observed = _channel_arrays(samples, channel)
    gaps = _scan_gaps(t_all, observed, params.max_interp_gap_s)
    supported, interp_spans = _support_regions(t_all, observed, gaps)

    obs_pts = [
        [round(float(t), 2), round(float(v), 2)]
        for t, v, ok in zip(t_all, v_obs, observed) if ok
    ]
    interp_pts = _interpolated_points(t_all, v_obs, observed, gaps)

    if len(t_all) > 0:
        grid = np.arange(math.ceil(float(t_all[0])), math.floor(float(t_all[-1])) + 1, 1.0)
    else:
        grid = np.array([])

    raw_interp_vals, raw_interp_origin = _value_on_grid(
        grid, t_all, v_obs, observed, supported, interp_spans
    )
    env_filled = [
        {"t_s": round(float(t), 2), "v": round(float(v), 2), "origin": _ORIGIN_NAME[int(o)]}
        for t, v, o in zip(grid, raw_interp_vals, raw_interp_origin) if o >= 0
    ]

    out: dict[str, Any] = {
        "observed": obs_pts,
        "interpolated": interp_pts,
        "filled_1s": env_filled,
        "gaps": gaps,
    }

    if channel == "bean_temp_c":
        smooth_vals, smooth_origin = _centered_window_smooth(
            grid, t_all, v_obs, observed, supported, interp_spans,
            params.smooth_window_s,
        )
        ror_vals, ror_origin = _ror(
            grid, smooth_vals, smooth_origin, params.ror_window_s, interp_spans
        )
        out["smoothed"] = [
            {"t_s": round(float(t), 2), "v": round(float(v), 3), "origin": _ORIGIN_NAME[int(o)]}
            for t, v, o in zip(grid, smooth_vals, smooth_origin) if o >= 0
        ]
        out["ror"] = [
            {
                "t_s": round(float(t), 2),
                "v": round(float(v), 3),
                "origin": _ORIGIN_NAME[int(o)],
                "window_s": params.ror_window_s,
                "method": "centered-endpoint-diff on centered-moving-average",
            }
            for t, v, o in zip(grid, ror_vals, ror_origin) if o >= 0
        ]
        out["ror_window"] = {
            "window_s": params.ror_window_s,
            "smooth_window_s": params.smooth_window_s,
            "description": (
                f"温升率采用居中窗口（整窗 {params.ror_window_s:g}s，即 t±{params.ror_window_s / 2:g}s）："
                f"先对豆温做 {params.smooth_window_s:g}s 居中窗均值平滑，"
                f"再取 t±{params.ror_window_s / 2:g}s 两个平滑端点做差分（℃/min）；"
                "网格离散导致的实际跨度用于分母；窗口内含不可跨越缺口或靠近序列边缘时该点留空"
            ),
        }
    return out


def _active_event_map(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    m: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("revoked_at") is not None:
            continue
        if e["kind"] in ("damper", "gas"):
            continue
        m[e["kind"]] = e
    return m


def _temp_at(channel: dict[str, Any], ts: float) -> float | None:
    """事件时刻的豆温：优先实测网格，其次允许的插值；落在缺口内返回 None。"""
    for p in channel["filled_1s"]:
        if abs(p["t_s"] - ts) < 0.55:
            return p["v"]
    return None


def _ror_stats(
    ror_pts: list[dict[str, Any]], t0: float, t1: float, clip_lo: float | None = None
) -> dict[str, Any] | None:
    """区间内有效 RoR 点的均值与覆盖率。

    clip_lo：区间内只统计 t >= clip_lo 的点（脱水段回温前豆温在下降，
    不属于"温升"，默认从回温点起算）。
    """
    start = t0 if clip_lo is None else max(t0, clip_lo)
    duration = max(0.0, t1 - t0)
    if duration <= 0 or not ror_pts:
        return None
    inside = [
        p["v"]
        for p in ror_pts
        if start - 0.55 <= p["t_s"] <= t1 + 0.55
    ]
    valid_s = float(len(inside))  # 1s 网格，有效点数≈有效秒数
    return {
        "avg_ror_c_per_min": round(float(np.mean(inside)), 3) if inside else None,
        # 有效点覆盖的是 [start,t1]（相对整个区间做归一化，上限 1）
        "coverage": round(min(1.0, valid_s / max(t1 - start, 1e-9)), 3),
        "n_valid": len(inside),
        "eval_start_s": round(start, 2),
    }


def phase_metrics(
    bean_channel: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """按明确区间计算阶段指标。

    区间定义（全部基于事件表时间，可人工修正）：
      脱水 drying:      charge      → turnaround
      梅纳 maillard:    turnaround  → yellow
      发展 development: first_crack → drop
      发展时间比:       (drop - first_crack) / (drop - turnaround)
    """
    em = _active_event_map(events)
    missing = [k for k in THERMAL_KINDS if k not in em]
    if missing:
        return {"ok": False, "missing_events": missing, "phases": []}

    t = {k: float(em[k]["t_s"]) for k in THERMAL_KINDS}
    ror_pts = bean_channel.get("ror", [])

    phase_defs = [
        ("drying", "脱水段（下豆→回温点）", "charge", "turnaround"),
        ("maillard", "梅纳段（回温点→变黄）", "turnaround", "yellow"),
        ("between_yellow_fc", "升温段（变黄→一爆）", "yellow", "first_crack"),
        ("development", "发展段（一爆→出豆）", "first_crack", "drop"),
    ]
    phases = []
    for key, label, k0, k1 in phase_defs:
        t0, t1 = t[k0], t[k1]
        # 脱水段包含下豆后的温降，平均"温升率"只在回温点之后有意义
        clip = t["turnaround"] if key == "drying" else None
        phases.append(
            {
                "key": key,
                "label": label,
                "interval": {"from": k0, "to": k1, "start_s": round(t0, 2), "end_s": round(t1, 2)},
                "duration_s": round(t1 - t0, 2),
                "start_bean_c": _temp_at(bean_channel, t0),
                "end_bean_c": _temp_at(bean_channel, t1),
                "ror": _ror_stats(ror_pts, t0, t1, clip_lo=clip),
                "ror_note": (
                    "脱水段在回温点之前为降温，RoR 均值仅统计回温点→该段结束"
                    if key == "drying"
                    else None
                ),
            }
        )
    total_after_turnaround = t["drop"] - t["turnaround"]
    dev = t["drop"] - t["first_crack"]
    return {
        "ok": True,
        "phases": phases,
        "turnaround_bean_c": _temp_at(bean_channel, t["turnaround"]),
        "development_s": round(dev, 2),
        "total_after_turnaround_s": round(total_after_turnaround, 2),
        "development_ratio": round(dev / total_after_turnaround, 4),
        "development_ratio_formula": "(drop_s - first_crack_s) / (drop_s - turnaround_s)",
    }


def operation_before_after(
    bean_channel: dict[str, Any], events: list[dict[str, Any]], window_s: float = 60.0
) -> list[dict[str, Any]]:
    """风门/燃气变化前后的描述性统计。

    仅报告操作时刻前后各 window_s 窗内的平均温升率与豆温，
    不做任何因果归因（操作与温度同时受其他因素影响，样本为单批次）。
    """
    ror_pts = bean_channel.get("ror", [])
    smooth_pts = bean_channel.get("smoothed", [])
    out = []
    for e in events:
        if e.get("revoked_at") is not None or e["kind"] not in ("damper", "gas"):
            continue
        tc = float(e["t_s"])

        def avg(points, lo, hi):
            xs = [p["v"] for p in points if lo - 0.55 <= p["t_s"] <= hi + 0.55]
            return round(float(np.mean(xs)), 3) if xs else None

        before = {"ror": avg(ror_pts, tc - window_s, tc), "bean": avg(smooth_pts, tc - window_s, tc)}
        after = {"ror": avg(ror_pts, tc, tc + window_s), "bean": avg(smooth_pts, tc, tc + window_s)}
        out.append(
            {
                "event_id": e["id"],
                "kind": e["kind"],
                "label": KIND_LABEL[e["kind"]],
                "t_s": round(tc, 2),
                "value": e["value"],
                "window_s": window_s,
                "before": before,
                "after": after,
                "interpretation": "前后窗均值为描述性对比，不表示该操作导致温度变化（非因果）",
            }
        )
    out.sort(key=lambda x: x["t_s"])
    return out
