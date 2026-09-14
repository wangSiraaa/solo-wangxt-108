"""分段对齐纯逻辑测试：共同前缀、缺失/矛盾不拉伸、RoR 不重求导、可比性。"""
from __future__ import annotations

from app import alignment as al


def _ev(**kw):
    base = {"revoked_at": None, "value": None, "source": "auto", "id": 1}
    base.update(kw)
    return base


def _events(times: dict[str, float]):
    return [_ev(kind=k, t_s=t) for k, t in times.items()]


STD = {"charge": 0, "turnaround": 80, "yellow": 300, "first_crack": 575, "drop": 700}


def test_full_anchors_five_segments():
    a = _events(STD)
    b = _events({"charge": 0, "turnaround": 86, "yellow": 306, "first_crack": 560, "drop": 680})
    anchors, issues = al.build_anchors(a, b)
    assert [x.kind for x in anchors] == al.ANCHOR_KINDS
    assert issues == []
    segs = al.aligned_segments(anchors, al.canonical_map(anchors))
    assert len(segs) == 4
    # 拉伸系数互为反向（B 该段更短则拉伸更大）
    assert segs[0]["a_duration_s"] == 80 and segs[0]["b_duration_s"] == 86


def test_missing_event_only_aligns_prefix_no_stretch():
    a = _events(STD)
    b = _events({"charge": 0, "turnaround": 96, "yellow": 342, "drop": 760})  # 缺一爆
    anchors, issues = al.build_anchors(a, b)
    assert [x.kind for x in anchors] == ["charge", "turnaround", "yellow"]
    fc_warn = [i for i in issues if i["kind"] == "first_crack" and i["severity"] == "warning"]
    assert fc_warn and "不做拉伸" in fc_warn[0]["reason"]
    # 两侧都有的出豆因前段缺失被阻断，不参与对齐
    assert any(i["kind"] == "drop" for i in issues)
    # 相位系列只覆盖到共同末锚点（变黄），不拉伸到出豆
    canonical = al.canonical_map(anchors)
    pts = [{"t_s": t, "v": 1.0} for t in range(0, 760)]
    warped_b = al.warp_points(pts, anchors, canonical, "b")
    assert warped_b[-1]["src_t_s"] == 342
    assert all(q["src_t_s"] <= 342 for q in warped_b)


def test_order_contradiction_stops_alignment():
    a = _events(STD)
    # B 回温点 86 但变黄 50（时间倒退）→ 在变黄处判定矛盾，锚点只到回温点
    b = _events({"charge": 0, "turnaround": 86, "yellow": 50, "first_crack": 560, "drop": 680})
    anchors, issues = al.build_anchors(a, b)
    assert [x.kind for x in anchors] == ["charge", "turnaround"]
    assert any(i["severity"] == "error" and i["kind"] == "yellow" for i in issues)


def test_revoked_events_ignored():
    a = _events(STD)
    b = _events({"charge": 0, "turnaround": 86, "yellow": 306, "first_crack": 560, "drop": 680})
    b.append(_ev(kind="first_crack", t_s=999, revoked_at="2026-01-01", source="manual"))
    anchors, _ = al.build_anchors(a, b)
    fc = [x for x in anchors if x.kind == "first_crack"][0]
    assert fc.tb == 560  # 作废版本不参与


def test_ror_is_relocated_not_recomputed():
    a = _events(STD)
    b = _events({"charge": 0, "turnaround": 100, "yellow": 320, "first_crack": 600, "drop": 720})
    anchors, _ = al.build_anchors(a, b)
    canonical = al.canonical_map(anchors)
    ror_b = [{"t_s": float(t), "v": 0.5 * t, "origin": "observed"} for t in range(0, 720)]
    warped = al.warp_points(ror_b, anchors, canonical, "b")
    # 每个相位点的 v 必须等于其真实源时间点的 v，且带来源
    for q in warped[::37]:
        src = next(p for p in ror_b if p["t_s"] == q["src_t_s"])
        assert q["v"] == src["v"]
        assert q["origin"] == "observed"
        assert q["t_s"] != q["src_t_s"] or q["t_s"] == 0  # 横坐标被移动（除原点）


def test_phase_vs_physical_tables_distinct():
    da = {"bean": {"smoothed": [{"t_s": t, "v": 100 + 0.1 * t, "origin": "observed"} for t in range(720)],
                   "ror": [{"t_s": t, "v": 6.0, "origin": "observed"} for t in range(720)]}}
    db = {"bean": {"smoothed": [{"t_s": t, "v": 110 + 0.1 * t, "origin": "observed"} for t in range(700)],
                   "ror": [{"t_s": t, "v": 5.0, "origin": "observed"} for t in range(700)]}}
    phys = al.physical_table(da, db, every_s=100)
    assert phys[1]["real_t_s"] == 100  # 同一真实秒
    anchors, _ = al.build_anchors(_events(STD),
                                  _events({"charge": 0, "turnaround": 86, "yellow": 306,
                                           "first_crack": 560, "drop": 680}))
    pt = al.phase_table(da, db, anchors, al.canonical_map(anchors))
    # 相位表同一行两侧真实时间不同（除非刚好进度相同），但规范时刻相同
    row = next(r for r in pt if r["segment"] == "变黄→一爆" and r["phase"] == 0.5)
    assert row["real_t_a_s"] != row["real_t_b_s"]
    assert "未在变形轴上求导" in row["ror_note"]


def test_comparability_flags():
    base = {"recipe": "R", "charge_g": 1200, "probe_position": "bean-bulk", "probe_offset_c": 0}
    same = dict(base)
    big = dict(base, charge_g=1600)
    wall = dict(base, probe_position="drum-wall", probe_offset_c=2)
    other = dict(base, recipe="X")

    f_same = al.comparability(base, same)
    assert f_same["absolute_temperature_comparable"] is True
    assert "可比" in f_same["headline"]

    f_big = al.comparability(base, big)
    assert f_big["absolute_temperature_comparable"] is True  # 探针相同
    assert f_big["charge_delta_g"] == 400 and any("锅量" in n for n in f_big["notes"])

    f_wall = al.comparability(base, wall)
    assert f_wall["absolute_temperature_comparable"] is False
    assert f_wall["ror_shape_comparable"] is True
    assert any("探针位置" in n for n in f_wall["notes"])

    f_other = al.comparability(base, other)
    assert f_other["ror_shape_comparable"] is False
