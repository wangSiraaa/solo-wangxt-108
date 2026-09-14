<script>
  import { onMount } from 'svelte';
  import { api } from './api.js';
  import RoastChart from './components/RoastChart.svelte';
  import PhaseChart from './components/PhaseChart.svelte';
  import EventEditor from './components/EventEditor.svelte';
  import MetricsPanel from './components/MetricsPanel.svelte';
  import ComparePanel from './components/ComparePanel.svelte';
  import CandidatePanel from './components/CandidatePanel.svelte';
  import NotesPanel from './components/NotesPanel.svelte';
  import TagsPanel from './components/TagsPanel.svelte';
  import Legend from './components/Legend.svelte';

  let batches = [];
  let mode = 'single';
  let selA = 1, selB = 2;
  let smooth = 21, ror = 45;
  let alignment = 'physical';
  let detail = null;
  let compareData = null;
  let revisions = [];
  let reports = [];
  let savedMsg = '';
  let loading = false, error = '';

  onMount(async () => {
    batches = await api.batches();
    if (batches.length) selA = batches[0].id;
    if (batches.length > 1) selB = batches[1].id;
    reports = await api.reports();
    refresh();
  });

  let reqSeq = 0;
  async function refresh() {
    if (selA == null) return;
    const seq = ++reqSeq;  // 防止快速切换时旧响应覆盖新状态
    loading = true; error = '';
    try {
      if (mode === 'compare' && selB != null) {
        const cmp = await api.compare(selA, selB, {
          smooth_window_s: smooth, ror_window_s: ror, alignment
        });
        if (seq !== reqSeq) return;  // 已有更新的请求，丢弃本次结果
        compareData = cmp;
        detail = cmp.a;
      } else {
        const d = await api.detail(selA, { smooth_window_s: smooth, ror_window_s: ror });
        if (seq !== reqSeq) return;
        compareData = null;
        detail = d;
      }
      const revs = await api.revisions(selA);
      if (seq === reqSeq) revisions = revs;
    } catch (e) {
      if (seq === reqSeq) error = e.message;
    } finally {
      if (seq === reqSeq) loading = false;
    }
  }

  async function reseed() {
    if (!confirm('重置为内置合成数据（4 批，含缺一爆/大锅量/壁探针），将清空修正与报告。继续？')) return;
    await api.reseed();
    batches = await api.batches();
    reports = await api.reports();
    await refresh();
  }

  let debounce;
  function paramChanged() {
    clearTimeout(debounce);
    debounce = setTimeout(refresh, 250);
  }

  async function saveReport() {
    savedMsg = '保存中…';
    const r = await api.createReport({
      a: selA, b: selB, alignment, smooth_window_s: smooth, ror_window_s: ror,
      note: `报告绑定创建时事件版本`
    });
    savedMsg = `已保存报告 #${r.report_id}，永久绑定当时两侧事件版本`;
    reports = await api.reports();
  }

  async function openReport(id) {
    const rep = await api.report(id);
    compareData = rep.snapshot;
    mode = 'compare';
    alignment = rep.alignment;
    detail = rep.snapshot.a;
    savedMsg = `正在查看历史报告 #${id}（${rep.binding_warning.slice(0, 24)}…）`;
  }

  function viewBatch(id) {
    mode = 'single';
    selA = id;
    refresh();
  }

  $: chartData = mode === 'compare' && compareData ? compareData : detail;
  $: phaseReady = mode === 'compare' && compareData && compareData.phase && compareData.phase.available;
</script>

<header>
  <h1>烘焙批次曲线对比 <span class="tag">合成数据 · 未连接烘焙机</span></h1>
  <div class="controls">
    <label>模式
      <select bind:value={mode} on:change={refresh}>
        <option value="single">单批次</option>
        <option value="compare">双批次对比</option>
      </select>
    </label>
    <label>批次A
      <select bind:value={selA} on:change={refresh}>
        {#each batches as b}<option value={b.id}>{b.name} · {b.charge_g}g · {b.probe_position === 'bean-bulk' ? '豆堆探针' : '滚筒壁探针'}</option>{/each}
      </select>
    </label>
    {#if mode === 'compare'}
      <label>批次B
        <select bind:value={selB} on:change={refresh}>
          {#each batches as b}<option value={b.id}>{b.name} · {b.charge_g}g · {b.probe_position === 'bean-bulk' ? '豆堆探针' : '滚筒壁探针'}</option>{/each}
        </select>
      </label>
      <label>时间轴
        <select bind:value={alignment} on:change={refresh}>
          <option value="physical">物理对齐（相同真实秒数）</option>
          <option value="phase">相位对齐（相同阶段进度）</option>
        </select>
      </label>
    {/if}
    <label>平滑窗 <input type="number" bind:value={smooth} min="3" max="300" step="2" on:input={paramChanged} /> s</label>
    <label>RoR窗 <input type="number" bind:value={ror} min="5" max="600" step="2" on:input={paramChanged} /> s</label>
    {#if mode === 'compare'}
      <button on:click={saveReport}>保存比较报告</button>
    {/if}
    {#if mode === 'single'}
      <a class="btn" href={api.exportUrl(selA, { smooth_window_s: smooth, ror_window_s: ror })} target="_blank">导出可复现JSON</a>
    {/if}
    <button on:click={reseed}>重置合成数据</button>
  </div>
</header>

{#if savedMsg}<div class="saved">{savedMsg}</div>{/if}
{#if error}<div class="error">{error}</div>{/if}
{#if loading}<div class="loading">加载中…</div>{/if}

{#if compareData && compareData.a}
  <div class="cmphead">
    <button class="chip a" on:click={() => viewBatch(compareData.a.batch.id)}>A: {compareData.a.batch.name}</button>
    <button class="chip b" on:click={() => (selB = compareData.b.batch.id, mode = 'single', selA = compareData.b.batch.id, refresh())}>
      B: {compareData.b.batch.name}
    </button>
    <span class="hint">{alignment === 'phase' ? '相位对齐（分段、仅共同锚点）' : compareData.time_reference || '物理对齐：各自下豆点为0'}；{compareData.note}</span>
  </div>
{/if}

{#if mode === 'compare' && compareData}
  <ComparePanel data={compareData} />
  <NotesPanel aId={selA} bId={selB} {alignment} {smooth} {ror} />
{/if}

{#if chartData && mode === 'single'}
  <RoastChart data={chartData} />
  <Legend {chartData} />
{:else if chartData && mode === 'compare'}
  {#if alignment === 'phase' && phaseReady}
    <PhaseChart data={compareData} />
    <div class="legend-note">
      <span>横轴为规范阶段时间（非真实时刻）；两批锚点处的真实时间见垂直线标注。</span>
      <span>RoR 为各自真实时间差分值的位置重映射，<b>未沿变形轴重新求导</b>。</span>
    </div>
  {:else if alignment === 'phase'}
    <div class="noalign">相位对齐不可用或仅有共同前缀——上方问题面板说明原因；下图保留物理时间轴。</div>
    <RoastChart data={compareData} />
    <Legend chartData={compareData} />
  {:else}
    <RoastChart data={compareData} />
    <Legend chartData={compareData} />
  {/if}
{/if}

{#if mode === 'compare' && compareData}
  <div class="grid2">
    <MetricsPanel detail={compareData.a} />
    <MetricsPanel detail={compareData.b} />
  </div>
{:else if detail}
  <div class="grid2">
    <MetricsPanel {detail} />
    <div class="stack">
      <TagsPanel {detail} onChange={refresh} />
      <EventEditor {detail} {revisions} on:revised={refresh} />
      <CandidatePanel batchId={detail.batch.id} onChange={refresh} />
      <NotesPanel aId={detail.batch.id} />
    </div>
  </div>
{/if}

{#if reports.length}
  <div class="reports">
    <h3>历史比较报告（绑定创建时事件版本）</h3>
    <table>
      <thead><tr><th>#</th><th>A</th><th>B</th><th>对齐</th><th>窗口 s</th><th>备注</th><th></th></tr></thead>
      <tbody>
        {#each reports as r}
          <tr>
            <td>{r.id}</td><td>{r.batch_a_name}</td><td>{r.batch_b_name}</td>
            <td>{r.alignment === 'phase' ? '相位' : '物理'}</td>
            <td>{r.smooth_window_s}/{r.ror_window_s}</td><td>{r.note || ''}</td>
            <td><button on:click={() => openReport(r.id)}>查看快照</button></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{#if chartData && mode === 'single'}
  <footer>
    <ul>{#each (detail?.caveats || []) as c}<li>{c}</li>{/each}</ul>
  </footer>
{/if}

<style>
  :global(body) { margin:0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background:#f5f5f3; color:#222; }
  h1 { font-size:18px; margin:0; }
  .tag { font-size:11px; background:#c0392b; color:#fff; padding:2px 8px; border-radius:10px; vertical-align:middle; }
  header { background:#fff; border-bottom:1px solid #ddd; padding:12px 18px; }
  .controls { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-top:10px; font-size:13px; }
  select, input[type=number] { padding:4px 6px; border:1px solid #bbb; border-radius:4px; }
  input[type=number] { width:64px; }
  .btn, button { padding:5px 12px; border:1px solid #888; border-radius:4px; background:#fafafa; cursor:pointer; text-decoration:none; color:#222; font-size:13px; }
  .cmphead { padding:8px 18px; display:flex; gap:10px; align-items:center; }
  .chip { font-weight:600; } .chip.a { border-color:#c0392b; } .chip.b { border-color:#d68910; }
  .hint { font-size:12px; color:#666; }
  .error { color:#c0392b; padding:8px 18px; }
  .loading { padding:8px 18px; color:#888; }
  .saved { margin:8px 18px 0; padding:8px 10px; background:#e8f6ee; color:#1e7d46; border-radius:6px; font-size:12.5px; }
  .legend-note { display:flex; gap:18px; flex-wrap:wrap; padding:6px 18px; font-size:12px; color:#444; }
  .noalign { margin:8px 18px 0; padding:8px 10px; background:#fdf3e2; color:#9c6b00; border-radius:6px; font-size:12.5px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; padding:8px 18px; }
  .stack { display:flex; flex-direction:column; gap:14px; }
  .reports { margin:8px 18px 24px; background:#fff; border:1px solid #e3e3e3; border-radius:8px; padding:14px; }
  .reports table { border-collapse:collapse; width:100%; font-size:12.5px; }
  .reports th,.reports td { border-bottom:1px solid #eee; padding:4px 8px; text-align:left; }
  footer { padding:8px 18px 24px; font-size:12px; color:#666; }
  footer ul { margin:0; padding-left:18px; }
  @media (max-width: 980px) { .grid2 { grid-template-columns:1fr; } }
</style>
