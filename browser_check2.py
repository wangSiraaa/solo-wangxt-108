"""第二阶段浏览器交互验证：分段相位对齐、候选修订、比较报告版本绑定。

真实 Chromium 操作：切换物理/相位对齐、切换到缺一爆/大锅量/壁探针批次、
建立并接受候选修订、保存报告、报告后再修正事件、回看旧报告快照。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
SHOTS = Path("/workspace/browser_shots")
SHOTS.mkdir(exist_ok=True)
os.environ.setdefault("LD_LIBRARY_PATH", f"/workspace/miniforge3/envs/roast/lib:{os.environ.get('LD_LIBRARY_PATH', '')}")
os.environ.setdefault("FONTCONFIG_FILE", "/workspace/miniforge3/envs/roast/etc/fonts/fonts.conf")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def select_label(page, text, value):
    page.evaluate(
        """([t,v])=>{const lab=[...document.querySelectorAll('label')].find(l=>l.textContent.includes(t));
        const s=lab.querySelector('select');const d=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;
        d.call(s,String(v));s.dispatchEvent(new Event('change',{bubbles:true}));}""",
        [text, value],
    )


def select_option_text(page, label_text, name, exact=False):
    """选择下拉项。exact=True 时按 ' ·' 前的批次名精确匹配。"""
    page.evaluate(
        """([t,frag,exact])=>{const lab=[...document.querySelectorAll('label')].find(l=>l.textContent.includes(t));
        const s=lab.querySelector('select');
        const nm=o=>o.textContent.split(' ·')[0].trim();
        const o=exact?[...s.options].find(x=>nm(x)===frag)
                   :[...s.options].find(x=>x.textContent.includes(frag));
        const d=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;
        d.call(s,o.value);s.dispatchEvent(new Event('change',{bubbles:true}));}""",
        [label_text, name, exact],
    )


def canvas_nonwhite(page):
    return page.evaluate(
        """()=>{const c=document.querySelector('canvas');const cv=document.createElement('canvas');
        cv.width=c.width;cv.height=c.height;cv.getContext('2d').drawImage(c,0,0);
        const d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;let n=0;
        for(let i=0;i<d.length;i+=4){if(d[i+3]>40&&(d[i]<235||d[i+1]<235||d[i+2]<235))n++;}return n;}"""
    )


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1500, "height": 1400})
        page.request.post("http://127.0.0.1:8000/api/admin/reseed?seed=7")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1600)

        # 进入对比模式 A=1 B=2，默认物理对齐
        select_label(page, "模式", "compare")
        page.wait_for_timeout(1400)
        body = page.inner_text("body")
        check("物理对齐显示可比性面板", "可比性判定" in body)
        check("同探针同配方→完全可比", "可比：相同物理时长" in body)
        check("物理对齐无锚点问题", "五个热事件双侧齐全" in body)
        check("物理对齐横轴为真实秒数",
              page.evaluate("()=>window.__roastChart.getOption().xAxis.map(x=>x.name).join('|')").count("相对下豆点") == 2)
        check("物理对齐画布已绘制", canvas_nonwhite(page) > 3000, str(canvas_nonwhite(page)))
        page.screenshot(path=str(SHOTS / "6_compare_physical.png"), full_page=True)

        # 切到相位对齐 A/B 全锚点
        select_label(page, "时间轴", "phase")
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("相位图提示规范阶段时间轴", "规范阶段时间" in body)
        check("RoR 明确未沿变形轴求导", "未沿变形轴重新求导" in body)
        check("展示四段真实时长", body.count("→") >= 4 and "下豆→回温点" in body)
        check("非工艺等效声明", "不构成工艺等效" in body or "不等于工艺等效" in body)
        # 相位图 ECharts 系列名
        ph_names = page.evaluate(
            "()=>window.__roastChart?window.__roastChart.getOption().series.map(s=>s.name):[]")
        # PhaseChart 也挂 __roastChart（后挂载者覆盖）；至少应有 RoR 真实时间值系列
        check("相位图 RoR 标注真实时间值", any("真实时间值" in n for n in ph_names), str(ph_names))
        page.screenshot(path=str(SHOTS / "7_compare_phase.png"), full_page=True)

        # B 换成批次3：大锅量 + 缺一爆
        select_option_text(page, "批次B", "B2026-0914-C-bigcharge", exact=True)
        page.wait_for_timeout(1600)
        body = page.inner_text("body")
        check("缺一爆→警告只对齐到变黄", "共同锚点仅到" in body and "变黄" in body)
        check("缺测尾段不强行拉伸", "不做拉伸" in body or "保持真实时间" in body)
        check("锅量差400g→部分可比/非等效", "400" in body and ("部分可比" in body or "工艺等效" in body))
        # 相位分段表：通过表头“阶段/A 真实 s”唯一定位
        seg_rows = page.evaluate(
            """()=>{const tbl=[...document.querySelectorAll('table')].find(t=>{
            const h=t.querySelector('thead');return h&&h.textContent.includes('A 真实')&&h.textContent.includes('B 真实');});
            return tbl?[...tbl.querySelectorAll('tbody tr')].map(r=>r.children[0].textContent.trim()):[];}""")
        check("共同前缀仅两段(下豆→回温点,回温点→变黄)",
              seg_rows == ["下豆→回温点", "回温点→变黄"], str(seg_rows))
        page.screenshot(path=str(SHOTS / "8_missing_fc_prefix.png"), full_page=True)

        # B 换成批次4：壁探针
        select_option_text(page, "批次B", "B2026-0914-D-wallprobe", exact=True)
        page.wait_for_timeout(1600)
        body = page.inner_text("body")
        check("探针位置不同→绝对温度不可比", "不可直接比" in body and "探针位置" in body)
        check("壁探针下 RoR 形态仍可参考", "可参考" in body)
        check("事件齐全仍五锚点", "五个热事件双侧齐全" in body)
        page.screenshot(path=str(SHOTS / "9_probe_position.png"), full_page=True)

        # ---- 候选修订：回到批次3单批次 ----
        select_label(page, "模式", "single")
        page.wait_for_timeout(600)
        select_option_text(page, "批次A", "B2026-0914-C-bigcharge", exact=True)
        page.wait_for_timeout(1400)
        body = page.inner_text("body")
        check("单批次页有候选修订面板", "候选修订" in body)
        # 建立候选
        page.locator(".stack .card", has_text="候选修订").locator("select").first.select_option("first_crack")
        page.fill(".stack input[placeholder='建议时间 s']", "642")
        page.fill(".stack input[placeholder='依据/原因']", "听声确认一爆")
        page.click("text=建立候选")
        page.wait_for_timeout(800)
        body = page.inner_text("body")
        check("候选出现在待确认列表", "642" in body and "听声确认一爆" in body)
        # 接受候选（处理 alert）
        page.once("dialog", lambda d: d.accept())
        page.click(".stack button:has-text('接受')")
        page.wait_for_timeout(1200)
        body = page.inner_text("body")
        check("接受后候选移入已处理", "已接受" in body)
        # 事件表出现 manual 一爆
        fc_row = page.evaluate(
            """()=>{const card=[...document.querySelectorAll('.card')].find(c=>c.textContent.includes('人工新增操作标记'));
            const tr=[...card.querySelectorAll('tbody tr')].find(r=>r.children[0].textContent.trim()==='一爆');
            return tr?[...tr.querySelectorAll('td')].map(td=>td.textContent.trim()):null;}""")
        check("接受候选落成人工一爆事件", fc_row and fc_row[1] == "642" and fc_row[3] == "人工", str(fc_row))

        # 再回对比 A=1 B=3 相位 → 锚点扩展到五锚
        select_label(page, "模式", "compare")
        page.wait_for_timeout(800)
        select_label(page, "时间轴", "phase")
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("接受候选后可全段对齐到出豆", "五个热事件双侧齐全" in body)

        # ---- 保存报告，然后修正事件，回看旧报告 ----
        # 先切物理轴（避免缺锚批次在相位下的异步竞争），稳定选回 A1/B2，再切相位
        select_label(page, "时间轴", "physical")
        page.wait_for_timeout(1000)
        reqs = []
        page.on("request", lambda r: reqs.append(r.url) if "/api/compare" in r.url else None)
        reqs.clear()
        select_option_text(page, "批次A", "B2026-0914-A", exact=True)
        end = time.time() + 8
        while time.time() < end and not any("a=1" in u for u in reqs):
            page.wait_for_timeout(100)
        reqs.clear()
        select_option_text(page, "批次B", "B2026-0914-B", exact=True)
        end = time.time() + 8
        while time.time() < end and not any("b=2" in u for u in reqs):
            page.wait_for_timeout(100)
        # 等待 a=1&b=2 的响应真正渲染到 DOM
        end = time.time() + 6
        while time.time() and "差 20g" not in page.inner_text("body"):
            if time.time() > end:
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(300)
        body = page.inner_text("body")
        assert "差 20g" in body, f"未切到 A1/B2: {[x for x in body.split(chr(10)) if '差' in x]}"
        select_label(page, "时间轴", "phase")
        page.wait_for_timeout(1400)
        page.click("text=保存比较报告")
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        check("出现保存成功与报告行", "永久绑定当时两侧事件版本" in body and "基线" not in body)
        reports_before = page.locator(".reports tbody tr").count()
        check("历史报告表至少一行", reports_before >= 1, str(reports_before))

        # 报告后修正批次1一爆 575→600
        select_label(page, "模式", "single")
        page.wait_for_timeout(600)
        select_option_text(page, "批次A", "B2026-0914-A", exact=True)
        page.wait_for_timeout(1200)
        answers = iter(["600", "保存报告后再修正"])

        def ans(d):
            try:
                d.accept(next(answers))
            except StopIteration:
                d.dismiss()
        page.on("dialog", ans)
        page.evaluate(
            """()=>{const card=[...document.querySelectorAll('.card')].find(c=>c.textContent.includes('人工新增操作标记'));
            const tr=[...card.querySelectorAll('tbody tr')].find(r=>r.children[0].textContent.trim()==='一爆');
            tr.querySelector('button').click();}""")
        page.wait_for_timeout(1600)
        page.remove_listener("dialog", ans)

        # 打开刚保存的（最新、第一行）历史报告快照
        page.locator(".reports tbody tr").first.locator("button").click()
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("回看报告提示为历史快照", "历史报告" in body or "永久绑定" in body)
        seg = page.evaluate(
            """()=>{const tbl=[...document.querySelectorAll('table')].find(t=>{
            const h=t.querySelector('thead');return h&&h.textContent.includes('A 真实')&&h.textContent.includes('B 真实');});
            const tr=[...tbl.querySelectorAll('tbody tr')].find(r=>r.children[0].textContent.trim().startsWith('变黄'));
            return tr?[tr.children[1].textContent.trim(),tr.children[2].textContent.trim()]:null;}""")
        check("旧报告段时长仍是旧值(A 275s)", seg and seg[0] == "275", str(seg))
        page.screenshot(path=str(SHOTS / "10_old_report_snapshot.png"), full_page=True)

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项对齐/修订/报告浏览器检查通过 ===")
    if passed != len(results):
        for n, ok, d in results:
            if not ok:
                print("  失败:", n, d)
        sys.exit(1)


if __name__ == "__main__":
    main()
