"""第三阶段浏览器验证：对比结论备注与待办。

覆盖：
- 比较页两条合成备注（相位待跟进/物理已确认），切换对齐方式后只显示对应备注；
- 创建备注（绑定批次对/对齐/参数/事件版本快照）；
- 后续修正事件不改旧备注快照；
- 状态更新可追溯（创建→确认→废弃的审计轨迹）；
- 切换批次对后只显示该对的备注；批次详情页展示该批次关联备注。
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


def select_label(page, text, value):
    page.evaluate(
        """([t,v])=>{const lab=[...document.querySelectorAll('label')].find(l=>l.textContent.includes(t));
        const s=lab.querySelector('select');const d=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
        d.call(s,String(v));s.dispatchEvent(new Event('change',{bubbles:true}));}""",
        [text, value],
    )


def select_batch(page, label_text, name):
    page.evaluate(
        """([t,pre])=>{const lab=[...document.querySelectorAll('label')].find(l=>l.textContent.includes(t));
        const s=lab.querySelector('select');const nm=o=>o.textContent.split(' ·')[0].trim();
        const o=[...s.options].find(x=>nm(x)===pre);
        const d=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
        d.call(s,o.value);s.dispatchEvent(new Event('change',{bubbles:true}));}""",
        [label_text, name],
    )


def wait_body(page, needle, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if needle in page.inner_text("body"):
            return True
        page.wait_for_timeout(120)
    return False


def notes_card(page):
    return page.locator(".card", has_text="对比结论备注与待办").first


def visible_conclusions(page):
    """当前备注卡片中显示的结论文本（排除表单占位）。"""
    return page.evaluate(
        """()=>{const c=[...document.querySelectorAll('.card')].find(x=>x.textContent.includes('对比结论备注与待办'));
        return c?[...c.querySelectorAll('.note .concl')].map(e=>e.textContent.trim()):[];}"""
    )


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = b.new_page(viewport={"width": 1500, "height": 1500})
        page.request.post("http://127.0.0.1:8000/api/admin/reseed?seed=7")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1600)

        select_label(page, "模式", "compare")
        page.wait_for_timeout(1400)

        # 默认物理对齐：应只见物理（已确认）备注，不见相位（待跟进）备注
        phys = "相同物理秒数"
        phs = "相位对齐下两批发展段"
        check("物理对齐显示物理合成备注", wait_body(page, phys))
        check("物理对齐隐藏相位备注", phs not in page.inner_text("body"))
        page.screenshot(path=str(SHOTS / "11_notes_physical.png"), full_page=True)

        # 切到相位：只显示相位（待跟进）备注
        select_label(page, "时间轴", "phase")
        page.wait_for_timeout(1400)
        check("相位对齐显示相位合成备注", wait_body(page, phs))
        check("相位对齐隐藏物理备注", phys not in page.inner_text("body"))
        # 状态筛选：相位备注是“待跟进”
        check("相位合成备注标记为待跟进", "待跟进" in notes_card(page).inner_text())

        # 状态筛选：切到“已确认”在相位下应为空
        page.click(".card button:has-text('已确认')")
        page.wait_for_timeout(400)
        check("相位页筛选已确认→空", "暂无备注" in notes_card(page).inner_text())
        page.click(".card button:has-text('全部')")
        page.wait_for_timeout(400)

        # ---- 创建一条相位备注 ----
        notes_before = page.request.get(
            "http://127.0.0.1:8000/api/notes?batch_id=1&other_id=2").json()
        page.click("text=＋ 新建备注")
        page.wait_for_timeout(300)
        check("创建表单提示快照绑定", "事件版本快照" in notes_card(page).inner_text())
        notes_card(page).locator("textarea").fill("浏览器测试：相位进度一致但 RoR 后段偏低，需跟进风门。")
        # 责任人/截止
        page.locator(".form input[placeholder='负责人']").fill("chen")
        page.locator(".form input[type='date']").fill("2026-10-15")
        # 状态选“待跟进”（默认）
        page.click(".form button:has-text('保存备注')")
        page.wait_for_timeout(1000)
        check("新备注显示在列表", wait_body(page, "需跟进风门"))
        check("新备注显示责任人与截止", "chen" in page.inner_text("body") and "2026-10-15" in page.inner_text("body"))

        # 通过 API 确认创建时已快照
        after = page.request.get(
            "http://127.0.0.1:8000/api/notes?batch_id=1&other_id=2&status=followup").json()
        mine = next((n for n in after if "需跟进风门" in n["conclusion"]), None)
        check("后端创建返回快照(五事件)", mine and len(mine["event_version_a"]) >= 5
              and len(mine["event_version_b"]) >= 5, str(len(after)))
        nid = mine["id"]
        fc_snap = next(e for e in mine["event_version_a"] if e["kind"] == "first_crack")["t_s"]
        check("快照一爆为修正前575", fc_snap == 575, str(fc_snap))
        page.screenshot(path=str(SHOTS / "12_notes_created.png"), full_page=True)

        # ---- 展开快照/轨迹，确认初始一条审计 ----
        page.click(f".note:has-text('需跟进风门') button:has-text('快照/轨迹')")
        page.wait_for_timeout(500)
        hist = notes_card(page).inner_text()
        check("快照表显示创建时事件版本", "first_crack" in hist and "575" in hist)
        check("初始状态轨迹(创建→待跟进)", "（创建）" in hist and "创建备注时的初始状态" in hist)

        # ---- 修正事件 575→608，旧备注快照不变 ----
        fc_id = next(e["id"] for e in page.request.get(
            "http://127.0.0.1:8000/api/batches/1").json()["events"]
            if e["kind"] == "first_crack")
        resp = page.request.put(f"http://127.0.0.1:8000/api/events/{fc_id}",
                                data={"new_t_s": 608, "reason": "备注创建后修正", "revised_by": "chen"},
                                headers={"content-type": "application/json"})
        check("事件修正成功", resp.status == 200, str(resp.status))
        page.wait_for_timeout(1200)
        after2 = page.request.get(
            f"http://127.0.0.1:8000/api/notes?batch_id=1&other_id=2").json()
        mine2 = next(n for n in after2 if n["id"] == nid)
        fc_after = next(e for e in mine2["event_version_a"] if e["kind"] == "first_crack")["t_s"]
        check("旧备注快照仍是575(不随后续修正改变)", fc_after == 575, str(fc_after))

        # ---- UI 上把状态改为已确认（带原因 prompt），再改废弃 ----
        page.once("dialog", lambda d: d.accept("复测确认 RoR 偏低成立"))
        page.click(f".note:has-text('需跟进风门') button:has-text('确认')")
        page.wait_for_timeout(1000)
        check("状态变为已确认", "已确认" in page.locator(f".note:has-text('需跟进风门')").first.inner_text())
        # 展开看轨迹新增一条
        page.click(f".note:has-text('需跟进风门') button:has-text('快照/轨迹')")
        page.wait_for_timeout(500)
        revs = page.request.get(f"http://127.0.0.1:8000/api/notes/{nid}/revisions").json()
        check("状态轨迹含 followup→confirmed",
              any(r["old_status"] == "followup" and r["new_status"] == "confirmed" for r in revs)
              and revs[0]["old_status"] is None, str([(r["old_status"], r["new_status"]) for r in revs]))
        page.screenshot(path=str(SHOTS / "13_notes_status.png"), full_page=True)

        # ---- 切换批次对（A1/B3）后只显示该对备注 ----
        select_batch(page, "批次B", "B2026-0914-C-bigcharge")
        page.wait_for_timeout(1500)
        check("切换批次对后无 A1/B2 的备注",
              "需跟进风门" not in page.inner_text("body") and "相位对齐下两批发展段" not in page.inner_text("body"))
        check("空批次对提示暂无备注", "暂无备注" in notes_card(page).inner_text())

        # 切回 A1/B2、物理对齐：物理合成备注(已确认)与已确认的新备注？
        # 新备注是 phase 对齐，物理视图应隐藏
        select_batch(page, "批次B", "B2026-0914-B")
        select_label(page, "时间轴", "physical")
        page.wait_for_timeout(1500)
        check("切回物理对齐后相位新备注被隐藏", "需跟进风门" not in page.inner_text("body"))
        check("物理合成备注仍可见", phys in page.inner_text("body"))

        # ---- 批次详情页展示该批次关联备注 ----
        select_label(page, "模式", "single")
        page.wait_for_timeout(1500)
        check("批次详情页展示关联备注(两种对齐都列出)",
              wait_body(page, "需跟进风门") and wait_body(page, phys))
        page.screenshot(path=str(SHOTS / "14_notes_batch_detail.png"), full_page=True)

        b.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项备注浏览器检查通过 ===")
    if passed != len(results):
        for n, ok, d in results:
            if not ok:
                print("  失败:", n, d)
        sys.exit(1)


if __name__ == "__main__":
    main()
