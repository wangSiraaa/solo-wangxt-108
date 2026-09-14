<script>
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import * as echarts from 'echarts';

  export let data = null; // 单批次 detail 或对比 {a,b}
  export let height = '620px';

  const dispatch = createEventDispatcher();
  let el;
  let chart;

  const KIND = {
    charge: '下豆', turnaround: '回温点', yellow: '变黄',
    first_crack: '一爆', drop: '出豆', damper: '风门', gas: '燃气'
  };
  const COLORS_A = { bean: '#c0392b', env: '#2e86c1' };
  const COLORS_B = { bean: '#d68910', env: '#138d75' };

  function thermalLines(detail, palette, suffix) {
    const ev = (detail.events || []).filter((e) =>
      ['charge', 'turnaround', 'yellow', 'first_crack', 'drop'].includes(e.kind)
    );
    return ev.map((e) => ({
      xAxis: e.t_s,
      label: {
        formatter: `${KIND[e.kind]}${suffix}`,
        position: 'insideEndTop',
        fontSize: 10,
        color: palette.bean
      },
      lineStyle: { color: palette.bean, type: 'solid', width: 1, opacity: 0.55 },
      _event: e
    }));
  }

  function damperAnnotations(detail, palette) {
    // 风门/燃气作为图上文字注释，明确属于"操作事件"而非热事件
    return (detail.events || [])
      .filter((e) => e.kind === 'damper' || e.kind === 'gas')
      .map((e) => ({
        xAxis: e.t_s,
        label: {
          formatter: `${KIND[e.kind]}→${e.value}`,
          position: 'insideStartTop',
          fontSize: 9,
          color: '#7d6608'
        },
        lineStyle: { color: '#b7950b', type: 'dotted', width: 1, opacity: 0.7 }
      }));
  }

  function missingBands(detail) {
    return (detail.bean.gaps || [])
      .filter((g) => !g.interpolated)
      .map((g) => [
        { xAxis: g.start, itemStyle: { color: 'rgba(120,120,120,0.15)' } },
        { xAxis: g.end }
      ]);
  }

  function buildSeries(detail, palette, suffix, yIndexBase) {
    const opSuffix = detail._faint ? 0.28 : 1;
    return [
      {
        name: `豆温实测${suffix}`,
        type: 'line',
        data: detail.bean.observed,
        showSymbol: false,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { width: 1.6, color: palette.bean, opacity: opSuffix },
        itemStyle: { color: palette.bean },
        connectNulls: false
      },
      {
        name: `豆温插值${suffix}(探针短暂失联,线性)`,
        type: 'line',
        data: detail.bean.interpolated,
        showSymbol: false,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { width: 1.2, type: 'dashed', color: palette.bean, opacity: 0.75 * opSuffix },
        itemStyle: { color: palette.bean },
        z: 2
      },
      {
        name: `环境温度实测${suffix}`,
        type: 'line',
        data: detail.env.observed,
        showSymbol: false,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { width: 1.2, color: palette.env, opacity: 0.8 * opSuffix }
      },
      {
        name: `环境温度插值${suffix}`,
        type: 'line',
        data: detail.env.interpolated,
        showSymbol: false,
        xAxisIndex: 0, yAxisIndex: 0,
        lineStyle: { width: 1.0, type: 'dashed', color: palette.env, opacity: 0.55 * opSuffix }
      },
      {
        name: `温升率RoR${suffix}`,
        type: 'line',
        data: (detail.bean.ror || []).map((p) => [p.t_s, p.v]),
        showSymbol: false,
        xAxisIndex: 1, yAxisIndex: 1,
        lineStyle: { width: 1.8, color: palette.bean, opacity: opSuffix }
      }
    ];
  }

  function option() {
    const series = [];
    const marks = [];
    const bands = [];
    let p;
    if (!data) {
      return { animation: false, series: [], xAxis: [], yAxis: [], grid: [] };
    }
    if (data.a && data.b) {
      p = data.params;
      series.push(...buildSeries(data.a, COLORS_A, ` ${data.a.batch.name.slice(-1)}`, 0));
      series.push(...buildSeries(data.b, COLORS_B, ` ${data.b.batch.name.slice(-1)}`, 0));
      marks.push(...thermalLines(data.a, COLORS_A, 'A'), ...thermalLines(data.b, COLORS_B, 'B'));
      marks.push(...damperAnnotations(data.a, COLORS_A), ...damperAnnotations(data.b, COLORS_B));
      bands.push(...missingBands(data.a), ...missingBands(data.b));
    } else {
      p = data.params;
      series.push(...buildSeries(data, COLORS_A, '', 0));
      marks.push(...thermalLines(data, COLORS_A, ''));
      marks.push(...damperAnnotations(data, COLORS_A));
      bands.push(...missingBands(data));
    }

    if (series[0]) {
      series[0].markLine = {
        symbol: 'none',
        silent: false,
        data: marks,
      };
      series[0].markArea = { silent: true, data: bands };
    }

    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(2))
      },
      legend: { top: 0, type: 'scroll' },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 56, right: 24, top: 44, height: '56%' },
        { left: 56, right: 24, top: '74%', height: '18%' }
      ],
      xAxis: [
        { type: 'value', name: '相对下豆点 s', gridIndex: 0 },
        { type: 'value', name: '相对下豆点 s', gridIndex: 1 }
      ],
      yAxis: [
        { type: 'value', name: '温度 ℃', gridIndex: 0, min: 15 },
        {
          type: 'value', name: `RoR ℃/min (${p.ror_window_s}s居窗)`,
          gridIndex: 1, min: 0
        }
      ],
      series
    };
  }

  onMount(() => {
    chart = echarts.init(el);
    chart.setOption(option());
    // 只读调试句柄，便于浏览器自动化校验双网格/系列，不参与任何渲染逻辑
    if (typeof window !== 'undefined') window.__roastChart = chart;
    chart.on('click', (p2) => {
      const ev = p2?.data?._event;
      if (ev) dispatch('markclick', { event: ev });
    });
    const onResize = () => chart.resize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  });

  $: if (chart && data) {
    chart.setOption(option(), { notMerge: true });
  }

  onDestroy(() => chart && chart.dispose());
</script>

<div bind:this={el} style="width:100%;height:{height};"></div>
