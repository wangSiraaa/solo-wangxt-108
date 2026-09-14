"""纯分析逻辑测试：插值边界、温升率窗口、阶段区间、原始数据不变性。"""
from __future__ import annotations

import math

from app import analytics
from app.analytics import ComputeParams

P = ComputeParams(smooth_window_s=11.0, ror_window_s=31.0, max_interp_gap_s=20.0)


def _ramp_samples(n=400, rate_c_per_min=10.0, missing=()):
    """线性升温测试数据：rate ℃/min。"""
    rows = []
    for t in range(n):
        if t in missing:
            rows.append({"t_s": float(t), "bean_temp_c": None, "env_temp_c": None, "is_missing": True})
        else:
            rows.append(
                {
                    "t_s": float(t),
                    "bean_temp_c": round(20.0 + rate_c_per_min / 60.0 * t, 4),
                    "env_temp_c": 190.0,
                    "is_missing": False,
                }
            )
    return rows


def _events():
    return [
        {"kind": k, "t_s": float(t), "revoked_at": None, "value": None, "source": "auto"}
        for k, t in [
            ("charge", 0), ("turnaround", 75), ("yellow", 300),
            ("first_crack", 570), ("drop", 690),
        ]
    ]


def test_ror_linear_ramp_recovers_slope():
    samples = _ramp_samples(700, rate_c_per_min=10.0)
    ch = analytics.analyze_channel(samples, "bean_temp_c", P)
    ror = [p["v"] for p in ch["ror"] if 120 < p["t_s"] < 560]
    assert ror, "应在远离边缘处得到温升率"
    assert max(abs(v - 10.0) for v in ror) < 0.05
    assert all(p["window_s"] == 31.0 for p in ch["ror"])
    assert "31" in ch["ror_window"]["description"]


def test_small_gap_interpolated_but_marked():
    samples = _ramp_samples(300, missing=set(range(100, 107)))  # 缺测槽 100..106
    ch = analytics.analyze_channel(samples, "bean_temp_c", P)
    assert len(ch["gaps"]) == 1
    g = ch["gaps"][0]
    # 跨度 = 两侧实测锚点之差（107 - 99 = 8s）
    assert g["interpolated"] is True and g["span_s"] == 8.0
    # 插值点独立成段，不混入 observed
    assert all(99 <= p[0] <= 107 for p in ch["interpolated"])
    assert not any(99 < p[0] < 107 for p in ch["observed"])
    # 1s 网格上缺口内部来源必须是 interpolated
    mid = next(p for p in ch["filled_1s"] if p["t_s"] == 103)
    assert mid["origin"] == "interpolated"
    far = next(p for p in ch["filled_1s"] if p["t_s"] == 200)
    assert far["origin"] == "observed"
    # 缺口附近的温升率/平滑值来源降级为 interpolated
    near = next(p for p in ch["smoothed"] if p["t_s"] == 103)
    assert near["origin"] == "interpolated"


def test_large_gap_not_bridged():
    samples = _ramp_samples(400, missing=set(range(200, 233)))  # 33s > 20s 上限
    ch = analytics.analyze_channel(samples, "bean_temp_c", P)
    g = ch["gaps"][0]
    assert g["interpolated"] is False
    assert ch["interpolated"] == []
    # 缺口内部不产生任何平滑/温升率点
    assert not any(200 <= p["t_s"] <= 233 for p in ch["smoothed"])
    assert not any(200 <= p["t_s"] <= 233 for p in ch["ror"])
    # 窗口跨越缺口边缘的点也应留空（支撑不足）
    assert not any(185 <= p["t_s"] <= 248 for p in ch["ror"])


def test_ror_origin_downgrades_near_interpolated_gap():
    # 小缺口（允许插值）附近的 RoR 必须标 interpolated，远离后恢复 observed。
    # 降级范围 = 差分窗与缺口重叠的区间 ∪ 平滑窗含插值的区间。
    samples = _ramp_samples(400, missing=set(range(100, 107)))  # 锚点 99..107
    ch = analytics.analyze_channel(samples, "bean_temp_c", P)
    # 差分窗(31s)与缺口重叠：t ∈ [99-15.5, 107+15.5] = [83.5, 122.5]
    near = [p for p in ch["ror"] if 83.5 <= p["t_s"] <= 122.5]
    assert near and all(p["origin"] == "interpolated" for p in near)
    far = [p for p in ch["ror"] if p["t_s"] > 140]
    assert far and all(p["origin"] == "observed" for p in far)


def test_smoothing_parameter_does_not_touch_raw():
    samples = _ramp_samples(300, missing=set(range(100, 104)))
    p2 = ComputeParams(smooth_window_s=61.0, ror_window_s=91.0, max_interp_gap_s=20.0)
    a = analytics.analyze_channel(samples, "bean_temp_c", P)
    b = analytics.analyze_channel(samples, "bean_temp_c", p2)
    # 原始实测序列逐点一致
    assert a["observed"] == b["observed"]
    assert a["gaps"] == b["gaps"]
    # 派生序列随参数变化
    assert len(a["smoothed"]) != len(b["smoothed"]) or any(
        x["v"] != y["v"] for x, y in zip(a["smoothed"], b["smoothed"])
    )


def test_phase_metrics_explicit_intervals():
    samples = _ramp_samples(700, rate_c_per_min=10.0)
    m = analytics.phase_metrics(
        analytics.analyze_channel(samples, "bean_temp_c", P), _events()
    )
    assert m["ok"]
    by = {p["key"]: p for p in m["phases"]}
    assert by["drying"]["duration_s"] == 75.0
    assert by["development"]["duration_s"] == 120.0
    # (690-570)/(690-75) = 120/615
    assert math.isclose(m["development_ratio"], round(120 / 615, 4))
    assert m["development_ratio_formula"].startswith("(drop_s")
    dev_ror = by["development"]["ror"]
    assert abs(dev_ror["avg_ror_c_per_min"] - 10.0) < 0.05


def test_nonuniform_timestamps_supported():
    # 间隔 0.7s / 1.5s 交替，证明不假设 1s 采样
    rows, t = [], 0.0
    i = 0
    while t < 200:
        rows.append({"t_s": round(t, 3), "bean_temp_c": 20 + 10 / 60 * t,
                     "env_temp_c": 190.0, "is_missing": False})
        t += 0.7 if i % 2 == 0 else 1.5
        i += 1
    ch = analytics.analyze_channel(rows, "bean_temp_c", P)
    ror = [p["v"] for p in ch["ror"] if 60 < p["t_s"] < 140]
    # 时间加权（梯形）窗均值对线性斜坡无偏，即使采样间隔不均匀
    assert max(abs(v - 10.0) for v in ror) < 0.05
