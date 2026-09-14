<script>
  // 相位对齐图：x 轴为“规范阶段时间”。所有系列都来自后端 phase.series，
  // 其中 RoR 只是真实时间 RoR 点的位置重映射（带 src_t_s），前端绝不重新求导。
  import { onMount, onDestroy } from 'svelte';
  import * as echarts from 'echarts';

  export let data = null; // /api/compare?alignment=phase 的返回

  let el;
  let chart;
  const CA = { bean: '#c0392b', env: '#2e86c1' };
  const CB = { bean: '#d68910', env: '#138d75' };

  function opt() {
    const ph = data.phase;
    const s = ph.series;
    const mk = (name, arr, color, opt2 = {}) => ({
      name, type: 'line', showSymbol: false, data: arr,
      lineStyle: { width: 1.5, color, ...(opt2.lineStyle || {}) },
      itemStyle: { color }, ...opt2.rest
    });
    const anchorLines = ph.canonical_anchors.map((an) => ({
      xAxis: an.x_s,
      label: {
        formatter: `${an.label}\nA${an.real_t_a}|B${an.real_t_b}`,
        position: 'insideEndTop', fontSize: 9, color: '#555'
      },
      lineStyle: { color: '#7f8c8d', type: 'solid', width: 1, opacity: 0.5 }
    }));

    const series = [
      mk('豆温实测 A', s.bean_observed_a, CA.bean, { rest: { xAxisIndex: 0, yAxisIndex: 0 } }),
      mk('豆温实测 B', s.bean_observed_b, CB.bean, { rest: { xAxisIndex: 0, yAxisIndex: 0 } }),
      mk('环境温度 A', s.env_observed_a, CA.env, { rest: { xAxisIndex: 0, yAxisIndex: 0, lineStyle: { opacity: 0.7 } } }),
      mk('环境温度 B', s.env_observed_b, CB.env, { rest: { xAxisIndex: 0, yAxisIndex: 0, lineStyle: { opacity: 0.7 } } }),
      mk('RoR A (真实时间值)', s.ror_a.map((p) => [p.t_s, p.v]), CA.bean,
        { rest: { xAxisIndex: 1, yAxisIndex: 1, lineStyle: { width: 1.8 } } }),
      mk('RoR B (真实时间值)', s.ror_b.map((p) => [p.t_s, p.v]), CB.bean,
        { rest: { xAxisIndex: 1, yAxisIndex: 1, lineStyle: { width: 1.8 } } })
    ];
    series[0].markLine = { symbol: 'none', data: anchorLines };

    return {
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { top: 0, type: 'scroll' },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 56, right: 24, top: 56, height: '52%' },
        { left: 56, right: 24, top: '72%', height: '18%' }
      ],
      xAxis: [
        { type: 'value', name: '规范阶段时间 s（分段对齐，非真实时刻）', gridIndex: 0 },
        { type: 'value', name: '规范阶段时间 s', gridIndex: 1 }
      ],
      yAxis: [
        { type: 'value', name: '温度 ℃', gridIndex: 0, min: 15 },
        { type: 'value', name: `RoR ℃/min (真实时间差分，${data.params.ror_window_s}s)`, gridIndex: 1, min: 0 }
      ],
      series
    };
  }

  onMount(() => {
    chart = echarts.init(el);
    if (typeof window !== 'undefined') window.__roastChart = chart;
    if (data && data.phase && data.phase.available) chart.setOption(opt());
  });
  $: if (chart && data && data.phase && data.phase.available) chart.setOption(opt(), { notMerge: true });
  onDestroy(() => {
    if (typeof window !== 'undefined') delete window.__roastChart;
    chart && chart.dispose();
  });
</script>

<div bind:this={el} style="width:100%;height:600px;"></div>
