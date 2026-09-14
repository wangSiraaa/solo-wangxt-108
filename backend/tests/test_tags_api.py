"""阶段复盘标签测试：快照绑定、修正后不变、按事件筛选、删除隔离、其他功能不受影响。"""
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


def test_seed_three_tags_cover_fc_drop_yellow(client):
    tags = client.get("/api/batches/1/tags").json()
    kinds = {t["event_kind"]: t for t in tags}
    assert set(kinds) == {"first_crack", "drop", "yellow"}
    # 三种处理状态
    statuses = {t["event_kind"]: t["status"] for t in tags}
    assert statuses == {"first_crack": "open", "drop": "adopted", "yellow": "ignored"}
    # 初始状态：与当前事件一致
    assert all(t["changed"] is False and t["current_state"] == "current" for t in tags)
    # 已采纳/已忽略带处理结论与处理人；待处理无决策
    assert kinds["drop"]["resolution"] and kinds["drop"]["resolved_by"]
    assert kinds["drop"]["last_decision"]["new_status"] == "adopted"
    assert kinds["first_crack"]["last_decision"] is None
    # 每条都有创建历史行
    assert all(any(r["old_status"] is None for r in t["status_history"]) for t in tags)
    # 绑定了具体事件版本与创建人/时间
    fc = kinds["first_crack"]
    assert fc["bound_event_id"] and fc["event_t_s"] == 575 and fc["event_source"] == "auto"
    assert fc["created_by"] == "li" and fc["created_at"]


def test_create_tag_saves_event_snapshot(client):
    r = client.post("/api/batches/1/tags", json={
        "event_kind": "turnaround", "label": "回温偏慢",
        "description": "回温点到达偏慢，初火偏保守", "created_by": "zhao",
    })
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["event_t_s"] == 82 and t["bound_event_id"] and t["event_source"] == "auto"
    # 新建标签默认待处理，并有初始历史
    assert t["status"] == "open"
    assert t["status_history"][0]["old_status"] is None and t["status_history"][0]["new_status"] == "open"
    # 批次详情内联返回标签
    assert any(x["id"] == t["id"] for x in client.get("/api/batches/1").json()["tags"])
    services.reseed(seed=7)


def test_revising_event_keeps_old_tag_snapshot_and_marks_changed(client):
    t0 = next(t for t in client.get("/api/batches/1/tags").json()
              if t["event_kind"] == "first_crack")
    assert t0["event_t_s"] == 575
    n_rev_before = len(client.get("/api/batches/1/events/revisions").json())

    fc = next(e["id"] for e in services.load_events(1) if e["kind"] == "first_crack")
    client.put(f"/api/events/{fc}", json={"new_t_s": 601, "reason": "标签后修正"})

    tags = client.get("/api/batches/1/tags").json()
    tag = next(t for t in tags if t["id"] == t0["id"])
    # 旧标签快照不变
    assert tag["event_t_s"] == 575 and tag["bound_event_id"] == t0["bound_event_id"]
    assert tag["event_source"] == "auto"
    # 明确标记当前事件已变更，并给出当前值
    assert tag["changed"] is True and tag["current_state"] == "changed"
    assert tag["current_event"]["t_s"] == 601 and tag["current_event"]["source"] == "manual"
    assert tag["current_event"]["id"] != tag["bound_event_id"]
    # 事件修订审计仍正常（标签逻辑不影响修订）
    revs = client.get("/api/batches/1/events/revisions").json()
    assert len(revs) == n_rev_before + 1
    services.reseed(seed=7)


def test_filter_tags_by_event(client):
    tags = client.get("/api/batches/1/tags", params={"event_kind": "yellow"}).json()
    assert [t["event_kind"] for t in tags] == ["yellow"]
    assert client.get("/api/batches/2/tags").json() == []  # 批次2无标签


def test_delete_tag_only_removes_tag(client):
    tid = next(t for t in client.get("/api/batches/1/tags").json()
               if t["event_kind"] == "drop")["id"]
    events_before = services.load_events(1)
    revs_before = client.get("/api/batches/1/events/revisions").json()

    r = client.delete(f"/api/batches/1/tags/{tid}")
    assert r.status_code == 200
    assert r.json()["events_unchanged"] is True

    # 接口列表与批次详情同步更新
    assert all(t["id"] != tid for t in client.get("/api/batches/1/tags").json())
    assert all(t["id"] != tid for t in client.get("/api/batches/1").json()["tags"])
    # 事件与历史修订完全不受影响
    assert services.load_events(1) == events_before
    assert client.get("/api/batches/1/events/revisions").json() == revs_before
    # 删不存在的标签
    assert client.delete(f"/api/batches/1/tags/{tid}").status_code == 404
    services.reseed(seed=7)


def test_tag_validation_and_missing_event(client):
    r = client.post("/api/batches/1/tags", json={"event_kind": "nope", "label": "x"})
    assert r.status_code == 422
    r = client.post("/api/batches/1/tags", json={"event_kind": "yellow", "label": "  "})
    assert r.status_code == 422
    # 批次3缺一爆：不能给不存在的已确认事件打标签
    r = client.post("/api/batches/3/tags", json={"event_kind": "first_crack", "label": "x"})
    assert r.status_code == 409


def test_tags_do_not_affect_export_and_compare(client):
    detail = client.get("/api/batches/1", params=P).json()
    exp = client.get("/api/batches/1/export", params=P).json()
    # 导出包含标签（含处理状态与历史），且原始采样/指标不受标签影响
    tags = exp["event_tags"]
    assert {t["event_kind"] for t in tags} == {"first_crack", "drop", "yellow"}
    by_kind = {t["event_kind"]: t for t in tags}
    assert by_kind["drop"]["status"] == "adopted" and by_kind["drop"]["last_decision"]
    assert by_kind["first_crack"]["status"] == "open"
    assert all("status_history" in t for t in tags)
    assert exp["derived"]["metrics"] == detail["metrics"]
    # 比较结果与无标签时一致（标签不进入对齐/指标）
    cmp1 = client.get("/api/compare", params={"a": 1, "b": 2, **P}).json()
    assert [a["kind"] for a in cmp1["anchors"]] == \
           ["charge", "turnaround", "yellow", "first_crack", "drop"]
    assert cmp1["a"]["metrics"]["development_ratio"] == detail["metrics"]["development_ratio"]


# ---------------- 处理闭环 ----------------

def test_status_lifecycle_is_traced(client):
    tid = next(t["id"] for t in client.get("/api/batches/1/tags").json()
               if t["event_kind"] == "first_crack")  # open
    # open -> adopted
    r = client.put(f"/api/batches/1/tags/{tid}/status",
                   json={"status": "adopted", "resolution": "下批提前 10s 观察一爆", "changed_by": "zhao"})
    assert r.json()["status"] == "adopted"
    assert r.json()["resolved_by"] == "zhao" and r.json()["resolution"]
    # adopted -> ignored
    r = client.put(f"/api/batches/1/tags/{tid}/status",
                   json={"status": "ignored", "resolution": "复测证伪", "changed_by": "li"})
    assert r.json()["status"] == "ignored"
    # ignored -> open（重新打开待处理）
    r = client.put(f"/api/batches/1/tags/{tid}/status",
                   json={"status": "open", "resolution": "再次出现，重开", "changed_by": "li"})
    assert r.json()["status"] == "open"

    revs = client.get(f"/api/batches/1/tags/{tid}/revisions").json()
    chain = [(x["old_status"], x["new_status"], x["changed_by"]) for x in revs]
    assert chain == [
        (None, "open", "li"),          # 创建行（种子）
        ("open", "adopted", "zhao"),
        ("adopted", "ignored", "li"),
        ("ignored", "open", "li"),
    ]
    # 重复同状态被拒绝
    dup = client.put(f"/api/batches/1/tags/{tid}/status", json={"status": "open"})
    assert dup.status_code == 409
    # 非法状态
    bad = client.put(f"/api/batches/1/tags/{tid}/status", json={"status": "nope"})
    assert bad.status_code in (422, 409)
    services.reseed(seed=7)


def test_event_revision_preserves_tag_snapshot_and_processing_history(client):
    tid = next(t["id"] for t in client.get("/api/batches/1/tags").json()
               if t["event_kind"] == "drop")  # adopted
    before = client.get(f"/api/batches/1/tags/{tid}/revisions").json()
    fc = next(e["id"] for e in services.load_events(1) if e["kind"] == "drop")
    client.put(f"/api/events/{fc}", json={"new_t_s": 690, "reason": "标签闭环后修正出豆"})

    tag = next(t for t in client.get("/api/batches/1/tags").json() if t["id"] == tid)
    # 事件快照与处理结论/历史都不被事件修正影响
    assert tag["event_t_s"] == 700 and tag["bound_event_id"]
    assert tag["changed"] is True and tag["current_event"]["t_s"] == 690
    assert tag["status"] == "adopted" and tag["resolution"]
    after = client.get(f"/api/batches/1/tags/{tid}/revisions").json()
    assert after == before  # 处理历史完全不变
    services.reseed(seed=7)


def test_status_filter(client):
    def kinds(status):
        return {t["event_kind"] for t in client.get(
            "/api/batches/1/tags", params={"status": status}).json()}
    assert kinds("open") == {"first_crack"}
    assert kinds("adopted") == {"drop"}
    assert kinds("ignored") == {"yellow"}
    # 事件 + 状态组合过滤
    r = client.get("/api/batches/1/tags",
                   params={"event_kind": "drop", "status": "adopted"}).json()
    assert len(r) == 1 and r[0]["event_kind"] == "drop"
    assert client.get("/api/batches/1/tags", params={"event_kind": "drop", "status": "open"}).json() == []


def test_delete_tag_cascades_history_only(client):
    tid = next(t["id"] for t in client.get("/api/batches/1/tags").json()
               if t["event_kind"] == "drop")  # adopted，带处理历史
    assert len(client.get(f"/api/batches/1/tags/{tid}/revisions").json()) >= 2
    events_before = services.load_events(1)
    ev_revs_before = client.get("/api/batches/1/events/revisions").json()
    notes_before = client.get("/api/notes", params={"batch_id": 1}).json()

    r = client.delete(f"/api/batches/1/tags/{tid}")
    assert r.status_code == 200 and r.json()["tag_history_deleted"] is True
    # 标签与其处理历史一并消失
    assert all(t["id"] != tid for t in client.get("/api/batches/1/tags").json())
    assert client.get(f"/api/batches/1/tags/{tid}/revisions").status_code == 404
    # 事件、事件修订、备注均不受影响
    assert services.load_events(1) == events_before
    assert client.get("/api/batches/1/events/revisions").json() == ev_revs_before
    assert client.get("/api/notes", params={"batch_id": 1}).json() == notes_before
    # 比较不受影响
    cmp1 = client.get("/api/compare", params={"a": 1, "b": 2, **P}).json()
    assert cmp1["anchors"] and cmp1["a"]["metrics"]
    services.reseed(seed=7)


def test_detail_and_export_carry_current_status_and_history(client):
    detail = client.get("/api/batches/1").json()
    for t in detail["tags"]:
        assert t["status"] in services.TAG_STATUSES and "status_history" in t
    exp = client.get("/api/batches/1/export").json()
    assert len(exp["event_tags"]) == 3
    assert all({"status", "resolution", "resolved_by", "resolved_at", "status_history"} <= set(t)
               for t in exp["event_tags"])
