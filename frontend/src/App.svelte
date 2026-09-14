<script>
  import { onMount } from 'svelte';
  import { api } from './api.js';
  import RoastChart from './components/RoastChart.svelte';
  import EventEditor from './components/EventEditor.svelte';
  import MetricsPanel from './components/MetricsPanel.svelte';

  let batches = [];
  let mode = 'single'; // single | compare
  let selA = null, selB = null;
  let smooth = 21, ror = 45;
  let detail = null;       // 单批次
  let compareData = null;  // 对比
  let revisions = [];
  let loading = false, error = '';

  onMount(loadBatches);

  async function loadBatches() {
    batches = await api.batches();
    if (batches.length) selA = batches[0].id;
    if (batches.length > 1) selB = batches[1].id;
    refresh();
  }

  async function refresh() {
    if (selA == null) return;
    loading = true; error = '';
    try {
      if (mode === 'compare' && selB != null) {
        compareData = await api.compare(selA, selB, { smooth_window_s: smooth, ror_window_s: ror });
        detail = compareData.a;
      } else {
        compareData = null;
        detail = await api.detail(selA, { smooth_window_s: smooth, ror_window_s: ror });
      }
      revisions = await api.revisions(selA);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  async function reseed() {
    if (!confirm('重置为内置合成数据（含噪声与缺测），将清空当前修正。继续？')) return;
    await api.reseed();
    await loadBatches();
  }

  let debounce;
  function paramChanged() {
    clearTimeout(debounce);
    debounce = setTimeout(refresh, 250);
  }

  function viewBatch(id) {
    mode = 'single';
    selA = id;
    refresh();
  }

  $: chartData = mode === 'compare' && compareData ? compareData : detail;
  $: exportHref = selA != null
    ? api.exportUrl(selA, { smooth_window_s: smooth, ror_window_s: ror }) : '#';
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
        {#each batches as b}<option value={b.id}>{b.name} · {b.variety}</option>{/each}
      </select>
    </label>
    {#if mode === 'compare'}
      <label>批次B
        <select bind:value={selB} on:change={refresh}>
          {#each batches as b}<option value={b.id}>{b.name} · {b.variety}</option>{/each}
        </select>
      </label>
    {/if}
    <label>平滑窗 <input type="number" bind:value={smooth} min="3" max="300" step="2" on:input={paramChanged} /> s</label>
    <label>RoR窗 <input type="number" bind:value={ror} min="5" max="600" step="2" on:input={paramChanged} /> s</label>
    <a class="btn" href={exportHref} target="_blank">导出可复现JSON</a>
    <button on:click={reseed}>重置合成数据</button>
  </div>
</header>

{#if compareData}
  <div class="cmphead">
    <button class="chip a" on:click={() => viewBatch(compareData.a.batch.id)}>A: {compareData.a.batch.name}</button>
    <button class="chip b" on:click={() => viewBatch(compareData.b.batch.id)}>B: {compareData.b.batch.name}</button>
    <span class="hint">{compareData.time_reference}；{compareData.note}</span>
  </div>
{/if}

{#if error}<div class="error">{error}</div>{/if}
{#if loading}<div class="loading">加载中…</div>{/if}

{#if chartData}
  <RoastChart data={chartData} />
  <div class="legend-note">
    <span><i class="solid"></i> 实测温度</span>
    <span><i class="dashed"></i> 线性插值（探针短暂失联，仅跨 {chartData.params?.max_interp_gap_s ?? 20}s 以内小缺口）</span>
    <span><i class="band"></i> 缺测过长，未插值</span>
    <span>温升率：{detail.bean.ror_window?.description}</span>
  </div>
{/if}

{#if compareData}
  <div class="grid2">
        <MetricsPanel detail={compareData.a} />
        <MetricsPanel detail={compareData.b} />
  </div>
{:else if detail}
  <div class="grid2">
    <MetricsPanel {detail} />
    <EventEditor {detail} {revisions} on:revised={refresh} />
  </div>
{/if}

{#if chartData}
  <footer>
    <ul>
      {#each (detail.caveats || []) as c}<li>{c}</li>{/each}
    </ul>
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
  .chip { font-weight:600; }
  .chip.a { border-color:#c0392b; } .chip.b { border-color:#d68910; }
  .hint { font-size:12px; color:#666; }
  .error { color:#c0392b; padding:8px 18px; }
  .loading { padding:8px 18px; color:#888; }
  .legend-note { display:flex; gap:18px; flex-wrap:wrap; padding:6px 18px; font-size:12px; color:#444; align-items:center; }
  .legend-note i { display:inline-block; width:22px; height:0; border-top-width:2px; border-top-style:solid; vertical-align:middle; margin-right:4px; }
  i.solid { border-top-color:#c0392b; }
  i.dashed { border-top-style:dashed; border-top-color:#c0392b; }
  i.band { height:12px; border:1px dashed #999; background:rgba(120,120,120,0.15); }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; padding:8px 18px; }
  @media (max-width: 980px) { .grid2 { grid-template-columns:1fr; } }
  footer { padding:8px 18px 24px; font-size:12px; color:#666; }
  footer ul { margin:0; padding-left:18px; }
</style>
