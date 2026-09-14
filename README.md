# 烘焙批次曲线对比系统

比较**豆温、环境温度与操作事件**的过程记录系统——不使用成品评分替代过程数据。

- **前端**：Svelte 4 + ECharts 5（双网格：温度 + 温升率），Vite 开发服务器
- **后端**：FastAPI + NumPy（非均匀时间戳上的平滑、插值、温升率、阶段指标）
- **存储**：PostgreSQL（原始采样、事件及人工修正审计轨迹）
- **不连接真实烘焙机**：全部数据由带物理合理性的合成生成器产生，含噪声、非均匀采样、探针短暂失联缺测

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
