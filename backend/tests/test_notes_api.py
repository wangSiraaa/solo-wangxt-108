"""对比结论备注测试：创建快照、事件修正不改旧备注、状态流转可追溯、按批次/模式过滤。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import services
from app.db import init_schema
from app.main import app


@pytest.fixture(scope="module", autouse=True)
def seeded():
    init_schema()
    services.reseed(seed=7)
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


P = {"smooth_window_s": 21, "ror_window_s": 45}


def test_seed_two_notes_phase_and_physical(client):
    rows = client.get("/api/notes", params={"batch_id": 1, "other_id": 2}).json()
    assert len(rows) == 2
    aligns = {r["alignment"] for r in rows}
    assert aligns == {"phase", "physical"}
    statuses = {r["alignment"]: r["status"] for r in rows}
    assert statuses["phase"] == "followup"
    assert statuses["physical"] == "confirmed"
    # 两条都带事件版本快照
    assert all(r["event_version_a"] and r["event_version_b"] for r in rows)
    # 创建即有初始审计行
    rev = client.get(f"/api/notes/{rows[0]['id']}/revisions").json()
    assert rev[0]["old_status"] is None and rev[0]["new_status"] == rows[0]["status"]


def test_create_note_persists_snapshot(client):
    r = client.post("/api/notes", json={
        "a": 1, "b": 2, "alignment": "phase", **P,
        "conclusion": "新结论待跟进", "status": "followup",
        "owner": "zhao", "due_date": "2026-10-01",
    })
    assert r.status_code == 200, r.text
    n = r.json()
    assert n["owner"] == "zhao" and n["due_date"] == "2026-10-01"
    # 快照保存了当时五个事件
    kinds = {e["kind"] for e in n["event_version_a"]}
    assert {"charge", "turnaround", "yellow", "first_crack", "drop"} <= kinds
    # 锚点快照
    anchor_kinds = [a["kind"] for a in n["anchor_snapshot"]["anchors"]]
    assert anchor_kinds == ["charge", "turnaround", "yellow", "first_crack", "drop"]
    services.reseed(seed=7)


def test_later_event_revision_does_not_change_old_note_snapshot(client):
    nid = client.post("/api/notes", json={
        "a": 1, "b": 2, "alignment": "phase", **P,
        "conclusion": "快照绑定验证", "status": "confirmed",
    }).json()["id"]
    fc = next(e["id"] for e in services.load_events(1) if e["kind"] == "first_crack")
    client.put(f"/api/events/{fc}", json={"new_t_s": 612, "reason": "备注之后修正一爆"})

    old = next(x for x in client.get("/api/notes", params={"batch_id": 1}).json() if x["id"] == nid)
    snap_fc = [e for e in old["event_version_a"] if e["kind"] == "first_crack"][0]
    assert snap_fc["t_s"] == 575  # 旧备注快照保持创建时的值
    snap_anchor = [a for a in old["anchor_snapshot"]["anchors"] if a["kind"] == "first_crack"][0]
    assert snap_anchor["t_a_s"] == 575
    # 结论文本/状态不变
    assert old["status"] == "confirmed" and old["conclusion"] == "快照绑定验证"
    services.reseed(seed=7)


def test_status_change_is_traced(client):
    nid = client.post("/api/notes", json={
        "a": 1, "b": 2, "alignment": "physical", **P,
        "conclusion": "待确认结论", "status": "followup",
    }).json()["id"]
    r = client.put(f"/api/notes/{nid}/status",
                   json={"status": "confirmed", "reason": "现场复核", "changed_by": "li"})
    assert r.json()["status"] == "confirmed"
    r = client.put(f"/api/notes/{nid}/status",
                   json={"status": "discarded", "reason": "口径变化", "changed_by": "wang"})
    assert r.json()["status"] == "discarded"
    revs = client.get(f"/api/notes/{nid}/revisions").json()
    chain = [(x["old_status"], x["new_status"], x["reason"], x["changed_by"]) for x in revs]
    assert chain == [
        (None, "followup", "创建备注时的初始状态", "operator"),
        ("followup", "confirmed", "现场复核", "li"),
        ("confirmed", "discarded", "口径变化", "wang"),
    ]
    # 相同状态重复更新被拒绝
    dup = client.put(f"/api/notes/{nid}/status", json={"status": "discarded"})
    assert dup.status_code == 409
    services.reseed(seed=7)


def test_notes_filter_by_status_and_batch(client):
    # 播种的两条：phase=followup, physical=confirmed
    f = client.get("/api/notes", params={"batch_id": 1, "other_id": 2, "status": "followup"}).json()
    assert len(f) == 1 and f[0]["alignment"] == "phase"
    c = client.get("/api/notes", params={"batch_id": 1, "other_id": 2, "status": "confirmed"}).json()
    assert len(c) == 1 and c[0]["alignment"] == "physical"
    # 与批次 3 无关：空
    assert client.get("/api/notes", params={"batch_id": 3, "other_id": 4}).json() == []
    # 只给 batch_id 也能列出（批次详情页用），不分 A/B 方向
    assert len(client.get("/api/notes", params={"batch_id": 1}).json()) == 2


def test_compare_payload_includes_notes_for_pair(client):
    d = client.get("/api/compare", params={"a": 1, "b": 2, "alignment": "phase", **P}).json()
    assert {n["alignment"] for n in d["notes"]} == {"phase", "physical"}
    # 详情也带 notes
    assert len(client.get("/api/batches/1").json()["notes"]) == 2


def test_create_note_validation(client):
    r = client.post("/api/notes", json={"a": 1, "b": 2, "alignment": "weird", "conclusion": "x"})
    assert r.status_code == 422
    r = client.post("/api/notes", json={"a": 1, "b": 2, "conclusion": "   "})
    assert r.status_code == 422
    r = client.put("/api/notes/99999/status", json={"status": "confirmed"})
    assert r.status_code == 404
