<script>
  export let detail;
  const KIND = {
    charge: '下豆', turnaround: '回温点', yellow: '变黄',
    first_crack: '一爆', drop: '出豆'
  };
  $: m = detail.metrics;
</script>

<div class="card">
  <h3>阶段指标（按事件时间的明确区间计算）</h3>
  {#if !m.ok}
    <div class="warn">缺少事件：{m.missing_events.join(', ')}，无法计算完整阶段</div>
  {:else}
    <table>
      <thead>
        <tr><th>阶段</th><th>区间</th><th>时长 s</th><th>起始豆温 ℃</th><th>结束豆温 ℃</th><th>平均RoR ℃/min</th><th>RoR覆盖率</th></tr>
      </thead>
      <tbody>
        {#each m.phases as p}
          <tr>
            <td>{p.label}</td>
            <td class="mono">{KIND[p.interval.from]}→{KIND[p.interval.to]}<br />
              <small>{p.interval.start_s}→{p.interval.end_s}s</small></td>
            <td>{p.duration_s}</td>
            <td>{p.start_bean_c ?? '缺测'}</td>
            <td>{p.end_bean_c ?? '缺测'}</td>
            <td>{p.ror?.avg_ror_c_per_min ?? '—'}</td>
            <td>{p.ror ? (p.ror.coverage * 100).toFixed(0) + '%' : '—'}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="ratio">
      <strong>发展时间比 = {m.development_ratio}</strong>
      <div class="formula">{m.development_ratio_formula}</div>
      <div class="formula">= {m.development_s} / {m.total_after_turnaround_s} s
        （回温点豆温 {m.turnaround_bean_c ?? '缺测'} ℃）</div>
    </div>
  {/if}

  <h4>风门/燃气变化前后（±{detail.operation_windows[0]?.window_s ?? 60}s 描述性对比，非因果）</h4>
  <table>
    <thead><tr><th>操作</th><th>t s</th><th>档位</th><th>前窗豆温</th><th>后窗豆温</th><th>前窗RoR</th><th>后窗RoR</th></tr></thead>
    <tbody>
      {#each detail.operation_windows as o}
        <tr>
          <td>{o.label}</td><td>{o.t_s}</td><td>{o.value}</td>
          <td>{o.before.bean ?? '—'}</td><td>{o.after.bean ?? '—'}</td>
          <td>{o.before.ror ?? '—'}</td><td>{o.after.ror ?? '—'}</td>
        </tr>
      {/each}
    </tbody>
  </table>
  <p class="note">前后窗均值仅描述同一批次操作时刻附近的曲线形态；
    温度同时受豆量、燃气、蓄热等因素共同影响，本视图不宣称风门变化导致了曲线变化。</p>
</div>

<style>
  .card { background:#fff; border:1px solid #e3e3e3; border-radius:8px; padding:14px; }
  h3 { margin:0 0 10px; font-size:15px; }
  h4 { margin:14px 0 6px; font-size:13px; }
  table { border-collapse:collapse; width:100%; font-size:12.5px; }
  th,td { border-bottom:1px solid #eee; padding:4px 8px; text-align:left; }
  .mono { font-variant-numeric: tabular-nums; }
  .ratio { margin-top:10px; padding:8px 10px; background:#fdf6e3; border-radius:6px; }
  .formula { font-size:11.5px; color:#666; }
  .note { font-size:11.5px; color:#666; margin:6px 0 0; }
  .warn { color:#c0392b; }
</style>
