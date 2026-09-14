<script>
  import { api } from '../api.js';

  export let batchId;
  export let onChange;

  const KIND = {
    charge: '下豆', turnaround: '回温点', yellow: '变黄',
    first_crack: '一爆', drop: '出豆', damper: '风门', gas: '燃气'
  };
  let candidates = [];
  let kind = 'first_crack';
  let t = '';
  let reason = '';
  let busy = false;
  let err = '';

  async function load() {
    if (!batchId) return;
    candidates = await api.candidates(batchId);
  }
  $: if (batchId) load();

  async function propose() {
    if (t === '') return;
    busy = true; err = '';
    try {
      await api.proposeCandidate(batchId, {
        kind, proposed_t_s: Number(t), reason: reason || '候选修订（待确认）'
      });
      t = ''; reason = '';
      await load();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function decide(id, accept) {
    busy = true;
    try {
      const r = await api.decideCandidate(id, accept);
      if (accept) alert(`已接受，落成正式人工修正（新事件 id=${r.applied_event.id}）`);
      await load();
      onChange && onChange();
    } catch (e) {
      alert(e.message);
    } finally {
      busy = false;
    }
  }

  $: pending = candidates.filter((x) => x.status === 'proposed');
  $: decided = candidates.filter((x) => x.status !== 'proposed');
</script>

<div class="card">
  <h3>候选修订（不改当前标记，待确认）</h3>
  {#if err}<div class="err">{err}</div>{/if}
  <div class="row">
    <select bind:value={kind}>
      {#each Object.keys(KIND) as k}<option value={k}>{KIND[k]}</option>{/each}
    </select>
    <input type="number" placeholder="建议时间 s" bind:value={t} min="0" step="0.5" />
    <input type="text" placeholder="依据/原因" bind:value={reason} />
    <button on:click={propose} disabled={busy}>建立候选</button>
  </div>

  {#if pending.length}
    <table>
      <thead><tr><th>事件</th><th>建议时间</th><th>原因</th><th>提出人</th><th></th></tr></thead>
      <tbody>
        {#each pending as x (x.id)}
          <tr>
            <td>{KIND[x.kind]}</td><td>{x.proposed_t_s}</td><td>{x.reason}</td><td>{x.proposed_by}</td>
            <td>
              <button on:click={() => decide(x.id, true)} disabled={busy}>接受</button>
              <button on:click={() => decide(x.id, false)} disabled={busy}>拒绝</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="empty">暂无待确认候选。候选接受前不会改变已确认事件，也不影响对齐。</p>
  {/if}

  {#if decided.length}
    <h4>已处理</h4>
    <table>
      <tbody>
        {#each decided as x (x.id)}
          <tr><td>{KIND[x.kind]}</td><td>{x.proposed_t_s}</td>
            <td class={x.status}>{x.status === 'accepted' ? '已接受→正式修正' : '已拒绝'}</td></tr>
      {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .card { background:#fff; border:1px solid #e3e3e3; border-radius:8px; padding:14px; }
  h3 { margin:0 0 10px; font-size:15px; }
  h4 { margin:12px 0 4px; font-size:13px; }
  .row { display:flex; gap:6px; flex-wrap:wrap; }
  select,input,button { padding:4px 6px; border:1px solid #bbb; border-radius:4px; font-size:12.5px; }
  button { background:#fafafa; cursor:pointer; margin-right:4px; }
  table { border-collapse:collapse; width:100%; font-size:12.5px; margin-top:8px; }
  td,th { border-bottom:1px solid #eee; padding:4px 8px; text-align:left; }
  .err { color:#c0392b; font-size:12.5px; }
  .empty { color:#888; font-size:12px; }
  .accepted { color:#1e7d46; }
  .rejected { color:#999; }
</style>
