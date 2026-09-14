"""API + PostgreSQL 集成测试：播种、修正审计、原始数据不变性、导出可复现。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import services
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def seeded():
    from app.db import init_schema

    init_schema()
    services.reseed(seed=7)
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_synthetic_feed_has_noise_gaps_and_uniformity(client):
    rows = services.list_batches()
    assert len(rows) == 4  # A/B 基础、C 大锅量缺一爆、D 滚筒壁探针
    ids = [r["id"] for r in rows]

    for bid in ids:
        samples = services.load_samples(bid)
        assert all(s["is_missing"] == (s["bean_temp_c"] is None) for s in samples)
        n_missing = sum(s["is_missing"] for s in samples)
        assert n_missing > 0, "合成数据必须包含探针失联缺测"
        # 非均匀间隔
        dts = [samples[i + 1]["t_s"] - samples[i]["t_s"] for i in range(50)]
        assert len({round(d, 1) for d in dts}) > 3
        # 噪声：一阶差分不应完全恒定
        obs = [s["bean_temp_c"] for s in samples if s["bean_temp_c"] is not None]
        diffs = {round(obs[i + 1] - obs[i], 2) for i in range(30)}
        assert len(diffs) > 5

    r = client.get(f"/api/batches/{ids[0]}")
    assert r.status_code == 200
    detail = r.json()
    # 批次1的缺口较短（允许插值），批次2含 32s 大缺口 → 不插值
    gap_kinds_a = {(round(g["span_s"]), g["interpolated"]) for g in detail["bean"]["gaps"]}
    assert all(interp for _, interp in gap_kinds_a)

    r2 = client.get(f"/api/batches/{ids[1]}")
    gap_kinds_b = {g["interpolated"] for g in r2.json()["bean"]["gaps"]}
    assert False in gap_kinds_b


def test_changing_smooth_param_keeps_raw_identical(client):
    bid = services.list_batches()[0]["id"]
    samples_before = services.load_samples(bid)
    r1 = client.get(f"/api/batches/{bid}?smooth_window_s=15&ror_window_s=31").json()
    r2 = client.get(f"/api/batches/{bid}?smooth_window_s=55&ror_window_s=91").json()
    samples_after = services.load_samples(bid)
    assert samples_before == samples_after, "查询参数不得改写原始采样"
    assert r1["bean"]["observed"] == r2["bean"]["observed"]
    assert r1["params"]["smooth_window_s"] == 15
    assert r2["params"]["smooth_window_s"] == 55
    # 派生数据确实变了
    assert r1["bean"]["smoothed"] != r2["bean"]["smoothed"]


def test_manual_revision_keeps_source_history(client):
    bid = services.list_batches()[0]["id"]
    ev = next(
        e for e in services.load_events(bid) if e["kind"] == "first_crack"
    )
    old_t, old_src = ev["t_s"], ev["source"]

    r = client.put(
        f"/api/events/{ev['id']}",
        json={"new_t_s": old_t + 12, "reason": "听声复核，一爆偏晚", "revised_by": "zhang"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "manual"

    active = services.load_events(bid)
    fc = [e for e in active if e["kind"] == "first_crack"]
    assert len(fc) == 1 and abs(fc[0]["t_s"] - (old_t + 12)) < 1e-9

    revs = client.get(f"/api/batches/{bid}/events/revisions").json()
    last = revs[-1]
    assert last["old_t_s"] == old_t and last["old_source"] == old_src
    assert last["new_source"] == "manual" and last["revised_by"] == "zhang"

    # 作废事件仍在库中（include_revoked）
    all_ev = services.load_events(bid, include_revoked=True)
    revoked = [e for e in all_ev if e["kind"] == "first_crack" and e["revoked_at"]]
    assert len(revoked) == 1

    # 阶段指标按修正后的区间重算
    detail = client.get(f"/api/batches/{bid}").json()
    dev = next(p for p in detail["metrics"]["phases"] if p["key"] == "development")
    assert dev["interval"]["start_s"] == round(old_t + 12, 2)


def test_compare_endpoint(client):
    ids = [r["id"] for r in services.list_batches()]
    r = client.get(f"/api/compare?a={ids[0]}&b={ids[1]}&smooth_window_s=21&ror_window_s=45")
    assert r.status_code == 200
    data = r.json()
    assert data["a"]["batch"]["id"] != data["b"]["batch"]["id"]
    assert data["params"]["smooth_window_s"] == 21
    assert "因果" in data["note"]
    for side in ("a", "b"):
        ow = data[side]["operation_windows"]
        assert ow and all("因果" in x["interpretation"] for x in ow)


def test_export_reproduces_all_stage_metrics(client, tmp_path):
    bid = services.list_batches()[0]["id"]
    r = client.get(f"/api/batches/{bid}/export?smooth_window_s=21&ror_window_s=45")
    assert r.status_code == 200
    bundle = r.json()
    assert bundle["machine_connected"] is False
    assert Path(bundle["exported_file"]).exists()

    # 磁盘文件可被独立读取
    on_disk = json.loads(Path(bundle["exported_file"]).read_text(encoding="utf-8"))
    assert on_disk["raw_samples"][0]["t_s"] == 0.0

    # 用导出包中的原始采样 + 参数重算，必须逐点复现派生指标
    from app.analytics import ComputeParams, analyze_channel, phase_metrics

    c = bundle["compute"]
    params = ComputeParams(
        c["smooth_window_s"], c["ror_window_s"], c["max_interp_gap_s"]
    )
    bean = analyze_channel(bundle["raw_samples"], "bean_temp_c", params)
    m = phase_metrics(bean, bundle["events_active"])

    assert m == bundle["derived"]["metrics"]
    assert [p["t_s"] for p in bean["ror"]] == [
        p["t_s"] for p in bundle["derived"]["bean"]["ror"]
    ]
    assert [p["v"] for p in bean["ror"]] == [
        p["v"] for p in bundle["derived"]["bean"]["ror"]
    ]
    # 原始数据中的缺测以 NULL 语义保留
    assert any(s["bean_temp_c"] is None for s in bundle["raw_samples"])
