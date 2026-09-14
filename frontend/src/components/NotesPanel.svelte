<script>
  import { api } from '../api.js';

  // compare 模式给 aId+bId（可创建并按当前对齐模式过滤）；
  // 批次详情模式只给 aId（列出该批次相关全部备注，不可创建新对比备注）。
  export let aId = null;
  export let bId = null;
  export let alignment = 'physical';
  export let smooth = 21;
  export let ror = 45;

  const STATUS = { followup: '待跟进', confirmed: '已确认', discarded: '已废弃' };
  const ALN = { physical: '物理对齐', phase: '相位对齐' };

  let notes = [];
  let statusFilter = 'all';
  let showCreate = false;
  let conclusion = '';
  let owner = 'operator';
  let due = '';
  let newStatus = 'followup';
  let busy = false;
  let err = '';
  let expanded = null; // 展开的备注 id（看快照/状态轨迹）
  let revisions = {};

  async function load() {
    if (aId == null) { notes = []; return; }
    const p = { batch_id: aId };
    if (bId != null) p.other_id = bId;
    notes = await api.notes(p);
  }
  // aId/bId/比较对变化都要重新加载，并收起创建表单
  $: aId, bId, (showCreate = false), load();

  // 只显示与当前对齐模式一致的备注（切批次/对齐方式后随之变化）
  $: filtered = notes.filter((n) =>
    (bId == null || n.alignment === alignment) &&
    (statusFilter === 'all' || n.status === statusFilter)
  );

  async function create() {
    if (!conclusion.trim() || bId == null) return;
    busy = true; err = '';
    try {
      await api.createNote({
        a: aId, b: bId, alignment,
        smooth_window_s: smooth, ror_window_s: ror,
        conclusion, status: newStatus, owner: owner || null,
        due_date: due || null,
      });
      conclusion = ''; due = ''; showCreate = false;
      await load();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function setStatus(n, status) {
    const reason = prompt(`把备注状态改为「${STATUS[status]}」的原因：`, '') || '';
    busy = true;
    try {
      await api.setNoteStatus(n.id, { status, reason });
      await load();
    } catch (e) {
      alert(e.message);
    } finally {
      busy = false;
    }
  }

  async function toggleHistory(n) {
    if (expanded === n.id) { expanded = null; return; }
    expanded = n.id;
    if (!revisions[n.id]) revisions[n.id] = await api.noteRevisions(n.id);
    revisions = revisions;
  }

  function snapshotEvent(n, side, kind) {
    const ev = (side === 'a' ? n.event_version_a : n.event_version_b)
      .find((e) => e.kind === kind);
    return ev ? ev.t_s : '—';
  }
</script>

<div class="card">
  <h3>
    对比结论备注与待办
    {#if bId != null}
      <span class="cur">当前：{ALN[alignment]}</span>
      <button class="add" on:click={() => (showCreate = !showCreate)}>
        {showCreate ? '收起' : '＋ 新建备注'}
      </button>
    {/if}
  </h3>

  {#if bId != null && showCreate}
    <div class="form">
      <p class="bind">将绑定当前批次对、{ALN[alignment]}、平滑 {smooth}s / RoR {ror}s
        及两批当前事件版本快照；之后事件修正不改变本备注依据。</p>
      <textarea bind:value={conclusion} rows="3" placeholder="结论 / 待办内容（描述性对比，不宣称因果）"></textarea>
      <div class="row">
        <label>状态
          <select bind:value={newStatus}>
            <option value="followup">待跟进</option>
            <option value="confirmed">已确认</option>
            <option value="discarded">已废弃</option>
          </select>
        </label>
        <label>责任人 <input bind:value={owner} placeholder="负责人" /></label>
        <label>截止 <input type="date" bind:value={due} /></label>
        <button on:click={create} disabled={busy || !conclusion.trim()}>保存备注</button>
      </div>
      {#if err}<div class="err">{err}</div>{/if}
    </div>
  {/if}

  <div class="filters">
    <span>状态：</span>
    {#each [['all','全部'],['followup','待跟进'],['confirmed','已确认'],['discarded','已废弃']] as [v, label]}
      <button class:active={statusFilter === v} on:click={() => (statusFilter = v)}>{label}</button>
    {/each}
  </div>

  {#if filtered.length === 0}
    <p class="empty">
      {bId != null ? '当前批次对 + ' + ALN[alignment] + ' 下暂无备注' : '该批次暂无关联备注'}
    </p>
  {/if}

  {#each filtered as n (n.id)}
    <div class="note {n.status}">
      <div class="note-head">
        <span class="badge {n.status}">{STATUS[n.status]}</span>
        <span class="meta">{ALN[n.alignment]} · 平滑{n.smooth_window_s}/RoR{n.ror_window_s}s
          {#if bId == null}· {n.batch_a_name} ↔ {n.batch_b_name}{/if}</span>
      </div>
      <p class="concl">{n.conclusion}</p>
      <div class="note-foot">
        <span>责任人：{n.owner || '—'}{#if n.due_date} · 截止 {n.due_date}{/if}</span>
        <span class="muted">创建 {n.created_at.slice(0, 16)} · {n.created_by}</span>
      </div>
      <div class="actions">
        {#if n.status !== 'followup'}<button on:click={() => setStatus(n, 'followup')} disabled={busy}>标记待跟进</button>{/if}
        {#if n.status !== 'confirmed'}<button on:click={() => setStatus(n, 'confirmed')} disabled={busy}>确认</button>{/if}
        {#if n.status !== 'discarded'}<button on:click={() => setStatus(n, 'discarded')} disabled={busy}>废弃</button>{/if}
        <button on:click={() => toggleHistory(n)}>快照/轨迹 ({n.n_revisions})</button>
      </div>
      {#if expanded === n.id}
        <div class="snap">
          <div><b>创建时事件版本快照</b>（后续修正不影响）：</div>
          <table>
            <thead><tr><th>事件</th><th>A 时间</th><th>B 时间</th></tr></thead>
            <tbody>
              {#each ['charge','turnaround','yellow','first_crack','drop'] as k}
                <tr><td>{k}</td><td>{snapshotEvent(n, 'a', k)}</td><td>{snapshotEvent(n, 'b', k)}</td></tr>
              {/each}
            </tbody>
          </table>
          <div class="revtitle">状态轨迹：</div>
          <ul class="revs">
            {#each revisions[n.id] || [] as r}
              <li>{r.changed_at.slice(0, 16)} · {r.old_status ? STATUS[r.old_status] : '（创建）'}
                → {STATUS[r.new_status]} · {r.reason} · {r.changed_by}</li>
            {/each}
          </ul>
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .card { background:#fff; border:1px solid #e3e3e3; border-radius:8px; padding:14px; margin:8px 18px; }
  h3 { margin:0 0 10px; font-size:15px; display:flex; align-items:center; gap:10px; }
  .cur { font-size:11.5px; color:#666; font-weight:400; }
  .add { margin-left:auto; }
  .form { background:#fafafa; border:1px solid #eee; border-radius:6px; padding:10px; margin-bottom:10px; }
  .bind { font-size:11.5px; color:#8a6d00; background:#fdf6e3; padding:6px 8px; border-radius:4px; margin:0 0 8px; }
  textarea { width:100%; box-sizing:border-box; border:1px solid #ccc; border-radius:4px; padding:6px; font-size:12.5px; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:8px; font-size:12.5px; }
  input,select,button { padding:4px 6px; border:1px solid #bbb; border-radius:4px; font-size:12.5px; }
  button { background:#fafafa; cursor:pointer; }
  .filters { display:flex; gap:6px; align-items:center; margin-bottom:8px; font-size:12.5px; flex-wrap:wrap; }
  .filters button.active { background:#2c3e50; color:#fff; border-color:#2c3e50; }
  .empty { color:#888; font-size:12.5px; }
  .note { border:1px solid #eee; border-left-width:4px; border-radius:6px; padding:8px 10px; margin:8px 0; }
  .note.followup { border-left-color:#b9770e; }
  .note.confirmed { border-left-color:#1e7d46; }
  .note.discarded { border-left-color:#999; opacity:0.72; }
  .badge { font-size:11px; padding:1px 8px; border-radius:10px; color:#fff; }
  .badge.followup { background:#b9770e; }
  .badge.confirmed { background:#1e7d46; }
  .badge.discarded { background:#888; }
  .meta { font-size:11.5px; color:#777; margin-left:8px; }
  .concl { font-size:13px; margin:6px 0; white-space:pre-wrap; }
  .note-foot { display:flex; justify-content:space-between; font-size:11.5px; color:#555; }
  .muted { color:#999; }
  .actions { margin-top:6px; display:flex; gap:6px; flex-wrap:wrap; }
  .snap { margin-top:8px; border-top:1px dashed #ddd; padding-top:8px; font-size:11.5px; color:#444; }
  .snap table { border-collapse:collapse; margin:6px 0; }
  .snap td,.snap th { border:1px solid #eee; padding:2px 8px; }
  .revtitle { margin-top:6px; }
  .revs { margin:4px 0 0; padding-left:18px; }
  .err { color:#c0392b; font-size:12px; margin-top:6px; }
</style>
