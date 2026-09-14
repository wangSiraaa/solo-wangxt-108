"""端到端浏览器交互验证（Playwright + 无头 Chromium）。

在真实浏览器里覆盖：
1. 初始单批次：ECharts 双网格、实测/插值/缺测图例、温度/RoR 系列绘制；
2. 双批次对比切换：两套颜色系列、两条批次的缺测带、提示文案；
3. 参数调整：修改平滑窗/RoR 窗 → 发出带 query 的请求且原始实测点不变；
4. 事件人工修正：连续 prompt 对话框（时间、原因）→ 来源变人工、
   历史表出现旧→新记录、阶段区间重算；
5. 人工新增操作事件；
6. 导出链接：参数随当前设置、返回 JSON 可复现指标。

运行：python browser_check.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
SHOTS = Path("/workspace/browser_shots")
SHOTS.mkdir(exist_ok=True)

os.environ.setdefault("LD_LIBRARY_PATH", f"/workspace/miniforge3/envs/roast/lib:{os.environ.get('LD_LIBRARY_PATH', '')}")
# 让无头 Chromium 使用 conda 的 fontconfig 配置（含 ~/.fonts 下的 Noto Sans SC）
os.environ.setdefault("FONTCONFIG_FILE", "/workspace/miniforge3/envs/roast/etc/fonts/fonts.conf")
os.environ.setdefault("FONTCONFIG_PATH", "/workspace/miniforge3/envs/roast/etc/fonts")

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def wait_idle(page, ms=1200):
    page.wait_for_timeout(ms)


def wait_req(reqs, needle, method=None, timeout=12.0):
    """在已捕获的请求列表里轮询等待匹配方法+URL 子串的请求。"""
    end = time.time() + timeout

    def hit():
        for m, u in reqs:
            if needle in u and (method is None or m == method):
                return (m, u)
        return None

    while time.time() < end:
        r = hit()
        if r:
            return r
        time.sleep(0.1)
    raise AssertionError(f"等待请求超时: {method or ''} {needle}; 最近={reqs[-3:]}")


def chart_opt(page):
    return page.evaluate("() => window.__roastChart ? window.__roastChart.getOption() : null")


def canvas_pixels(page):
    """返回 (非白像素数, 上半区非白, 下半区非白) 证明两张子图都画了内容。"""
    return page.evaluate(
        """() => {
            const c = document.querySelector('canvas');
            const cv = document.createElement('canvas');
            cv.width = c.width; cv.height = c.height;
            cv.getContext('2d').drawImage(c, 0, 0);
            const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
            let all = 0, top = 0, bot = 0;
            for (let y = 0; y < cv.height; y++)
              for (let x = 0; x < cv.width; x++) {
                const i = (y * cv.width + x) * 4;
                const nonWhite = d[i+3] > 40 && (d[i] < 235 || d[i+1] < 235 || d[i+2] < 235);
                if (nonWhite) { all++; if (y < cv.height * 0.62) top++; else if (y > cv.height * 0.72) bot++; }
              }
            return { all, top, bot, w: cv.width, h: cv.height };
        }"""
    )


def set_number_input(page, label_text, value):
    """通过相邻 label 文本定位数字输入框并触发 input 事件。"""
    page.evaluate(
        """([text, value]) => {
            const labels = [...document.querySelectorAll('label')];
            const lab = labels.find(l => l.textContent.includes(text));
            const inp = lab.querySelector('input[type=number]');
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, value);
            inp.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
        [label_text, str(value)],
    )


def select_by_label(page, label_text, value):
    page.evaluate(
        """([text, value]) => {
            const lab = [...document.querySelectorAll('label')].find(l => l.textContent.includes(text));
            const sel = lab.querySelector('select');
            const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
            setter.call(sel, String(value));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [label_text, value],
    )


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        reqs = []
        page.on("request", lambda r: reqs.append((r.method, r.url)) if "/api/" in r.url else None)

        # 先保证数据库是干净的基线
        page.request.post("http://127.0.0.1:8000/api/admin/reseed?seed=7")

        # ---------------- 1. 初始单批次 ----------------
        page.goto(BASE, wait_until="networkidle")
        wait_idle(page, 1800)
        check("页面标题", "烘焙" in page.title())
        check("ECharts canvas 存在", page.locator("canvas").count() == 1)
        opt = chart_opt(page)
        check("双网格 grid(温度+RoR)", opt is not None and len(opt.get("grid", [])) == 2,
              f"grids={len(opt.get('grid', [])) if opt else None}")
        sers = [s["name"] for s in opt["series"]] if opt else []
        check("含豆温实测系列", any("豆温实测" in s for s in sers), str([s for s in sers if "豆温" in s]))
        check("含环境温度实测系列", any("环境温度实测" in s for s in sers))
        check("含 RoR 系列", any("温升率RoR" in s for s in sers))
        check("含独立插值系列(虚线)", any("插值" in s for s in sers))
        ynames = [y.get("name", "") for y in opt["yAxis"]] if opt else []
        check("RoR 轴名带窗口秒数", any("RoR" in n and "s居窗" in n for n in ynames), str(ynames))
        # markArea 缺测带：单批次1只有小缺口（无灰带），但系列应保留 markArea 结构
        check("markArea 结构存在", opt["series"][0].get("markArea") is not None)
        # 事件垂直线（下豆/回温点/一爆等）
        marks = opt["series"][0].get("markLine", {}).get("data", [])
        mark_labels = [m.get("label", {}).get("formatter", "") for m in marks]
        check("事件线含回温点/一爆", any("回温点" in x for x in mark_labels) and any("一爆" in x for x in mark_labels),
              str(mark_labels))
        check("事件线含风门注释", any("风门" in x for x in mark_labels), str([x for x in mark_labels if "风" in x]))
        px = canvas_pixels(page)
        check("温度子图已绘制(上区非白)", px["top"] > 2000, str(px))
        check("RoR 子图已绘制(下区非白)", px["bot"] > 500, str(px))
        # 图例说明条
        body = page.inner_text("body")
        check("图例条:实测", "实测温度" in body)
        check("图例条:插值为探针失联线性", "线性插值" in body and "探针短暂失联" in body)
        check("图例条:缺测过长未插值", "缺测过长" in body)
        check("RoR 窗口文字说明", "居中窗口" in body and "平滑" in body)
        check("非因果免责声明", "不宣称风门变化导致" in body or "不表示因果" in body)
        check("未连接烘焙机标识", "未连接烘焙机" in body)
        page.screenshot(path=str(SHOTS / "1_single_batch.png"), full_page=True)

        # 指标表
        check("阶段表含发展时间比", "发展时间比" in body)
        check("公式区间明确", "(drop_s" in body and "first_crack" in body)

        # ---------------- 2. 双批次对比 ----------------
        reqs.clear()
        select_by_label(page, "模式", "compare")
        wait_req(reqs, "/api/compare")
        wait_idle(page, 1500)
        opt = chart_opt(page)
        sers = [s["name"] for s in opt["series"]]
        n_bean = sum(1 for s in sers if "豆温实测" in s)
        check("对比模式两条豆温系列", n_bean == 2, f"{n_bean} 条豆温")
        n_ror = sum(1 for s in sers if "温升率RoR" in s)
        check("对比模式两条 RoR 系列", n_ror == 2, f"{n_ror} 条 RoR")
        marks = opt["series"][0].get("markLine", {}).get("data", [])
        labels = [m.get("label", {}).get("formatter", "") for m in marks]
        check("两批事件线带 A/B 后缀",
              any(l.endswith("A") and "回温点" in l for l in labels)
              and any(l.endswith("B") and "回温点" in l for l in labels),
              str([l for l in labels if "回温点" in l]))
        # 批次 B 的 34s 大缺口 → 必须有灰色 markArea
        areas = opt["series"][0].get("markArea", {}).get("data", [])
        check("批次B大缺口灰带存在(不插值)", len(areas) >= 1, f"{len(areas)} 个灰带")
        body = page.inner_text("body")
        check("对比页时间基准说明", "下豆点" in body and "对齐" in body)
        check("对比页非因果提示", "风门前后差异不构成因果" in body)
        check("两块指标面板(A/B)", body.count("阶段指标") == 2)
        page.screenshot(path=str(SHOTS / "2_compare.png"), full_page=True)

        # 切换 A/B 选择（把 A 设为批次2，B 设为批次1）
        bids = page.evaluate(
            "() => [...document.querySelectorAll('label')].find(l=>l.textContent.includes('批次A')).querySelector('select')"
        )
        id1, id2 = page.evaluate(
            """() => {
                const s=[...document.querySelectorAll('label')].find(l=>l.textContent.includes('批次A')).querySelector('select');
                return [...s.options].map(o=>o.value);
            }"""
        )
        reqs.clear()
        select_by_label(page, "批次A", id2)
        wait_req(reqs, "/api/compare")
        wait_idle(page, 800)
        check("切换批次A后仍为两条系列",
              sum(1 for s in [x["name"] for x in chart_opt(page)["series"]] if "豆温实测" in s) == 2)
        reqs.clear()
        select_by_label(page, "批次A", id1)
        wait_req(reqs, "/api/compare")
        wait_idle(page, 800)

        # ---------------- 3. 回单批次 + 参数调整 ----------------
        reqs.clear()
        select_by_label(page, "模式", "single")
        wait_req(reqs, "/api/batches/1")
        wait_idle(page, 1200)
        opt0 = chart_opt(page)
        bean0 = [s["data"] for s in opt0["series"] if s["name"] == "豆温实测"][0]
        reqs.clear()
        smooth_box = page.locator("header label", has_text="平滑窗").locator("input")
        ror_box = page.locator("header label", has_text="RoR窗").locator("input")
        # 真人操作：聚焦→全选→直接键入覆盖原值（不先清空，避免空串绑定触发异常请求）
        smooth_box.click()
        page.keyboard.press("Control+A")
        smooth_box.type("51", delay=40)
        page.wait_for_timeout(120)
        ror_box.click()
        page.keyboard.press("Control+A")
        ror_box.type("71", delay=40)
        # 防抖（250ms）在停止键入后才触发；等待同时含两组参数的最终请求
        end = time.time() + 10
        last = ""
        while time.time() < end:
            cand = [u for (m, u) in reqs if m == "GET" and "smooth_window_s=51" in u and "ror_window_s=71" in u]
            if cand:
                last = cand[-1]
                break
            page.wait_for_timeout(100)
        check("参数调整发出带 query 的请求", bool(last), last)
        wait_idle(page, 1200)
        opt1 = chart_opt(page)
        bean1 = [s["data"] for s in opt1["series"] if s["name"] == "豆温实测"][0]
        check("改平滑参数后原始实测豆温逐点不变", bean0 == bean1,
              f"points={len(bean0)}/{len(bean1)}")
        sm0 = [s["data"] for s in opt0["series"] if "平滑" not in s["name"]]  # 平滑未直接成系列；用 RoR 验证派生变化
        ror0 = [s["data"] for s in opt0["series"] if "温升率RoR" in s["name"]][0]
        ror1 = [s["data"] for s in opt1["series"] if "温升率RoR" in s["name"]][0]
        check("RoR 派生序列随参数变化", ror0 != ror1, f"{len(ror0)} vs {len(ror1)} pts")
        yn = [y.get("name", "") for y in chart_opt(page)["yAxis"]]
        check("RoR 轴名更新为新窗口", any("71s居窗" in n for n in yn), str(yn))
        check("页面窗口文字更新为71", "71" in page.inner_text("body"))
        page.screenshot(path=str(SHOTS / "3_params_changed.png"), full_page=True)

        # ---------------- 4. 事件人工修正 ----------------
        row = page.locator("tr", has_text="一爆").first
        old_t = row.locator("td").nth(1).inner_text()
        answers = iter(["592.5", "听声复核，一爆偏晚"])

        def answer_dialog(dialog):
            try:
                dialog.accept(next(answers))
            except StopIteration:
                dialog.dismiss()

        page.on("dialog", answer_dialog)
        reqs.clear()
        # 用 XPath 直接锁定热事件表中“一爆”那一行的修正按钮（get_by_role 在长页面下不稳定）
        revise_btn = page.locator("xpath=//tr[td[normalize-space()='一爆']]//button[normalize-space()='修正']")
        revise_btn.wait_for(state="attached", timeout=10000)
        revise_btn.evaluate("(el) => el.click()")
        wait_req(reqs, "/api/events/", "PUT")
        wait_idle(page, 1500)
        page.remove_listener("dialog", answer_dialog)
        body = page.inner_text("body")
        check("PUT 修正返回后表格显示新时间", "592.5" in body)
        # 该行来源变为“人工”（限定当前事件表，排除下面的修正历史表）
        src = page.evaluate(
            """() => {
                const card = [...document.querySelectorAll('.card')].find(c => c.textContent.includes('人工新增操作标记'));
                const tr = [...card.querySelectorAll('tbody tr')].find(r => r.children[0].textContent.trim() === '一爆');
                return tr.children[3].textContent.trim();
            }"""
        )
        check("一爆来源变为人工", src == "人工", f"来源={src}")
        check("修正历史表出现 旧→新", "575" in body and "592.5" in body and "听声复核" in body)
        # 发展段区间起点重算为 592.5
        check("发展段区间按修正后重算", "592.5→" in body)
        page.screenshot(path=str(SHOTS / "4_event_revised.png"), full_page=True)

        # ---------------- 5. 人工新增操作事件 ----------------
        # 选 kind=damper，填时间/档位/备注，点新增
        page.locator(".card .row").first.scroll_into_view_if_needed()
        page.locator(".card .row select").first.select_option("damper")  # 事件编辑卡片内的新增下拉
        page.fill(".row input[placeholder='时间 s']", "430")
        page.fill(".row input[placeholder='档位 0-100']", "65")
        page.fill(".row input[placeholder='备注']", "浏览器自动化新增风门")
        before = body.count("风门")
        reqs.clear()
        page.click(".row button:has-text('新增')")
        wait_req(reqs, "/api/events", "POST")
        wait_idle(page, 1500)
        body = page.inner_text("body")
        # 新增的操作行：在“当前事件表”（每行带修正按钮）里查找 风门/430/65/人工
        has_damper_row = page.evaluate(
            """() => {
                const card = [...document.querySelectorAll('.card')].find(c => c.textContent.includes('人工新增操作标记'));
                return [...card.querySelectorAll('tbody tr')].some(tr => {
                    if (!tr.querySelector('button')) return false;  // 排除修正历史表
                    const c = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
                    return c[0] === '风门' && c[1] === '430' && c[2] === '65' && c[3] === '人工';
                });
            }"""
        )
        check("新增风门事件出现在表中(风门/430/65/人工)", has_damper_row)
        # 备注是否保存：直接查后端事件（UI 不逐条铺备注，避免表格过宽）
        ev_json = page.request.get("http://127.0.0.1:8000/api/batches/1").json()
        saved_note = any(
            e["kind"] == "damper" and abs(e["t_s"] - 430) < 1e-9
            and e.get("note") == "浏览器自动化新增风门" and e["source"] == "manual"
            for e in ev_json["events"]
        )
        check("新增风门备注与人工来源已落库", saved_note)
        opt = chart_opt(page)
        labels = [m.get("label", {}).get("formatter", "") for m in
                  opt["series"][0].get("markLine", {}).get("data", [])]
        check("新风门 430 已画到图上", any("风门→65" in x for x in labels), str([x for x in labels if "风门" in x]))
        page.screenshot(path=str(SHOTS / "5_manual_event.png"), full_page=True)

        # ---------------- 6. 导出链接 ----------------
        href = page.get_attribute(".controls a.btn", "href")
        check("导出链接带当前参数", "smooth_window_s=51" in href and "ror_window_s=71" in href, href)
        resp = page.goto("http://127.0.0.1:8000" + href)
        check("导出 HTTP 200", resp.status == 200, str(resp.status))
        bundle = resp.json()
        check("导出 schema/未连机标识", bundle.get("export_schema") == "roast-export-v1"
              and bundle.get("machine_connected") is False)
        check("导出含原始缺测 NULL", any(s["bean_temp_c"] is None for s in bundle["raw_samples"]))
        check("导出含修正历史", len(bundle["event_revisions"]) >= 1)
        check("导出含全部计算参数",
              bundle["compute"]["smooth_window_s"] == 51 and bundle["compute"]["ror_window_s"] == 71)

        # 用导出包重算并核对指标（与后端测试一致的复现保证）
        sys.path.insert(0, "/workspace/backend")
        from app.analytics import ComputeParams, analyze_channel, phase_metrics
        c = bundle["compute"]
        p = ComputeParams(c["smooth_window_s"], c["ror_window_s"], c["max_interp_gap_s"])
        bean = analyze_channel(bundle["raw_samples"], "bean_temp_c", p)
        m = phase_metrics(bean, bundle["events_active"])
        check("导出包可重算并复现阶段指标", m == bundle["derived"]["metrics"])
        check("导出包可复现逐点 RoR",
              [(x["t_s"], x["v"], x["origin"]) for x in bean["ror"]] ==
              [(x["t_s"], x["v"], x["origin"]) for x in bundle["derived"]["bean"]["ror"]])
        check("修正后一爆时间进入导出事件",
              abs(next(e for e in bundle["events_active"] if e["kind"] == "first_crack")["t_s"] - 592.5) < 1e-9)

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} 项浏览器交互检查通过 ===")
    if passed != len(results):
        for n, ok, d in results:
            if not ok:
                print("  失败:", n, d)
        sys.exit(1)


if __name__ == "__main__":
    main()
