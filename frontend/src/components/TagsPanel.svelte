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
  let filter = 'all';
  let kind = 'first_crack';
  let label = '';
  let desc = '';
  let busy = false;
  let err = '';

  async function load() {
    tags = await api.tags(detail.batch.id);  // 始终取全量，按 filter 前端过滤
  }
  $: if (detail) load();
  $: shownTags = filter === 'all' ? tags : tags.filter((t) => t.event_kind === filter);

  async function create() {
    if (!label.trim()) return;
    busy = true; err = '';
    try {
      await api.createTag(detail.batch.id, {
        event_kind: kind, label: label.trim(),
        description: desc || null, created_by: 'operator',
      });
      label = ''; desc = '';
      filter = 'all';
      await load();
      onChange && onChange();
    } catch (e) {
      err = e.message;
    } finally {
      busy = false;
    }
  }

  async function remove(t) {
    if (!confirm(`删除标签「${t.label}」？只删除标签，不影响事件与修订历史。`)) return;
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
    <button class:active={filter === 'all'} on:click={() => (filter = 'all')}>全部</button>
    {#each KINDS as k}
      <button class:active={filter === k} on:click={() => (filter = k)}>{KIND[k]}</button>
    {/each}
  </div>

  {#if shownTags.length === 0}
    <p class="empty">该筛选下暂无标签。</p>
  {/if}

  {#each shownTags as t (t.id)}
    <div class="tag {t.event_kind}">
      <div class="tag-head">
        <span class="kind">{KIND[t.event_kind]}</span>
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
      <div class="meta">{t.created_by} · {t.created_at.slice(0, 16)}</div>
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
  .filters { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0; }
  .filters button.active { background:#2c3e50; color:#fff; border-color:#2c3e50; }
  .empty { color:#888; font-size:12.5px; }
  .tag { border:1px solid #eee; border-left-width:4px; border-radius:6px; padding:8px 10px; margin:8px 0; border-left-color:#7d8fa3; }
  .tag.first_crack { border-left-color:#c0392b; }
  .tag.drop { border-left-color:#8e44ad; }
  .tag.yellow { border-left-color:#d4ac0d; }
  .tag.turnaround { border-left-color:#2471a3; }
  .tag-head { display:flex; align-items:center; gap:8px; }
  .kind { font-size:11px; background:#eef2f5; border-radius:8px; padding:1px 8px; color:#34495e; }
  .labeltext { font-weight:600; font-size:13px; flex:1; }
  .del { color:#c0392b; }
  .desc-text { font-size:12px; color:#555; margin:5px 0; }
  .times { font-size:11.5px; display:flex; gap:12px; flex-wrap:wrap; margin:4px 0; }
  .changed { color:#c0392b; font-weight:700; }
  .old { color:#888; text-decoration:line-through; }
  .new { color:#c0392b; }
  .cur { color:#1e7d46; }
  .meta { font-size:11px; color:#999; }
  .err { color:#c0392b; font-size:12.5px; }
</style>
