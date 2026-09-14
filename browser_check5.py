"""第五阶段浏览器验证：标签处理闭环（待处理/已采纳/已忽略）。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
SHOTS = Path("/workspace/browser_shots")
os.environ.setdefault("LD_LIBRARY_PATH", f"/workspace/miniforge3/envs/roast/lib:{os.environ.get('LD_LIBRARY_PATH', '')}")
os.environ.setdefault("FONTCONFIG_FILE", "/workspace/miniforge3/envs/roast/etc/fonts/fonts.conf")

results = []


def check(n, cond, detail=""):
    results.append((n, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {n} {detail}")


def wait_body(page, needle, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if needle in page.inner_text("body"):
            return True
        page.wait_for_timeout(120)
    return False


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = b.new_page(viewport={"width": 1500, "height": 1500})

        def api(path, method="GET", data=None):
            url = "http://127.0.0.1:8000" + path
            if data is not None:
                return page.request.fetch(
                    url, method=method, data=data,
                    headers={"content-type": "application/json"})
            return page.request.fetch(url, method=method)

        api("/api/admin/reseed?seed=7", "POST")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1700)

        c = page.locator(".card", has_text="阶段复盘标签").first
        check("标签面板出现", wait_body(page, "阶段复盘标签"))
        body = page.inner_text("body")
        for s in ("待处理", "已采纳", "已忽略"):
            check(f"合成数据含状态徽标 {s}", s in body)
        check("已采纳标签显示处理结论", "下一批出豆前" in body)
        check("已忽略标签显示处理结论", "无需动作" in body)
        check("待处理标签显示尚未处理", "尚未处理" in body)
        page.screenshot(path=str(SHOTS / "17_tag_closedloop.png"), full_page=True)

        # ---- 按处理状态筛选 ----
        c.locator(".filters button", has_text="已采纳").click()
        page.wait_for_timeout(400)
        blocks = c.locator(".tag").all_inner_texts()
        check("状态筛选=已采纳只剩1条", len(blocks) == 1 and "尾段火力过强" in blocks[0], str(len(blocks)))
        c.locator(".filters button", has_text="待处理").click()
        page.wait_for_timeout(400)
        blocks = c.locator(".tag").all_inner_texts()
        check("状态筛选=待处理只显示一爆标签", len(blocks) == 1 and "一爆判断偏晚" in blocks[0])
        # 事件=变黄 + 状态=待处理 → 空
        c.locator(".filters button", has_text="全部").nth(1).click()  # 状态行“全部”
        page.wait_for_timeout(300)
        c.locator(".filters button", has_text="变黄").click()
        page.wait_for_timeout(400)
        c.locator(".filters button", has_text="待处理").click()
        page.wait_for_timeout(400)
        check("事件=变黄+状态=待处理→空", "该筛选下暂无标签" in c.inner_text())
        # 复位：事件行全部(第一个)、状态行全部(第二个)
        c.locator(".filters button", has_text="全部").nth(0).click()
        c.locator(".filters button", has_text="全部").nth(1).click()
        page.wait_for_timeout(400)
        check("复位后三条标签都在", c.locator(".tag").count() == 3, str(c.locator(".tag").count()))

        # ---- 把待处理的一爆标记为已采纳 ----
        fc_tag = c.locator(".tag", has_text="一爆判断偏晚")
        page.once("dialog", lambda d: d.accept("下批一爆前提前 10s 观察，已采纳"))
        fc_tag.locator(".actions .statebtn", has_text="已采纳").click()
        page.wait_for_timeout(1000)
        check("一爆标签状态变已采纳(徽标)",
              fc_tag.locator(".statbadge.adopted").count() == 1)
        check("卡片显示处理结论", "下批一爆前提前 10s 观察" in fc_tag.inner_text())
        check("显示最近处理信息", "最近处理" in fc_tag.inner_text())
        fc_tag.locator("button", has_text="完整历史").click()
        page.wait_for_timeout(500)
        hist = fc_tag.locator(".history").inner_text()
        check("历史含创建与 待处理→已采纳",
              "（创建）" in hist and "待处理" in hist and "已采纳" in hist
              and "下批一爆前提前" in hist)

        tags = api("/api/batches/1/tags").json()
        t_fc = next(t for t in tags if t["event_kind"] == "first_crack")
        tid = t_fc["id"]
        chain = [(r["old_status"], r["new_status"]) for r in t_fc["status_history"]]
        check("处理历史可追溯(创建+采纳)",
              chain == [(None, "open"), ("open", "adopted")], str(chain))
        check("采纳后有处理人和时间", t_fc["resolved_by"] == "operator" and t_fc["resolved_at"])

        # ---- 再改已忽略，然后修正事件：快照与历史不变 ----
        page.once("dialog", lambda d: d.accept("改判：本批噪声干扰，结论忽略"))
        fc_tag.locator(".actions .statebtn", has_text="已忽略").click()
        page.wait_for_timeout(900)
        history_before = api(f"/api/batches/1/tags/{tid}/revisions").json()
        chain_before = [(h["old_status"], h["new_status"]) for h in history_before]
        fc_ev = next(e["id"] for e in api("/api/batches/1").json()["events"]
                     if e["kind"] == "first_crack")
        r = api(f"/api/events/{fc_ev}", "PUT", {"new_t_s": 602, "reason": "闭环后修正", "revised_by": "x"})
        check("事件修正成功", r.status == 200)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1700)

        tags2 = {t["event_kind"]: t for t in api("/api/batches/1/tags").json()}
        t2 = tags2["first_crack"]
        check("事件修正后标签快照仍575", t2["event_t_s"] == 575 and t2["bound_event_id"])
        check("标签检测到当前事件已变更", t2["changed"] is True and t2["current_event"]["t_s"] == 602)
        check("处理状态与历史不被事件修正改变",
              t2["status"] == "ignored" and
              [(h["old_status"], h["new_status"]) for h in t2["status_history"]] == chain_before)
        page.screenshot(path=str(SHOTS / "18_tag_status_history.png"), full_page=True)

        # ---- 导出包含处理状态与历史 ----
        exp = api("/api/batches/1/export?smooth_window_s=21&ror_window_s=45").json()
        et = {t["event_kind"]: t for t in exp["event_tags"]}
        check("导出含已采纳与已忽略状态",
              et["drop"]["status"] == "adopted" and et["first_crack"]["status"] == "ignored")
        # 一爆标签历史覆盖 待处理→已采纳→已忽略 三态
        fc_states = {(h["old_status"], h["new_status"]) for h in et["first_crack"]["status_history"]}
        check("导出历史覆盖三种处理状态",
              ("open", "adopted") in fc_states and ("adopted", "ignored") in fc_states,
              str(et["first_crack"]["status"]))
        check("导出含处理历史", all(len(t["status_history"]) >= 1 for t in exp["event_tags"]))
        check("导出含处理结论字段", et["drop"]["resolution"] and et["drop"]["resolved_by"])

        # ---- 删除标签：级联其处理历史 ----
        notes_before = api("/api/notes?batch_id=1").json()
        ev_before = api("/api/batches/1").json()["events"]
        dtag = next(t for t in api("/api/batches/1/tags").json() if t["event_kind"] == "yellow")
        r = api(f"/api/batches/1/tags/{dtag['id']}", "DELETE")
        check("删除返回成功并声明级联", r.status == 200 and r.json()["tag_history_deleted"] is True)
        check("删除后处理历史接口404",
              api(f"/api/batches/1/tags/{dtag['id']}/revisions").status == 404)
        check("删除后列表同步",
              all(t["event_kind"] != "yellow" for t in api("/api/batches/1/tags").json()))
        ev_after = api("/api/batches/1").json()["events"]
        check("事件不受影响",
              [(e["id"], e["t_s"]) for e in ev_after] == [(e["id"], e["t_s"]) for e in ev_before])
        notes_after = api("/api/notes?batch_id=1").json()
        check("备注功能不受影响", notes_after == notes_before and len(notes_after) >= 2)
        cmp = api("/api/compare?a=1&b=2&alignment=phase").json()
        check("批次比较不受影响", [a["kind"] for a in cmp["anchors"]][-1] == "drop")

        b.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项标签闭环浏览器检查通过 ===")
    if passed != len(results):
        for n, ok, d in results:
            if not ok:
                print("  失败:", n, d)
        sys.exit(1)


if __name__ == "__main__":
    main()
