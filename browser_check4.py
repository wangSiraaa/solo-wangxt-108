"""第四阶段浏览器验证：阶段复盘标签。

覆盖：
- 批次详情三条合成标签（一爆/出豆/变黄），按事件筛选；
- 创建标签并保存事件版本快照；
- 修正事件后旧标签显示“当前事件已变更”，同时呈现原时间与当前时间；
- 删除标签后页面与接口同步、事件/修订不受影响；
- 事件编辑、批次导出、双批次比较仍正常。
"""
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


def tags_card(page):
    return page.locator(".card", has_text="阶段复盘标签").first


def tag_blocks(page):
    return page.evaluate(
        """()=>{const c=[...document.querySelectorAll('.card')].find(x=>x.textContent.includes('阶段复盘标签'));
        return c?[...c.querySelectorAll('.tag')].map(t=>t.innerText):[];}"""
    )


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = b.new_page(viewport={"width": 1500, "height": 1500})
        page.request.post("http://127.0.0.1:8000/api/admin/reseed?seed=7")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1600)  # 默认单批次=批次1

        check("批次详情显示标签面板", wait_body(page, "阶段复盘标签"))
        body = page.inner_text("body")
        for txt in ("一爆判断偏晚", "尾段火力过强", "回黄正常"):
            check(f"合成标签可见: {txt}", txt in body)
        # 初始均与当前事件一致
        blocks = tag_blocks(page)
        check("初始标签显示与当前事件一致", sum("与当前事件一致" in x for x in blocks) >= 3, str(len(blocks)))

        # 按事件筛选：点“变黄”
        tags_card(page).locator(".filters button", has_text="变黄").click()
        page.wait_for_timeout(500)
        blocks = tag_blocks(page)
        check("按变黄筛选只剩变黄标签", len(blocks) == 1 and "回黄正常" in blocks[0], str(blocks))
        tags_card(page).locator(".filters button", has_text="全部").nth(0).click()  # 事件行全部
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "15_tags_list.png"), full_page=True)

        # 创建一个回温点标签
        tags_card(page).locator("select").first.select_option("turnaround")
        tags_card(page).locator("input[placeholder*='一爆判断偏晚']").fill("回温偏慢")
        tags_card(page).locator("input[placeholder='简短说明（可选）']").fill("初火偏保守，回温到达偏慢。")
        tags_card(page).locator("button:has-text('添加标签')").click()
        page.wait_for_timeout(900)
        check("新标签出现在列表", wait_body(page, "回温偏慢"))
        api_tags = page.request.get("http://127.0.0.1:8000/api/batches/1/tags").json()
        new_tag = next(t for t in api_tags if t["label"] == "回温偏慢")
        check("新标签保存事件快照(回温82,auto)", new_tag["event_t_s"] == 82
              and new_tag["event_source"] == "auto" and new_tag["bound_event_id"], str(new_tag["event_t_s"]))
        new_tid = new_tag["id"]

        # ---- 修正一爆 575→603，观察一爆标签变为“当前事件已变更” ----
        fc = next(e["id"] for e in page.request.get(
            "http://127.0.0.1:8000/api/batches/1").json()["events"]
            if e["kind"] == "first_crack")
        r = page.request.put(f"http://127.0.0.1:8000/api/events/{fc}",
                             data={"new_t_s": 603, "reason": "标签功能验证修正", "revised_by": "zhao"},
                             headers={"content-type": "application/json"})
        check("事件修正成功", r.status == 200, str(r.status))
        # 通过 UI 触发详情刷新：切换批次离开再回来（只读操作）
        # 直接刷新当前页面数据：重新加载
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1600)

        body = page.inner_text("body")
        check("一爆标签提示当前事件已变更", "当前事件已变更" in body)
        check("同时显示原时间575与当前603", "575s" in body and "603s" in body)
        # 其他标签（出豆/变黄/回温）仍标记一致
        api_after = {t["event_kind"]: t for t in page.request.get(
            "http://127.0.0.1:8000/api/batches/1/tags").json()}
        check("一爆标签 changed=True", api_after["first_crack"]["changed"] is True)
        check("出豆/变黄标签未变",
              api_after["drop"]["changed"] is False and api_after["yellow"]["changed"] is False)
        check("一爆标签快照仍是575/auto",
              api_after["first_crack"]["event_t_s"] == 575
              and api_after["first_crack"]["event_source"] == "auto")
        page.screenshot(path=str(SHOTS / "16_tag_event_changed.png"), full_page=True)

        # ---- 删除标签：页面与接口同步，事件/修订不动 ----
        revs_before = page.request.get("http://127.0.0.1:8000/api/batches/1/events/revisions").json()
        events_before = page.request.get("http://127.0.0.1:8000/api/batches/1").json()["events"]
        # 用 confirm 对话框接受删除
        page.once("dialog", lambda d: d.accept())
        tags_card(page).locator(f".tag:has-text('回温偏慢') button:has-text('删除')").click()
        page.wait_for_timeout(900)
        check("删除后页面不再显示该标签", "回温偏慢" not in page.inner_text("body"))
        still = page.request.get("http://127.0.0.1:8000/api/batches/1/tags").json()
        check("删除后接口列表同步", all(t["id"] != new_tid for t in still), str(len(still)))
        events_after = page.request.get("http://127.0.0.1:8000/api/batches/1").json()["events"]
        revs_after = page.request.get("http://127.0.0.1:8000/api/batches/1/events/revisions").json()
        check("删除标签不影响事件",
              [(e["id"], e["t_s"], e["revoked_at"]) for e in events_after] ==
              [(e["id"], e["t_s"], e["revoked_at"]) for e in events_before])
        check("删除标签不影响修订历史", len(revs_after) == len(revs_before))

        # ---- 事件编辑功能仍可用（再次修正变黄）----
        yl = next(e["id"] for e in events_after if e["kind"] == "yellow")
        r = page.request.put(f"http://127.0.0.1:8000/api/events/{yl}",
                             data={"new_t_s": 305, "reason": "标签外的常规修正"},
                             headers={"content-type": "application/json"})
        check("事件编辑仍正常", r.status == 200, str(r.status))

        # ---- 导出包含标签且指标可复现 ----
        exp = page.request.get(
            "http://127.0.0.1:8000/api/batches/1/export?smooth_window_s=21&ror_window_s=45").json()
        tag_kinds = {t["event_kind"] for t in exp["event_tags"]}
        check("导出包含标签", {"first_crack", "drop", "yellow"} <= tag_kinds, str(tag_kinds))
        detail = page.request.get(
            "http://127.0.0.1:8000/api/batches/1?smooth_window_s=21&ror_window_s=45").json()
        check("导出指标与详情一致", exp["derived"]["metrics"] == detail["metrics"])

        # ---- 双批次比较不受标签影响 ----
        cmp = page.request.get(
            "http://127.0.0.1:8000/api/compare?a=1&b=2&alignment=phase"
            "&smooth_window_s=21&ror_window_s=45").json()
        check("比较仍五锚点", [a["kind"] for a in cmp["anchors"]][-1] == "drop")
        # 对齐/指标/可比性结构不被标签污染（批次详情 a/b 内可带 tags 供侧栏展示）
        check("标签不进入对齐与指标",
              "tags" not in cmp["anchors"] and "tags" not in cmp["comparability"]
              and "tags" not in cmp["a"]["metrics"] and "tags" not in cmp["phase"])

        b.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项标签浏览器检查通过 ===")
    if passed != len(results):
        for n, ok, d in results:
            if not ok:
                print("  失败:", n, d)
        sys.exit(1)


if __name__ == "__main__":
    main()
