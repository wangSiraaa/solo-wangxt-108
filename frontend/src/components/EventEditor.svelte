<script>
  import { createEventDispatcher } from 'svelte';
  import { api } from '../api.js';

  export let detail;
  export let revisions = [];
  export let onChange;

  const dispatch = createEventDispatcher();
  let busy = false;
  let err = '';
  let draftKind = 'turnaround';
  let draftT = '';
  let draftValue = '';
  let draftNote = '';
  const KIND = {
    charge: '下豆', turnaround: '回温点', yellow: '变黄',
    first_crack: '一爆', drop: '出豆', damper: '风门', gas: '燃气'
  };

  async function revise(ev) {
    const nt = prompt(
      `人工修正「${KIND[ev.kind]}」时间（当前 ${ev.t_s}s，来源 ${ev.source}）\n输入新的时间（相对下豆点，秒）：`,
      ev.t_s
    );
    if (nt === null) return;
    const newT = Number(nt);
    if (!(newT >= 0)) {
      err = '时间无效';
      return;
    }
    const reason = prompt('修正原因（将与来源一起保留）：', '人工复核') || '人工复核';
    let value = null;
    if (ev.kind === 'damper' || ev.kind === 'gas') {
      const nv = prompt(`${KIND[ev.kind]}档位 0-100（当前 ${ev.value ?? '—'}）：`, ev.value);
      value = nv === null ? ev.value : Number(nv);
    }
    busy = true;
    err = '';
    try {
      await api.revise(ev.id, { new_t_s: newT, new_value: value, reason, revised_by: 'operator' });
      dispatch('revised');
      onChange && onChange();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function addManual() {
    if (draftT === '') return;
    busy = true;
    err = '';
    try {
      await api.addEvent(detail.batch.id, {
        kind: draftKind,
        t_s: Number(draftT),
        value: draftValue === '' ? null : Number(draftValue),
        note: draftNote || '人工新增标记'
      });
      dispatch('revised');
      onChange && onChange();
      draftT = ''; draftValue = ''; draftNote = '';
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  $: thermal = detail.events.filter((e) =>
    ['charge', 'turnaround', 'yellow', 'first_crack', 'drop'].includes(e.kind));
  $: ops = detail.events.filter((e) => e.kind === 'damper' || e.kind === 'gas');
</script>

<div class="card">
  <h3>事件标记（可人工修正，保留来源与历史）</h3>
  {#if err}<div class="err">{err}</div>{/if}
  <table>
    <thead><tr><th>事件</th><th>时间 s</th><th>档位</th><th>来源</th><th></th></tr></thead>
    <tbody>
      {#each thermal as e (e.id)}
        <tr>
          <td>{KIND[e.kind]}</td>
          <td>{e.t_s}</td>
          <td>—</td>
          <td class:manual={e.source === 'manual'}>{e.source === 'manual' ? '人工' : '自动'}</td>
          <td><button disabled={busy} on:click={() => revise(e)}>修正</button></td>
        </tr>
      {/each}
      {#each ops as e (e.id)}
        <tr class="op">
          <td>{KIND[e.kind]}</td>
          <td>{e.t_s}</td>
          <td>{e.value}</td>
          <td class:manual={e.source === 'manual'}>{e.source === 'manual' ? '人工' : '自动'}</td>
          <td><button disabled={busy} on:click={() => revise(e)}>修正</button></td>
        </tr>
      {/each}
    </tbody>
  </table>

  <h4>人工新增操作标记</h4>
  <div class="row">
    <select bind:value={draftKind}>
      {#each Object.keys(KIND) as k}<option value={k}>{KIND[k]}</option>{/each}
    </select>
    <input type="number" placeholder="时间 s" bind:value={draftT} min="0" step="0.5" />
    {#if draftKind === 'damper' || draftKind === 'gas'}
      <input type="number" placeholder="档位 0-100" bind:value={draftValue} min="0" max="100" />
    {/if}
    <input type="text" placeholder="备注" bind:value={draftNote} />
    <button on:click={addManual} disabled={busy}>新增</button>
  </div>

  {#if revisions.length}
    <h4>修正历史（审计轨迹）</h4>
    <table class="rev">
      <thead><tr><th>事件</th><th>旧时间</th><th>旧来源</th><th>新时间</th><th>新来源</th><th>原因</th><th>操作人</th></tr></thead>
      <tbody>
        {#each revisions as r (r.id)}
          <tr>
            <td>{KIND[r.kind]}</td>
            <td>{r.old_t_s}</td>
            <td>{r.old_source === 'manual' ? '人工' : '自动'}</td>
            <td>{r.new_t_s}</td>
            <td>人工</td>
            <td>{r.reason}</td>
            <td>{r.revised_by}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .card { background: #fff; border: 1px solid #e3e3e3; border-radius: 8px; padding: 14px; }
  h3 { margin: 0 0 10px; font-size: 15px; }
  h4 { margin: 14px 0 6px; font-size: 13px; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { border-bottom: 1px solid #eee; padding: 4px 8px; text-align: left; }
  tr.op td { color: #7d6608; }
  .manual { color: #c0392b; font-weight: 600; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  input, select { padding: 4px 6px; border: 1px solid #ccc; border-radius: 4px; font-size: 12.5px; }
  button { padding: 4px 10px; border: 1px solid #888; border-radius: 4px; background: #fafafa; cursor: pointer; }
  .err { color: #c0392b; margin-bottom: 6px; font-size: 12.5px; }
</style>
