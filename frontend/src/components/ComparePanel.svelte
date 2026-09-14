<script>
  export let data; // /api/compare 返回
  $: c = data.comparability;
  $: issues = data.anchor_issues || [];
  $: phase = data.phase;
  const SEV = { error: '错误', warning: '警告', info: '提示' };
</script>

<div class="card">
  <h3>可比性判定 <span class="sub">对齐是可视化归一化，不代表工艺等效</span></h3>
  <div class="headline" class:partial={!c.absolute_temperature_comparable || c.charge_delta_g >= 100}>
    {c.headline}
  </div>
  <table>
    <tbody>
      <tr><td>同一配方</td><td>{c.same_recipe ? '是' : '否'}</td></tr>
      <tr><td>探针位置</td><td>A: {c.probe_a} / B: {c.probe_b}
        {#if !c.same_probe_position}<span class="warn">不同</span>{/if}</td></tr>
      <tr><td>锅量</td><td>A {c.charge_a_g}g / B {c.charge_b_g}g（差 {c.charge_delta_g}g）</td></tr>
      <tr>
        <td>绝对温度水平</td>
        <td class:ok={c.absolute_temperature_comparable} class:no={!c.absolute_temperature_comparable}>
          {c.absolute_temperature_comparable ? '可比' : '不可直接比'}</td>
      </tr>
      <tr><td>RoR 形态 / 阶段速度</td>
        <td class:ok={c.ror_shape_comparable} class:no={!c.ror_shape_comparable}>
          {c.ror_shape_comparable ? '可参考' : '不建议比较'}</td></tr>
    </tbody>
  </table>
  <ul class="notes">
    {#each c.notes as n}<li>{n}</li>{/each}
  </ul>

  <h4>事件锚点与对齐问题</h4>
  {#if issues.length === 0}<p class="ok">五个热事件双侧齐全、顺序一致，可全段对齐。</p>{/if}
  <ul class="issues">
    {#each issues as i}
      <li class={i.severity}><b>[{SEV[i.severity]}]</b> {i.reason}</li>
    {/each}
  </ul>

  {#if phase && phase.available}
    <h4>分段对齐明细（真实时长，速度差异保留在此）</h4>
    <table>
      <thead><tr><th>阶段</th><th>A 真实 s</th><th>B 真实 s</th><th>差 s</th><th>A拉伸</th><th>B拉伸</th></tr></thead>
      <tbody>
        {#each phase.segments as s}
          <tr>
            <td>{s.label}</td><td>{s.a_duration_s}</td><td>{s.b_duration_s}</td>
            <td class:fast={s.duration_delta_s !== 0}>{s.duration_delta_s}</td>
            <td>{s.a_stretch}</td><td>{s.b_stretch}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="cov">{phase.coverage.tail_unaligned}</p>
    <p class="rule">{phase.ror_rule}</p>
    <p class="rule warn2">{phase.not_equivalence}</p>
  {:else if phase}
    <p class="no">相位对齐不可用：{phase.reason}</p>
  {/if}
</div>

<style>
  .card { background:#fff; border:1px solid #e3e3e3; border-radius:8px; padding:14px; }
  h3 { margin:0 0 8px; font-size:15px; }
  .sub { font-size:11px; color:#888; font-weight:400; margin-left:6px; }
  h4 { margin:14px 0 6px; font-size:13px; }
  .headline { font-weight:600; padding:8px 10px; border-radius:6px; background:#e8f6ee; color:#1e7d46; margin-bottom:8px; }
  .headline.partial { background:#fdf3e2; color:#9c6b00; }
  table { border-collapse:collapse; width:100%; font-size:12.5px; }
  td,th { border-bottom:1px solid #eee; padding:4px 8px; text-align:left; }
  .ok { color:#1e7d46; font-weight:600; }
  .no { color:#c0392b; font-weight:600; }
  .warn { color:#b9770e; margin-left:6px; font-size:11px; }
  .notes, .issues { margin:6px 0 0; padding-left:18px; font-size:12px; }
  .notes li { color:#555; margin:2px 0; }
  .issues li { margin:3px 0; }
  .issues .error { color:#c0392b; }
  .issues .warning { color:#b9770e; }
  .issues .info { color:#666; }
  .fast { color:#1f618d; }
  .cov { font-size:11.5px; color:#666; margin:6px 0; }
  .rule { font-size:11.5px; color:#444; background:#f6f6f6; padding:6px 8px; border-radius:4px; }
  .warn2 { color:#9c6b00; background:#fdf6e3; }
</style>
