# 烘焙批次曲线对比系统

比较**豆温、环境温度与操作事件**的过程记录系统——不使用成品评分替代过程数据。

- **前端**：Svelte 4 + ECharts 5（双网格：温度 + 温升率），Vite 开发服务器
- **后端**：FastAPI + NumPy（非均匀时间戳上的平滑、插值、温升率、阶段指标）
- **存储**：PostgreSQL（原始采样、事件及人工修正审计轨迹）
- **不连接真实烘焙机**：全部数据由带物理合理性的合成生成器产生，含噪声、非均匀采样、探针短暂失联缺测

## 阶段对齐与可比性（第二阶段）

单按下豆点对齐会掩盖同配方不同锅量的阶段速度差异，因此比较提供两种时间轴：

- **物理对齐**（默认，`alignment=physical`）：各自下豆点为 0 的真实秒数，回答“**相同物理时长**下的数值”；
- **相位对齐**（`alignment=phase`，`alignment.py`）：仅在两侧**共同拥有、顺序一致的已确认事件锚点**
  （下豆→回温点→变黄→一爆→出豆）之间做分段线性映射到规范时间，回答“**相同阶段进度**下的数值”。

关键约束（均有测试与浏览器验证）：

1. **保留原始时间轴**：原始采样、平滑、RoR 全部不动，相位系列是单独的派生显示坐标
   （`phase.series`），物理视图与原始导出不受影响。
2. **事件缺失/顺序矛盾不强行拉伸**：`build_anchors` 只建立共同有序锚点。批次 C 缺一爆时，
   锚点只到“变黄”，其后尾段（含出豆）保持真实时间、不进入相位图，并在 `anchor_issues`
   给出警告；检测到时间倒退（顺序矛盾）时在该锚点停止对齐并报错，不拉伸曲线。
3. **RoR 绝不沿变形时间重求导**：RoR 始终由 `analytics` 在真实时间上差分得到；
   相位视图只把每个已有 RoR 点搬到规范横坐标（同时保留 `src_t_s`），值与来源逐点不变
   （`test_phase_ror_values_equal_real_time_ror`）。
4. **区分两种问题**：`physical_table` 按同一真实秒取值；`phase_table` 按同一段内同一百分比取值
   （每行同时给出 `real_t_a_s/real_t_b_s` 与 `canonical_t_s`）。真实阶段快慢单独列在
   `segments` 的两侧真实时长与拉伸系数里——对齐会把速度差异归一化掉，所以速度只能看那里。
5. **可比性标识**（`comparability`）：依据配方、锅量、探针位置/系统偏差给出
   `absolute_temperature_comparable` / `ror_shape_comparable` 等标志与说明。
   - 探针位置不同（批次 D 为滚筒壁探针 +2℃ 偏差）→ **绝对温度水平不可直接比**，RoR 形态可参考；
   - 锅量差 400g（批次 C 1600g）→ 头条降为“部分可比”，明确速度差异可能主要来自装料量；
   - 模型对齐一律标注为“可视化归一化，**不等于工艺等效**”。

### 候选修订与报告版本绑定

- **候选修订**（`event_candidates` 表）：负责人可为疑似误标建立“建议”，状态 proposed，
  **不改变当前有效事件、不影响对齐**；`POST /api/candidates/{id}/decision` 接受时才落成一次
  正式人工修正（进入事件新版本与审计轨迹；缺该事件时则补建 manual 事件，如批次 C 补一爆），
  拒绝只改状态。已决定候选不能重复决定。
- **比较报告**（`comparison_reports` 表）：`POST /api/reports` 保存当前比较，同时快照两侧
  **当时的有效事件版本**（`event_version_a/b`）与完整结果。之后事件再被人工修正，旧报告不变
  （`GET /api/reports/{id}` 回看旧段时长，附“永久绑定”声明），实时视图才反映新事件。

### 四个合成批次

| 批次 | 用途 |
|---|---|
| A `B2026-0914-A` | 基准：豆堆探针 1200g，全事件 |
| B `B2026-0914-B` | 同配方 1180g，风门更早，32s 大缺口（不插值） |
| C `B2026-0914-C-bigcharge` | 同配方 **1600g（锅量改变、更慢）**，**一爆标记缺失** |
| D `B2026-0914-D-wallprobe` | 同豆同量但**探针装在滚筒壁 +2℃ 偏差** |

第二阶段浏览器验证 `browser_check2.py`（26 项）覆盖物理/相位切换、三种样例的可比性标识、
缺锚只对齐前缀、候选建立→接受→全段对齐、保存报告→报告后修正→回看旧快照段时长仍是旧值。

## 运行

环境为无 root 的 aarch64 机器，依赖装在 `/workspace/miniforge3` 的 conda 环境 `roast` 中
（NumPy / FastAPI / psycopg / PostgreSQL 18 均在其中）。

```bash
# 1. 启动用户态 PostgreSQL（端口 5433，socket /tmp，库 roastdb）
./scripts/start_pg.sh

# 2. 启动 FastAPI（:8000，启动时自动建表并播种两条合成批次）
./scripts/start_backend.sh

# 3. 启动前端（:5173，/api 代理到 8000）
cd frontend && npm install && npm run dev
```

浏览器打开 http://localhost:5173 。重置合成数据：`POST /api/admin/reseed?seed=7`
（同一 seed 可完全重现采样与事件）。

测试：

```bash
PYTHONPATH=backend python -m pytest backend/tests/ -q
```

### 浏览器端到端交互验证（已在本机无头 Chromium 执行）

`browser_check.py` 用 Playwright 驱动真实 Chromium，在页面上完成全部交互后断言，
共 50 项检查（截图输出到 `browser_shots/`）：

- ECharts **双网格**（温度上、RoR 下）均实际绘制像素；系列名、双 Y 轴、RoR 轴窗口秒数；
- 实测实线 / 插值虚线独立系列；批次 B 的 34s 缺口渲染为灰色 markArea；
  事件垂直线（下豆/回温点/变黄/一爆/出豆）与风门/燃气注释；
- 双批次对比切换：A/B 两套豆温与 RoR 系列、事件线 A/B 后缀、两块指标面板、非因果提示；
- 切换批次下拉后仍为两套系列；
- 用**真实键盘**修改平滑窗 51 / RoR 窗 71 → 请求带正确 query、
  原始实测豆温 730 个点逐点不变、RoR 派生点数变化（653→597）、轴名更新；
- 点“修正”→ 连续 prompt（新时间 592.5、原因）→ 来源变红色“人工”、
  底部审计表出现 575(自动)→592.5(人工) 与原因/操作人、发展段区间与发展时间比重算；
- 人工新增风门 430/65（含备注）→ 出现在当前事件表并画到图上、备注落库；
- 导出链接带当前参数，返回 JSON，且用导出的原始数据+参数在脚本中重算，
  阶段指标与逐点 RoR（含来源）完全一致。

无头环境字体：CJK 字体需放在 `~/.fonts` 且 fontconfig 缓存版本要与 Chromium 自带版本匹配
（删除 `~/.cache/fontconfig` 让其自重建），否则中文显示为方框——仅影响截图，不影响功能。


## 需求是如何落实的

### 1. 温升率必须说明采用的窗口
`GET /api/batches/{id}` 返回 `bean.ror_window.description` 与每点 `window_s`/`method`：

> 先对豆温做 **S 秒居中窗均值平滑**（时间加权梯形积分，非均匀采样下对线性斜坡无偏），
> 再取 **t±W/2 两个平滑端点做差分**（℃/min）。网格离散时用两端实际时间差做分母；
> 窗口含不可跨越缺口或贴近序列边缘时，该点 RoR **留空**（不外推）。

前端可实时调整 `smooth_window_s`、`ror_window_s`，参数随每个响应返回（`params`）。

### 2. 插值段不能冒充实测
- 原始采样探针失联时温度存 `NULL`（`samples.is_missing=true`），从不写回假值。
- 缺口两侧有实测锚点且跨度 ≤ `max_interp_gap_s`（默认 20s）才线性插值；
  插值点单独放在 `bean.interpolated` / `env.interpolated`，`origin="interpolated"`，
  前端画**虚线**，与实测实线分离。
- 超过上限（批次 2 有 34s 缺口）或位于序列边缘：**不插值**，图上以灰色带标注，
  平滑与 RoR 在受影响区间直接留空。
- 来源会传播：平滑窗/差分窗一旦"碰到"插值段，派生点也标 `interpolated`
  （见 `test_ror_origin_downgrades_near_interpolated_gap`）。

### 3. 回温点、一爆等事件可人工修正并保留来源
- 每个事件带 `source`（`auto` 生成器播种 / `manual` 人工）。
- `PUT /api/events/{id}` 修正：旧记录置 `revoked_at`（保留不删），插入 `manual` 新记录，
  并向 `event_revisions` 写一行审计（旧/新时间、旧/新来源、原因、操作人）。
- 也支持 `POST /api/events?bid=` 人工新增标记。阶段指标始终按**当前有效事件**重算。

### 4. 发展时间比按明确区间计算
`analytics.phase_metrics`：

| 阶段 | 区间 |
|---|---|
| 脱水 | charge → turnaround（RoR 均值只在回温点之后统计） |
| 梅纳 | turnaround → yellow |
| 升温 | yellow → first_crack |
| 发展 | first_crack → drop |

**发展时间比 = (drop − first_crack) / (drop − turnaround)**，公式字符串与两个秒数一起返回。

### 5. 双批次对比，但不对风门变化宣称因果
- `GET /api/compare?a=&b=` 两批次以各自下豆点为 t=0 对齐叠加。
- 风门/燃气是图上点线注释；`operation_windows` 只给操作时刻 ±60s 窗的
  描述性前后均值，每条都带"非因果"声明。

### 6. 含噪声与缺测的合成数据
`synth.py`：集总升温模型（环境温度分段折线目标 + 一阶跟踪 + 燃气/风门小幅扰动 +
一爆前吸热凹陷）→ 非均匀时间戳（1s±抖动、偶发长间隔）→ 独立高斯噪声 →
注入失联窗口（批次 A 15s 可插值；批次 B 34s 不可插值）。

### 7. 改变平滑参数不改原始温度（已验证）
`test_changing_smooth_param_keeps_raw_identical`：用 15/31s 与 55/91s 两组参数请求，
库中原始采样与响应中的 `observed` 逐点一致，只有 `smoothed`/`ror` 变化。
派生序列与原始数据在代码与导出包中始终分开存放。

### 8. 导出能重现所有阶段指标
`GET /api/batches/{id}/export` 产出 `roast-export-v1` JSON（同时落盘
`backend/exports/batch_{id}_export.json`），包含：
原始采样（NULL 缺测语义保留）、有效事件、完整修正历史、`algo_version`、
全部计算参数、插值/RoR/发展比规则、以及派生产物。
`test_export_reproduces_all_stage_metrics` 用导出包中的原始数据 + 参数重算，
与包内阶段指标和逐点 RoR（值与来源）断言完全一致。

## 目录

```
backend/app/
  config.py      环境变量配置（DSN、默认窗口、插值缺口上限）
  db.py          PostgreSQL schema（batches/samples/events/event_revisions）
  synth.py       合成生成器（噪声/非均匀/失联）
  analytics.py   NumPy 平滑·插值·温升率·阶段区间·操作前后窗
  services.py    播种、查询、事件人工修正（审计轨迹）
  main.py        FastAPI 路由
backend/tests/   6 个纯算法测试 + 5 个 API/PG 集成测试
frontend/src/
  App.svelte                     批次选择、参数、对比/单批次
  components/RoastChart.svelte   ECharts 双网格（实测实线/插值虚线/缺测灰带/事件线）
  components/EventEditor.svelte  事件修正与新增、修正历史
  components/MetricsPanel.svelte 阶段指标表 + 操作前后描述性对比
```
