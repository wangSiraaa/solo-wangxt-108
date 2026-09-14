"""对齐/候选修订/比较报告的 API + PostgreSQL 集成测试。

样例：
- A↔C：同配方、锅量 1200→1600g，且 C 缺一爆 → 只前缀对齐、不拉伸、可比性部分；
- A↔D：探针位置不同(豆堆 vs 滚筒壁) → 绝对温度不可比、RoR 形态可比；
- A↔B：正常全锚点 → 四段相位对齐；
并验证候选修订接受前后行为、旧报告绑定旧事件版本。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import services
from app.main import app


@pytest.fixture(scope="module", autouse=True)
def seeded():
    from app.db import init_schema
    init_schema()
    services.reseed(seed=7)
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


P = {"smooth_window_s": 21, "ror_window_s": 45}


def _cmp(client, a, b, alignment="phase"):
    return client.get(f"/api/compare", params={"a": a, "b": b, "alignment": alignment, **P}).json()


def test_full_anchor_pair_has_four_phase_segments(client):
    d = _cmp(client, 1, 2)
    assert [x["kind"] for x in d["anchors"]] == \
           ["charge", "turnaround", "yellow", "first_crack", "drop"]
    assert d["anchor_issues"] == []
    assert len(d["phase"]["segments"]) == 4
    assert d["phase"]["available"] is True
    # 物理表与相位表都存在且语义不同
    assert d["physical_table"][5]["real_t_s"] == 150
    assert "phase_table" in d["phase"]


def test_missing_first_crack_aligns_only_prefix(client):
    d = _cmp(client, 1, 3)  # C 缺一爆
    kinds = [x["kind"] for x in d["anchors"]]
    assert kinds == ["charge", "turnaround", "yellow"]
    assert not any(x["kind"] in ("first_crack", "drop") for x in d["anchors"])
    assert any(i["kind"] == "first_crack" and i["severity"] == "warning"
               for i in d["anchor_issues"])
    # 相位系列只到变黄（A 300s 锚点），没有强行拉伸到出豆
    ror_a = d["phase"]["series"]["ror_a"]
    assert ror_a and max(q["src_t_s"] for q in ror_a) <= 300
    # 阶段速度差异没有被抹掉：段时长仍真实列出
    seg = d["phase"]["segments"][-1]
    assert seg["a_duration_s"] < seg["b_duration_s"]  # 大锅量更慢


def test_mass_change_is_partial_comparability_not_equivalence(client):
    d = _cmp(client, 1, 3)
    c = d["comparability"]
    assert c["charge_delta_g"] == 400
    assert "部分可比" in c["headline"]
    assert any("锅量" in n and "工艺等效" in n for n in c["notes"])
    assert "不构成工艺等效" in d["phase"]["not_equivalence"]


def test_probe_position_marks_absolute_temp_not_comparable(client):
    d = _cmp(client, 1, 4)  # D 滚筒壁探针 +2℃
    c = d["comparability"]
    assert c["same_probe_position"] is False
    assert c["absolute_temperature_comparable"] is False
    assert c["ror_shape_comparable"] is True
    assert any("探针位置" in n for n in c["notes"])
    # 探针不同不影响锚点建立（事件齐全）
    assert [x["kind"] for x in d["anchors"]][-1] == "drop"


def test_phase_ror_values_equal_real_time_ror(client):
    d = _cmp(client, 1, 4)
    # 用 (源时间,值) 集合做容差匹配，避免取整键碰撞
    real = [(p["t_s"], p["v"]) for p in d["b"]["bean"]["ror"]]
    for q in d["phase"]["series"]["ror_b"][::20]:
        best = min(real, key=lambda tv: abs(tv[0] - q["src_t_s"]))
        assert abs(best[0] - q["src_t_s"]) < 0.01 and best[1] == q["v"]
    assert "沿变形时间重新求导" in d["phase"]["ror_rule"]


def test_physical_alignment_keeps_real_time(client):
    d = _cmp(client, 1, 2, alignment="physical")
    assert "phase" not in d
    rows = d["physical_table"]
    # 同一真实秒两侧都取值；这是“相同物理时长”视角
    assert all(r["real_t_s"] % 30 == 0 for r in rows)


def test_candidate_does_not_change_events_until_accepted(client):
    before = [e["t_s"] for e in services.load_events(3) if e["kind"] == "first_crack"]
    assert before == []  # C 缺一爆
    r = client.post("/api/batches/3/candidates", json={
        "kind": "first_crack", "proposed_t_s": 642, "reason": "听声确认", "proposed_by": "li"})
    cid = r.json()["id"]
    # 仅候选存在，有效事件仍缺一爆 → 锚点仍只到变黄
    d = _cmp(client, 1, 3)
    assert [x["kind"] for x in d["anchors"]] == ["charge", "turnaround", "yellow"]

    r2 = client.post(f"/api/candidates/{cid}/decision", json={"accept": True, "decided_by": "li"})
    assert r2.json()["status"] == "accepted"
    # 接受后补成 manual 事件，锚点扩展
    d2 = _cmp(client, 1, 3)
    assert [x["kind"] for x in d2["anchors"]][-1] == "drop"
    ev = [e for e in services.load_events(3) if e["kind"] == "first_crack"]
    assert len(ev) == 1 and ev[0]["source"] == "manual" and ev[0]["t_s"] == 642
    # 已决定候选不能重复处理
    r3 = client.post(f"/api/candidates/{cid}/decision", json={"accept": True})
    assert r3.status_code == 409

    services.reseed(seed=7)  # 还原，避免污染后续


def test_reject_candidate_leaves_events_untouched(client):
    cid = client.post("/api/batches/3/candidates", json={
        "kind": "first_crack", "proposed_t_s": 642, "reason": "存疑"}).json()["id"]
    r = client.post(f"/api/candidates/{cid}/decision", json={"accept": False})
    assert r.json()["status"] == "rejected"
    assert [e["kind"] for e in services.load_events(3) if e["kind"] == "first_crack"] == []
    services.reseed(seed=7)


def test_report_binds_old_event_version(client):
    rid = client.post("/api/reports", json={
        "a": 1, "b": 2, "alignment": "phase", **P, "note": "基线"}).json()["report_id"]
    fc = next(e["id"] for e in services.load_events(1) if e["kind"] == "first_crack")
    client.put(f"/api/events/{fc}", json={"new_t_s": 600, "reason": "报告后修正"})

    rep = client.get(f"/api/reports/{rid}").json()
    bound_fc = [e for e in rep["event_version_a"] if e["kind"] == "first_crack"][0]
    assert bound_fc["t_s"] == 575  # 旧报告仍绑定 575
    seg = [s for s in rep["snapshot"]["phase"]["segments"] if s["from"] == "yellow"][0]
    assert seg["a_duration_s"] == 275  # 600-300 之前的旧时长
    assert "永久绑定" in rep["binding_warning"]

    # 实时视图已反映新值
    live = _cmp(client, 1, 2)
    assert [x for x in live["anchors"] if x["kind"] == "first_crack"][0]["t_a_s"] == 600
    services.reseed(seed=7)
