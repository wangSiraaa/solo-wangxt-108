<script>
  import { api } from '../api.js';

  export let detail;
  export let onChange;

  const KINDS = ['turnaround', 'yellow', 'first_crack', 'drop'];
  const KIND = {
    charge: '下豆', turnaround: '回温点', yellow: '变黄',
    first_crack: '一爆', drop: '出豆', damper: '风门', gas: '燃气'
  };

  let tags = [];
  let eventFilter = 'all';
  let statusFilter = 'all';
  let kind = 'first_crack';
  let label = '';
  let desc = '';
  let busy = false;
  let err = '';
  let expanded = null;       // 展开完整处理历史的标签 id
  let resolving = {};        // 正在填写处理结论的标签 id

  const STATUS = { open: '待处理', adopted: '已采纳', ignored: '已忽略' };
  const STATUS_ORDER = ['open', 'adopted', 'ignored'];

  async function load() {
    tags = await api.tags(detail.batch.id);  // 全量，前端按事件/状态过滤
  }
  $: if (detail) load();
  $: shownTags = tags.filter((t) =>
    (eventFilter === 'all' || t.event_kind === eventFilter) &&
    (statusFilter === 'all' || t.status === statusFilter)
  );

  async function create() {
    if (!label.trim()) return;
    busy = true; err = '';
    try {
      await api.createTag(detail.batch.id, {
        event_kind: kind, label: label.trim(),
        description: desc || null, created_by: 'operator',
      });
      label = ''; desc = '';
      eventFilter = 'all'; statusFilter = 'all';
      await load();
      onChange && onChange();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function setStatus(t, status) {
    if (t.status === status) { resolving[t.id] = null; return; }
    const def = t.status ? `处理结论（${STATUS[t.status]}→${STATUS[status]}）` : '处理结论';
    const resolution = prompt(def, '') || '';
    busy = true;
    try {
      await api.setTagStatus(detail.batch.id, t.id, { status, resolution });
      resolving[t.id] = null;
      await load();
      onChange && onChange();
    } catch (e) {
      alert(e.message);
    } finally {
      busy = false;
    }
  }

  async function toggleHistory(t) {
    expanded = expanded === t.id ? null : t.id;
  }

  async function remove(t) {
    if (!confirm(`删除标签「${t.label}」？将同时删除其处理历史；不影响事件、事件修订、比较与备注。`)) return;
    busy = true;
    try {
      await api.deleteTag(detail.batch.id, t.id);
      await load();
      onChange && onChange();
    } catch (e) {
      alert(e.message);
    } finally {
      busy = false;
    }
  }

  // 当前有效事件时间映射
  $: currentByKind = Object.fromEntries(
    detail.events.filter((e) => KIND[e.kind]).map((e) => [e.kind, e])
  );
</script>

<div class="card">
  <h3>阶段复盘标签（绑定创建时事件版本）</h3>
  {#if err}<div class="err">{err}</div>{/if}

  <div class="form">
    <div class="row">
      <select bind:value={kind}>
        {#each KINDS as k}<option value={k}>{KIND[k]}</option>{/each}
      </select>
      <input bind:value={label} placeholder="标签，如 一爆判断偏晚 / 尾段火力过强" maxlength="40" />
    </div>
    <div class="row">
      <input class="desc" bind:value={desc} placeholder="简短说明（可选）" maxlength="120" />
      <button on:click={create} disabled={busy || !label.trim()}>添加标签</button>
    </div>
  </div>

  <div class="filters">
    <span class="f-label">事件</span>
    <button class:active={eventFilter === 'all'} on:click={() => (eventFilter = 'all')}>全部</button>
    {#each KINDS as k}
      <button class:active={eventFilter === k} on:click={() => (eventFilter = k)}>{KIND[k]}</button>
    {/each}
  </div>
  <div class="filters">
    <span class="f-label">状态</span>
    <button class:active={statusFilter === 'all'} on:click={() => (statusFilter = 'all')}>全部</button>
    {#each STATUS_ORDER as s}
      <button class:active={statusFilter === s} on:click={() => (statusFilter = s)}>{STATUS[s]}</button>
    {/each}
  </div>

  {#if shownTags.length === 0}
    <p class="empty">该筛选下暂无标签。</p>
  {/if}

  {#each shownTags as t (t.id)}
    <div class="tag {t.event_kind} {t.status}">
      <div class="tag-head">
        <span class="kind">{KIND[t.event_kind]}</span>
        <span class="statbadge {t.status}">{STATUS[t.status]}</span>
        <span class="labeltext">{t.label}</span>
        <button class="del" on:click={() => remove(t)} disabled={busy}>删除</button>
      </div>
      {#if t.description}<p class="desc-text">{t.description}</p>{/if}

      <div class="times">
        {#if t.changed}
          <span class="changed">⚠ 当前事件已变更</span>
          <span class="old">标签创建时：{t.event_t_s}s（{t.event_source === 'manual' ? '人工' : '自动'}版本）</span>
          <span class="new">当前：{t.current_event ? t.current_event.t_s + 's' : '事件缺失'}
            {#if t.current_event}（{t.current_event.source === 'manual' ? '人工' : '自动'}）{/if}</span>
        {:else}
          <span class="cur">✓ 与当前事件一致：{t.event_t_s}s</span>
        {/if}
      </div>

      <!-- 最近一次处理信息 -->
      {#if t.last_decision}
        <div class="last">
          <b>最近处理：</b>{STATUS[t.last_decision.new_status]}
          {#if t.last_decision.resolution}· {t.last_decision.resolution}{/if}
          <span class="lastmeta">{t.last_decision.changed_by} · {t.last_decision.changed_at.slice(0, 16)}</span>
        </div>
      {:else}
        <div class="last open">尚未处理（待跟进）。</div>
      {/if}

      <!-- 状态切换 + 完整历史 -->
      <div class="actions">
        {#each STATUS_ORDER as s}
          <button class="statebtn" class:current={t.status === s}
                  on:click={() => setStatus(t, s)} disabled={busy}>{STATUS[s]}</button>
        {/each}
        <button class="histbtn" on:click={() => toggleHistory(t)}>
          {expanded === t.id ? '收起历史' : `完整历史 (${t.status_history.length})`}
        </button>
      </div>
      {#if expanded === t.id}
        <ul class="history">
          {#each t.status_history as r}
            <li>
              <span class="h-time">{r.changed_at.slice(0, 16)}</span>
              <span class="h-change">{r.old_status ? STATUS[r.old_status] : '（创建）'} → {STATUS[r.new_status]}</span>
              {#if r.resolution}<span class="h-res">{r.resolution}</span>{/if}
              <span class="h-by">{r.changed_by}</span>
            </li>
          {/each}
        </ul>
      {/if}

      <div class="meta">标签创建：{t.created_by} · {t.created_at.slice(0, 16)}</div>
    </div>
  {/each}
</div>

<style>
  .card { background:#fff; border:1px solid #e3e3e3; border-radius:8px; padding:14px; }
  h3 { margin:0 0 10px; font-size:15px; }
  .form { background:#fafafa; border:1px solid #eee; border-radius:6px; padding:8px; margin-bottom:8px; }
  .row { display:flex; gap:6px; margin:4px 0; }
  select,input,button { padding:4px 6px; border:1px solid #bbb; border-radius:4px; font-size:12.5px; }
  input { flex:1; min-width:0; }
  input.desc { flex:2; }
  button { background:#fafafa; cursor:pointer; }
  .filters { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0; align-items:center; }
  .f-label { font-size:11.5px; color:#888; }
  .filters button.active { background:#2c3e50; color:#fff; border-color:#2c3e50; }
  .empty { color:#888; font-size:12.5px; }
  .tag { border:1px solid #eee; border-left-width:4px; border-radius:6px; padding:8px 10px; margin:8px 0; border-left-color:#7d8fa3; }
  .tag.first_crack { border-left-color:#c0392b; }
  .tag.drop { border-left-color:#8e44ad; }
  .tag.yellow { border-left-color:#d4ac0d; }
  .tag.turnaround { border-left-color:#2471a3; }
  .tag.open { background:#fffdf7; }
  .tag.adopted { background:#f7fdf9; }
  .tag.ignored { background:#f7f7f7; }
  .tag-head { display:flex; align-items:center; gap:8px; }
  .kind { font-size:11px; background:#eef2f5; border-radius:8px; padding:1px 8px; color:#34495e; }
  .statbadge { font-size:11px; border-radius:8px; padding:1px 8px; color:#fff; }
  .statbadge.open { background:#b9770e; }
  .statbadge.adopted { background:#1e7d46; }
  .statbadge.ignored { background:#888; }
  .labeltext { font-weight:600; font-size:13px; flex:1; }
  .del { color:#c0392b; }
  .desc-text { font-size:12px; color:#555; margin:5px 0; }
  .times { font-size:11.5px; display:flex; gap:12px; flex-wrap:wrap; margin:4px 0; }
  .changed { color:#c0392b; font-weight:700; }
  .old { color:#888; text-decoration:line-through; }
  .new { color:#c0392b; }
  .cur { color:#1e7d46; }
  .last { font-size:12px; background:#f4f6f8; border-radius:4px; padding:4px 8px; margin:4px 0; }
  .last.open { background:transparent; color:#99827a; padding-left:0; }
  .lastmeta { color:#888; margin-left:6px; }
  .actions { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0 2px; }
  .statebtn.current { background:#2c3e50; color:#fff; border-color:#2c3e50; }
  .histbtn { margin-left:auto; color:#555; }
  .history { list-style:none; margin:4px 0 0; padding:6px 8px; background:#fafafa; border:1px dashed #ddd; border-radius:4px; font-size:11.5px; }
  .history li { display:flex; gap:10px; flex-wrap:wrap; padding:2px 0; }
  .h-time { color:#999; min-width:100px; }
  .h-change { font-weight:600; min-width:110px; }
  .h-res { color:#444; flex:1; min-width:160px; }
  .h-by { color:#777; }
  .meta { font-size:11px; color:#999; }
  .err { color:#c0392b; font-size:12.5px; }
</style>
