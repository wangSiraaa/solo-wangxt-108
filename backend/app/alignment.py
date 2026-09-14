"""分段时间对齐与批次可比性。

关键约束（对应需求）：
1. 保留原始时间轴：原始采样、平滑、RoR 全部不动。这里只额外产出
   “物理对齐”和“相位对齐”两套**派生显示坐标**。
2. 相位对齐只在两批次共同拥有、且顺序一致的已确认事件锚点之间做分段线性变换；
   事件缺失或顺序矛盾时**绝不强行拉伸整条曲线**——共同前缀之外的区域保持真实时间，
   并标记 aligned=False、给出原因。
3. 温升率 RoR 始终是真实时间上的差分结果（由 analytics 计算）。
   相位视图只是把已有 RoR 点“搬运”到相位坐标，**不在变形时间上重新求导**。
4. 比较输出区分两种问题：
   - physical：相同物理时长（相对各自下豆点的同一秒）下的数值；
   - phase：相同阶段进度（同锚点间同一百分比）下的数值。
   两者各自标注可比性，不混为一谈。
5. 模型对齐是可视化归一化，不代表工艺等效（对齐本身会抹掉阶段速度差异，
   所以阶段时长被单独列出，用于真正比较快慢）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

ALIGN_VERSION = "segment-phase-align-v1"

# 锚点顺序；操作事件不参与对齐
ANCHOR_KINDS = ["charge", "turnaround", "yellow", "first_crack", "drop"]
ANCHOR_LABEL = {
    "charge": "下豆",
    "turnaround": "回温点",
    "yellow": "变黄",
    "first_crack": "一爆",
    "drop": "出豆",
}


@dataclass(frozen=True)
class Anchor:
    kind: str
    ta: float
    tb: float

    @property
    def label(self) -> str:
        return ANCHOR_LABEL[self.kind]


def _active_thermal_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("revoked_at") is not None:
            continue
        if e["kind"] in ANCHOR_KINDS:
            out[e["kind"]] = e
    return out


def _ordered_anchor_times(evmap: dict[str, dict[str, Any]]) -> dict[str, float]:
    return {k: float(evmap[k]["t_s"]) for k in ANCHOR_KINDS if k in evmap}


def build_anchors(
    events_a: list[dict[str, Any]], events_b: list[dict[str, Any]]
) -> tuple[list[Anchor], list[dict[str, Any]]]:
    """返回 (共同有序锚点, 问题列表)。

    顺序矛盾（时间不单调）的事件之后停止建立锚点；
    某侧缺失事件则该事件及之后都不参与相位对齐（仅保留共同前缀）。
    """
    ta_map = _ordered_anchor_times(_active_thermal_events(events_a))
    tb_map = _ordered_anchor_times(_active_thermal_events(events_b))
    issues: list[dict[str, Any]] = []

    last_a = -math.inf
    last_b = -math.inf
    anchors: list[Anchor] = []
    stop = False
    for kind in ANCHOR_KINDS:
        if stop:
            break
        in_a, in_b = kind in ta_map, kind in tb_map
        if not in_a or not in_b:
            if kind == "charge":
                issues.append({
                    "kind": kind, "severity": "error",
                    "reason": "至少一侧缺少下豆点，无法定义时间原点，相位对齐完全不可用",
                })
                return [], issues
            issues.append({
                "kind": kind, "severity": "warning",
                "reason": f"共同锚点仅到 {ANCHOR_LABEL[anchors[-1].kind] if anchors else '（无）'}："
                          f"A {'有' if in_a else '缺'}{ANCHOR_LABEL[kind]}，"
                          f"B {'有' if in_b else '缺'}{ANCHOR_LABEL[kind]}；"
                          "该事件之后不做拉伸，保留真实时间",
            })
            stop = True
            break
        xa, xb = ta_map[kind], tb_map[kind]
        # 顺序矛盾：时间必须严格递增（charge 允许为 0）
        if xa <= last_a or xb <= last_b:
            issues.append({
                "kind": kind, "severity": "error",
                "reason": f"{ANCHOR_LABEL[kind]} 顺序矛盾（A:{last_a:g}→{xa:g}, B:{last_b:g}→{xb:g}），"
                          "不强行拉伸；该事件及之后保持真实时间，请先修正或建立候选修订",
            })
            stop = True
            break
        anchors.append(Anchor(kind, xa, xb))
        last_a, last_b = xa, xb

    # 共同前缀之外的事件分类说明：仅一侧有 vs 双侧都有但被前段缺失/矛盾阻断
    for kind in ANCHOR_KINDS[len(anchors):]:
        in_a, in_b = kind in ta_map, kind in tb_map
        if in_a and in_b:
            issues.append({
                "kind": kind, "severity": "info",
                "reason": f"两批次都标记了{ANCHOR_LABEL[kind]}，但前段共同锚点已缺失或矛盾，"
                          "无法在不强行拉伸的前提下把它纳入对齐，故保持真实时间",
            })
        elif in_a or in_b:
            issues.append({
                "kind": kind, "severity": "info",
                "reason": f"{ANCHOR_LABEL[kind]} 仅一侧有标记（A有={in_a}, B有={in_b}），不参与对齐",
            })
    return anchors, issues


def canonical_map(anchors: list[Anchor]) -> dict[str, float]:
    """规范时间轴上各锚点的位置：取两批次锚点时间的均值（保留真实秒的量纲感）。"""
    return {a.kind: (a.ta + a.tb) / 2.0 for a in anchors}


def _piecewise_x(t: float, src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> float | None:
    """把源时间 t 按 (src,dst) 分段线性映射到目标坐标。t 必须落在 [首, 末] 内。"""
    if not src or t < src[0][1] - 1e-9 or t > src[-1][1] + 1e-9:
        return None
    for i in range(len(src) - 1):
        x0, x1 = src[i][1], src[i + 1][1]
        if x0 - 1e-9 <= t <= x1 + 1e-9:
            y0, y1 = dst[i][1], dst[i + 1][1]
            if x1 == x0:
                return y0
            f = (t - x0) / (x1 - x0)
            return y0 + f * (y1 - y0)
    return dst[-1][1]


def warp_points(
    points: list[Any],
    anchors: list[Anchor],
    canonical: dict[str, float],
    which: str,
) -> list[dict[str, Any]]:
    """把 [{t_s,v,origin,...}] 重映射到规范（相位）坐标。

    which='a' 用 anchors 的 ta，'b' 用 tb。只搬运，值不重算。
    落在共同锚点跨度之外的点被丢弃（该区域保持物理视图展示）。
    """
    src = []
    dst = []
    for a in anchors:
        src.append((a.kind, a.ta if which == "a" else a.tb))
        dst.append((a.kind, canonical[a.kind]))
    out: list[dict[str, Any]] = []
    for p in points:
        t = p["t_s"] if isinstance(p, dict) else p[0]
        v = p["v"] if isinstance(p, dict) else p[1]
        x = _piecewise_x(float(t), src, dst)
        if x is None:
            continue
        q = {"t_s": round(x, 2), "v": v, "src_t_s": round(float(t), 2)}
        if isinstance(p, dict) and p.get("origin"):
            q["origin"] = p["origin"]
        out.append(q)
    return out


def warp_xy(
    xy: list[list[float]],
    anchors: list[Anchor],
    canonical: dict[str, float],
    which: str,
) -> list[list[float]]:
    """把 [[t,v],...] 实测点重映射到规范坐标（用于温度实测系列）。"""
    src = []
    dst = []
    for a in anchors:
        src.append((a.kind, a.ta if which == "a" else a.tb))
        dst.append((a.kind, canonical[a.kind]))
    out = []
    for t, v in xy:
        x = _piecewise_x(float(t), src, dst)
        if x is not None:
            out.append([round(x, 2), v])
    return out


def aligned_segments(
    anchors: list[Anchor], canonical: dict[str, float]
) -> list[dict[str, Any]]:
    """逐段描述对齐：每段两批真实时长、规范时长、拉伸系数、阶段速度差。"""
    segs: list[dict[str, Any]] = []
    for i in range(len(anchors) - 1):
        a0, a1 = anchors[i], anchors[i + 1]
        da = a1.ta - a0.ta
        db = a1.tb - a0.tb
        cx0, cx1 = canonical[a0.kind], canonical[a1.kind]
        segs.append({
            "from": a0.kind, "to": a1.kind,
            "label": f"{ANCHOR_LABEL[a0.kind]}→{ANCHOR_LABEL[a1.kind]}",
            "canonical_start_s": round(cx0, 2),
            "canonical_end_s": round(cx1, 2),
            "a_duration_s": round(da, 2),
            "b_duration_s": round(db, 2),
            "duration_delta_s": round(da - db, 2),
            "a_stretch": round((cx1 - cx0) / da, 4) if da else None,
            "b_stretch": round((cx1 - cx0) / db, 4) if db else None,
            "aligned": True,
        })
    return segs


def comparability(batch_a: dict[str, Any], batch_b: dict[str, Any]) -> dict[str, Any]:
    """根据元数据判定哪些差异可比。温度水平对探针位置/系统偏差敏感。"""
    same_recipe = (batch_a.get("recipe") == batch_b.get("recipe"))
    mass_delta = abs(float(batch_a["charge_g"]) - float(batch_b["charge_g"]))
    same_probe = batch_a.get("probe_position") == batch_b.get("probe_position")
    same_offset = abs(float(batch_a.get("probe_offset_c", 0))
                      - float(batch_b.get("probe_offset_c", 0))) < 0.05

    temp_abs = same_probe and same_offset
    flags = {
        "same_recipe": same_recipe,
        "same_probe_position": same_probe,
        "same_probe_offset": same_offset,
        "charge_a_g": batch_a["charge_g"],
        "charge_b_g": batch_b["charge_g"],
        "charge_delta_g": round(mass_delta, 1),
        "probe_a": batch_a.get("probe_position"),
        "probe_b": batch_b.get("probe_position"),
        # RoR 是差分，对静态探针偏差不敏感；但探针位置不同（动态响应不同）仍需谨慎
        "absolute_temperature_comparable": temp_abs,
        "ror_shape_comparable": same_recipe,
        "phase_speed_comparable": same_recipe,
    }
    notes: list[str] = []
    if not same_recipe:
        notes.append("配方不同：任何温度/速度对齐仅作形态参考，不构成工艺等效")
    if not same_probe:
        notes.append(f"探针位置不同（A={flags['probe_a']}, B={flags['probe_b']}）："
                     "绝对温度水平不可直接比；请比较曲线形态与 RoR，勿比较具体℃")
    elif not same_offset:
        notes.append("两探针存在不同系统偏差：绝对温度差中混有读数偏差")
    if mass_delta >= 100:
        notes.append(f"锅量相差 {mass_delta:g}g：阶段时长/速度差异可能主要来自装料量，"
                     "分段对齐把它归一化到相同进度，便于看形态，但这恰恰说明不能视为工艺等效")
    flags["notes"] = notes
    fully = same_recipe and temp_abs and mass_delta < 100
    flags["headline"] = (
        "可比：相同物理时长/阶段进度下的绝对温度、RoR 形态与阶段时长"
        if fully
        else "部分可比（见明细）：模型对齐是可视化归一化，不等于工艺等效"
    )
    return flags


def _at_real_t(t: float, pts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """在 1s 网格派生素中按真实时间取该秒值（缺口/留空处返回 None）。"""
    ti = int(round(t))
    exact = [p for p in pts if p["t_s"] == ti]
    if exact:
        return exact[0]
    cand = [p for p in pts if abs(p["t_s"] - t) <= 1.0]
    return cand[0] if cand else None


def physical_table(
    detail_a: dict[str, Any], detail_b: dict[str, Any], every_s: int = 30
) -> list[dict[str, Any]]:
    """相同物理时长（相对各自下豆点同一真实秒）下的数值表。RoR 为真实时间差分结果。"""
    sm_a = detail_a["bean"].get("smoothed", [])
    sm_b = detail_b["bean"].get("smoothed", [])
    ror_a = detail_a["bean"].get("ror", [])
    ror_b = detail_b["bean"].get("ror", [])
    tmax = min(
        max((p["t_s"] for p in sm_a), default=0),
        max((p["t_s"] for p in sm_b), default=0),
    )
    rows = []
    for t in range(0, int(tmax) + 1, every_s):
        va = _at_real_t(t, sm_a)
        vb = _at_real_t(t, sm_b)
        ra = _at_real_t(t, ror_a)
        rb = _at_real_t(t, ror_b)
        rows.append({
            "real_t_s": t,
            "bean_a": va["v"] if va else None,
            "bean_b": vb["v"] if vb else None,
            "bean_delta": round(va["v"] - vb["v"], 3) if va and vb else None,
            "ror_a": ra["v"] if ra else None,
            "ror_b": rb["v"] if rb else None,
            "ror_delta": round(ra["v"] - rb["v"], 3) if ra and rb else None,
            "bean_origin_a": va["origin"] if va else "missing",
            "bean_origin_b": vb["origin"] if vb else "missing",
        })
    return rows


def phase_table(
    detail_a: dict[str, Any],
    detail_b: dict[str, Any],
    anchors: list[Anchor],
    canonical: dict[str, float],
    steps_per_segment: int = 4,
) -> list[dict[str, Any]]:
    """相同阶段进度下的数值表。

    在每条共同段内按百分比采样，把两侧真实时间对应的**已算好**平滑豆温与 RoR
    放在同一规范时刻。RoR 不在规范坐标上求导。
    """
    sm_a = detail_a["bean"].get("smoothed", [])
    sm_b = detail_b["bean"].get("smoothed", [])
    ror_a = detail_a["bean"].get("ror", [])
    ror_b = detail_b["bean"].get("ror", [])
    rows: list[dict[str, Any]] = []
    for i in range(len(anchors) - 1):
        a0, a1 = anchors[i], anchors[i + 1]
        for k in range(steps_per_segment + 1):
            f = k / steps_per_segment
            real_a = a0.ta + f * (a1.ta - a0.ta)
            real_b = a0.tb + f * (a1.tb - a0.tb)
            canon = canonical[a0.kind] + f * (canonical[a1.kind] - canonical[a0.kind])
            va = _at_real_t(real_a, sm_a)
            vb = _at_real_t(real_b, sm_b)
            ra = _at_real_t(real_a, ror_a)
            rb = _at_real_t(real_b, ror_b)
            rows.append({
                "segment": f"{ANCHOR_LABEL[a0.kind]}→{ANCHOR_LABEL[a1.kind]}",
                "phase": round(f, 3),
                "canonical_t_s": round(canon, 2),
                "real_t_a_s": round(real_a, 2),
                "real_t_b_s": round(real_b, 2),
                "bean_a": va["v"] if va else None,
                "bean_b": vb["v"] if vb else None,
                "bean_delta_at_same_progress": round(va["v"] - vb["v"], 3) if va and vb else None,
                "ror_a_real_time": ra["v"] if ra else None,
                "ror_b_real_time": rb["v"] if rb else None,
                "ror_delta": round(ra["v"] - rb["v"], 3) if ra and rb else None,
                "ror_note": "RoR 取自各自真实时间，未在变形轴上求导",
                "bean_origin_a": va["origin"] if va else "missing",
                "bean_origin_b": vb["origin"] if vb else "missing",
            })
    return rows


def build_phase_series(
    detail_a: dict[str, Any],
    detail_b: dict[str, Any],
    anchors: list[Anchor],
) -> dict[str, Any]:
    """生成相位对齐视图所需的派生系列（原始系列保持不动）。"""
    canonical = canonical_map(anchors)
    return {
        "canonical_anchors": [
            {"kind": a.kind, "label": a.label, "x_s": round(canonical[a.kind], 2),
             "real_t_a": round(a.ta, 2), "real_t_b": round(a.tb, 2)}
            for a in anchors
        ],
        "segments": aligned_segments(anchors, canonical),
        "bean_observed_a": warp_xy(detail_a["bean"].get("observed", []), anchors, canonical, "a"),
        "bean_observed_b": warp_xy(detail_b["bean"].get("observed", []), anchors, canonical, "b"),
        "bean_interp_a": warp_xy(detail_a["bean"].get("interpolated", []), anchors, canonical, "a"),
        "bean_interp_b": warp_xy(detail_b["bean"].get("interpolated", []), anchors, canonical, "b"),
        "env_observed_a": warp_xy(detail_a["env"].get("observed", []), anchors, canonical, "a"),
        "env_observed_b": warp_xy(detail_b["env"].get("observed", []), anchors, canonical, "b"),
        # RoR 仅位置重映射，数值与来源保持
        "ror_a": warp_points(detail_a["bean"].get("ror", []), anchors, canonical, "a"),
        "ror_b": warp_points(detail_b["bean"].get("ror", []), anchors, canonical, "b"),
        "smoothed_a": warp_points(detail_a["bean"].get("smoothed", []), anchors, canonical, "a"),
        "smoothed_b": warp_points(detail_b["bean"].get("smoothed", []), anchors, canonical, "b"),
        "canonical": canonical,
    }
