// 前端只做请求与展示；温升率/插值/指标均由后端计算，前端不自行从原始点估算。
const j = async (r) => {
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status} ${t}`);
  }
  return r.json();
};

const qs = (params) => {
  const u = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') u.set(k, v);
  });
  const s = u.toString();
  return s ? `?${s}` : '';
};

export const api = {
  batches: () => fetch('/api/batches').then(j),
  detail: (id, p) => fetch(`/api/batches/${id}${qs(p)}`).then(j),
  compare: (a, b, p) => fetch(`/api/compare${qs({ a, b, ...p })}`).then(j),
  revisions: (id) => fetch(`/api/batches/${id}/events/revisions`).then(j),
  revise: (eventId, body) =>
    fetch(`/api/events/${eventId}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    }).then(j),
  addEvent: (bid, body) =>
    fetch(`/api/events?bid=${bid}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    }).then(j),
  candidates: (bid) => fetch(`/api/batches/${bid}/candidates`).then(j),
  proposeCandidate: (bid, body) =>
    fetch(`/api/batches/${bid}/candidates`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    }).then(j),
  decideCandidate: (cid, accept, by = 'operator') =>
    fetch(`/api/candidates/${cid}/decision`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ accept, decided_by: by })
    }).then(j),
  createReport: (body) =>
    fetch('/api/reports', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    }).then(j),
  reports: () => fetch('/api/reports').then(j),
  report: (id) => fetch(`/api/reports/${id}`).then(j),
  exportUrl: (id, p) => `/api/batches/${id}/export${qs(p)}`,
  reseed: () => fetch('/api/admin/reseed?seed=7', { method: 'POST' }).then(j)
};
