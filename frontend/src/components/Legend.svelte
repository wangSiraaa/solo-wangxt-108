<script>
  export let chartData;
</script>

<div class="legend-note">
  <span><i class="solid"></i> 实测温度</span>
  <span><i class="dashed"></i> 线性插值（探针短暂失联，仅跨 {chartData?.params?.max_interp_gap_s ?? 20}s 以内小缺口）</span>
  <span><i class="band"></i> 缺测过长，未插值</span>
  {#if chartData?.bean?.ror_window}
    <span>温升率：{chartData.bean.ror_window.description}</span>
  {/if}
</div>

<style>
  .legend-note { display:flex; gap:18px; flex-wrap:wrap; padding:6px 18px; font-size:12px; color:#444; align-items:center; }
  .legend-note i { display:inline-block; width:22px; height:0; border-top-width:2px; border-top-style:solid; vertical-align:middle; margin-right:4px; }
  i.solid { border-top-color:#c0392b; }
  i.dashed { border-top-style:dashed; border-top-color:#c0392b; }
  i.band { height:12px; border:1px dashed #999; background:rgba(120,120,120,0.15); }
</style>
