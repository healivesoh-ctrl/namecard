/* 가게 관리자 · 다이음 관리자 콘솔 공통 스크립트.
   페이지에서 window.ADMIN_ROLE = "store" | "operator" 를 먼저 정의한다. */

const ROLE = window.ADMIN_ROLE;
const IS_STORE = ROLE === 'store';
const KEY = 'admin-token:' + ROLE;
let TOKEN = localStorage.getItem(KEY) || '';
let FILTER = '';
let CATALOG = null;

const $ = (id) => document.getElementById(id);
const show = (id, on = true) => $(id).classList.toggle('hidden', !on);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const when = (t) => t ? new Date(t * 1000).toLocaleString('ko-KR') : '-';

function msg(text, kind = 'err') {
  const el = $('msg');
  el.className = 'msg ' + kind;
  el.textContent = text;
  show('msg', !!text);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json',
               ...(TOKEN ? { 'X-Admin-Token': TOKEN } : {}), ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && TOKEN) { logout(); throw new Error('로그인이 만료되었습니다. 다시 로그인해 주세요.'); }
  if (!res.ok) throw new Error(data.detail || `요청 실패 (${res.status})`);
  return data;
}

function logout() {
  TOKEN = '';
  localStorage.removeItem(KEY);
  show('login', true); show('console', false);
}

async function initLogin() {
  if (!IS_STORE) return;
  CATALOG = await api('/api/catalog');
  $('partner').innerHTML = CATALOG.partners
    .map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
}

async function doLogin() {
  msg('');
  try {
    const body = { role: ROLE, password: $('password').value,
                   partner_id: IS_STORE ? $('partner').value : null };
    const res = await api('/api/admin/login', { method: 'POST', body: JSON.stringify(body) });
    TOKEN = res.token;
    localStorage.setItem(KEY, TOKEN);
    $('who').textContent = res.name;
    show('login', false); show('console', true);
    await load();
  } catch (e) { msg(e.message); }
}

const STATUS_TABS = [
  ['', '전체'], ['pending', '승인 대기'], ['approved', '인증 완료'],
  ['fulfilled', '전달 완료'], ['phone_pending', '본인확인 대기'], ['rejected', '반려'],
];

function renderTabs(counts) {
  $('tabs').innerHTML = STATUS_TABS.map(([v, label]) => {
    const n = v ? (counts[v] || 0) : Object.values(counts).reduce((a, b) => a + b, 0);
    return `<button data-status="${v}" class="${FILTER === v ? 'on' : ''}">${label} ${n}</button>`;
  }).join('');
  $('tabs').querySelectorAll('button').forEach(b => {
    b.onclick = () => { FILTER = b.dataset.status; load(); };
  });
}

function approvalLine(claim, role, label) {
  const ap = claim.approvals?.[role];
  if (!ap) return `<div class="approval">${esc(label)}: 대기 중</div>`;
  if (!ap.valid) return `<div class="approval bad">${esc(label)}: <b>승인 무효</b> — 접수 내용이 변경되어 재확인 필요</div>`;
  return `<div class="approval">${esc(label)}: <b>승인</b> ${esc(ap.admin)} · ${when(ap.at)}${ap.note ? ' · ' + esc(ap.note) : ''}</div>`;
}

function claimCard(c) {
  const roleLabel = { store: c.partner_name, operator: CATALOG?.operator?.name || '다이음' };
  const label = (k) => c.labels?.[k] || k;
  const identityRows = IS_STORE
    ? Object.entries(c.identity || {}).map(([k, v]) =>
        `<div class="kv"><span>${esc(label(k))}</span><span>${esc(v)}</span></div>`).join('')
    : '';
  const canAct = ['pending'].includes(c.status) && !c.approvals?.[ROLE]?.valid;
  const canFulfill = !IS_STORE && c.status === 'approved';
  return `<div class="claim" data-id="${esc(c.id)}">
    <h3>${esc(c.applicant_name)} <span class="pill ${esc(c.status)}">${esc(c.status_label)}</span></h3>
    <div class="cid">${esc(c.id)} · ${esc(c.partner_name)} · 지문 ${esc(c.fingerprint)}</div>
    <div style="margin-top:10px">
      <div class="kv"><span>서비스</span><span>${esc(c.service_name)}</span></div>
      <div class="kv"><span>제휴 상품</span><span>${esc(c.product_name)}</span></div>
      <div class="kv"><span>연락처</span><span>${esc(c.phone_masked)}</span></div>
      <div class="kv"><span>접수일</span><span>${when(c.created_at)}</span></div>
      ${Object.entries(c.booking || {}).map(([k, v]) =>
        `<div class="kv"><span>${esc(label(k))}</span><span>${esc(v)}</span></div>`).join('')}
      ${identityRows ? `<div class="hint" style="margin-top:8px">본인확인 입력값</div>${identityRows}` : ''}
    </div>
    <div style="margin-top:10px">
      ${(c.required_roles || []).map(r => approvalLine(c, r, roleLabel[r] || r)).join('')}
      ${c.rejection?.reason ? `<div class="approval bad">반려: ${esc(c.rejection.reason)} (${esc(c.rejection.admin)})</div>` : ''}
      ${c.fulfillment?.at ? `<div class="approval">전달 완료: ${esc(c.fulfillment.product_name)} · ${esc(c.fulfillment.by)} · ${when(c.fulfillment.at)} · 확인번호 ${esc(c.fulfillment.receipt)}</div>` : ''}
    </div>
    ${canAct ? `<div class="row" style="margin-top:12px">
        <button class="primary act-approve">${IS_STORE ? '우리 고객 맞음 — 승인' : '배정 확인 — 승인'}</button>
        <button class="danger act-reject">반려</button>
      </div>` : ''}
    ${canFulfill ? `<div class="row" style="margin-top:12px">
        <button class="primary act-fulfill">수령코드 확인 후 전달 확정</button>
      </div>` : ''}
  </div>`;
}

async function load() {
  // 목록 새로고침이 직전 승인/반려 결과 메시지를 지우지 않도록 msg 는 건드리지 않는다.
  try {
    if (!CATALOG) CATALOG = await api('/api/catalog');
    const data = await api('/api/admin/claims');
    renderTabs(data.counts);
    const list = FILTER ? data.claims.filter(c => c.status === FILTER) : data.claims;
    $('claims').innerHTML = list.length
      ? list.map(claimCard).join('')
      : '<div class="panel" style="grid-column:1/-1">해당하는 접수 건이 없습니다.</div>';
    $('who').textContent = data.admin;
    bindActions();
  } catch (e) { msg(e.message); }
}

function bindActions() {
  document.querySelectorAll('.claim').forEach(card => {
    const id = card.dataset.id;
    card.querySelector('.act-approve')?.addEventListener('click', () => approve(id));
    card.querySelector('.act-reject')?.addEventListener('click', () => reject(id));
    card.querySelector('.act-fulfill')?.addEventListener('click', () => openFulfill(id));
  });
}

async function approve(id) {
  msg('');
  try {
    if (IS_STORE) return openVerify(id);
    const res = await api(`/api/admin/claims/${id}/approve`, {
      method: 'POST', body: JSON.stringify({ note: '' }) });
    msg(res.message, 'ok');
    load();
  } catch (e) { msg(e.message); }
}

/* 가게 관리자: 본인확인 항목을 하나씩 대조한 뒤에만 승인 */
async function openVerify(id) {
  const detail = await api(`/api/admin/claims/${id}`);
  $('verify-title').textContent = `${detail.applicant_name} 님이 ${detail.partner_name} 이용 고객이 맞습니까?`;
  $('verify-body').innerHTML = (detail.identity_fields || []).map(f => `
    <label class="check"><input type="checkbox" value="${esc(f.key)}" ${f.required ? '' : 'checked'}>
      <span><b>${esc(f.verify_label || f.label)}</b><br>
      <span class="hint">신청자 입력: ${esc(detail.identity?.[f.key] ?? '(미입력)')}</span></span></label>`).join('');
  $('verify-note').value = '';
  $('verify').dataset.id = id;
  $('verify').showModal();
}

async function confirmVerify() {
  const id = $('verify').dataset.id;
  const checks = [...$('verify-body').querySelectorAll('input:checked')].map(i => i.value);
  try {
    const res = await api(`/api/admin/claims/${id}/approve`, {
      method: 'POST', body: JSON.stringify({ checks, note: $('verify-note').value.trim() }) });
    $('verify').close();
    msg(res.message, 'ok');
    load();
  } catch (e) { msg(e.message); }
}

async function reject(id) {
  const reason = prompt('반려 사유를 입력해 주세요. (신청자에게 표시됩니다)');
  if (!reason) return;
  try {
    await api(`/api/admin/claims/${id}/reject`, { method: 'POST', body: JSON.stringify({ reason }) });
    msg('반려 처리했습니다.', 'ok');
    load();
  } catch (e) { msg(e.message); }
}

function openFulfill(id) {
  $('fulfill').dataset.id = id;
  $('f-code').value = ''; $('f-last4').value = ''; $('f-note').value = '';
  $('fulfill').showModal();
}

async function confirmFulfill() {
  const id = $('fulfill').dataset.id;
  try {
    const res = await api(`/api/admin/claims/${id}/fulfill`, {
      method: 'POST',
      body: JSON.stringify({ code: $('f-code').value, phone_last4: $('f-last4').value,
                             note: $('f-note').value.trim() }) });
    $('fulfill').close();
    msg(`${res.message} 확인번호 ${res.receipt}`, 'ok');
    load();
  } catch (e) { msg(e.message); }
}

window.addEventListener('DOMContentLoaded', async () => {
  $('login-btn').onclick = doLogin;
  $('password').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  $('logout').onclick = logout;
  $('reload').onclick = () => { msg(''); load(); };
  $('verify-ok')?.addEventListener('click', confirmVerify);
  $('verify-cancel')?.addEventListener('click', () => $('verify').close());
  $('fulfill-ok')?.addEventListener('click', confirmFulfill);
  $('fulfill-cancel')?.addEventListener('click', () => $('fulfill').close());
  $('reload-config')?.addEventListener('click', async () => {
    try { const r = await api('/api/admin/reload', { method: 'POST' }); CATALOG = null; msg(r.message, 'ok'); load(); }
    catch (e) { msg(e.message); }
  });
  try { await initLogin(); } catch (e) { msg(e.message); }
  if (TOKEN) { show('login', false); show('console', true); load(); }
});
