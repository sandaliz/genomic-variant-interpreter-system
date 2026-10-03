const EXAMPLES = [
  { label: 'Gene + variant',  q: "Is BRCA1 NC_000017.10:g.41244442A>G pathogenic?" },
  { label: 'Short form',      q: "Classify BRCA2 c.5946delT" },
  { label: 'By rsID',         q: "What do we know about rs80356943?" },
  { label: 'Whole gene',      q: "Tell me about variants in BRCA1" }
];

const exWrap = document.getElementById('examples');
EXAMPLES.forEach(({ q }) => {
  const b = document.createElement('button');
  b.className = 'ex-btn';
  b.textContent = q;
  b.onclick = () => { document.getElementById('query').value = q; classifyVariant(); };
  exWrap.appendChild(b);
});

// The same examples on the landing page carry their label, and route through
// sign-in first: clicking one while signed out queues the query rather than
// dropping the visitor on a page they cannot use.
const landingWrap = document.getElementById('landingExamples');
EXAMPLES.forEach(({ label, q }) => {
  const b = document.createElement('button');
  b.className = 'example-btn';
  b.innerHTML = '<span class="ex-label">' + escapeHtml(label) + '</span>' + escapeHtml(q);
  b.onclick = () => {
    if (getToken()){
      document.getElementById('query').value = q;
      showView('workspace');
      classifyVariant();
    } else {
      pendingQuery = q;
      openAuth('signin');
    }
  };
  landingWrap.appendChild(b);
});

let latestResult = null;

// ---- Session (JWT bearer auth) -------------------------------------
// The token is held in localStorage and sent as Authorization: Bearer.
// ---- Views ---------------------------------------------------------------
// Four screens, one visible at a time: landing, workspace, profile, admin.
// Keeping the query bar off the landing page is the point -- a visitor should
// see what the tool is before being asked to sign in.
const VIEWS = ['landing', 'workspace', 'profile', 'admin'];
let currentView = 'landing';
let currentUser = null;
let pendingQuery = null;   // a landing example waiting for sign-in

function showView(name){
  if (name === 'workspace' && !getToken()){
    // Never show the query surface to someone who cannot use it yet.
    pendingQuery = document.getElementById('query').value.trim() || null;
    openAuth('signin');
    return;
  }
  if (name === 'admin' && !(currentUser && currentUser.user.role === 'admin')){
    // UI convenience only -- the endpoints enforce require_admin regardless.
    if (getToken()){
      adminMessage('Admin role required. This account cannot reach the dashboard.', true);
      showView('workspace');
      return;
    }
  }
  currentView = name;
  VIEWS.forEach(v => {
    const el = document.getElementById('view' + v.charAt(0).toUpperCase() + v.slice(1));
    if (el) el.classList.toggle('hidden', v !== name);
  });
  window.scrollTo({ top: 0, behavior: 'auto' });
  if (name === 'profile') loadProfile();
  if (name === 'admin') loadAdminPage();
}

function goHome(){
  showView(getToken() ? 'workspace' : 'landing');
}

function toggleProfileMenu(event){
  if (event) event.stopPropagation();
  const menu = document.getElementById('profileMenu');
  const open = menu.classList.toggle('hidden');
  document.getElementById('avatarBtn').setAttribute('aria-expanded', String(!open));
}

document.addEventListener('click', () => {
  const menu = document.getElementById('profileMenu');
  if (menu && !menu.classList.contains('hidden')){
    menu.classList.add('hidden');
    document.getElementById('avatarBtn').setAttribute('aria-expanded', 'false');
  }
});
// It is a short-lived access token, not a credential store: the server
// re-reads the account from PostgreSQL on every request, so signing out
// or deactivating an account takes effect immediately.
const TOKEN_KEY = 'variantlens_token';

function getToken(){ return localStorage.getItem(TOKEN_KEY); }
function setToken(token){
  if (token) { localStorage.setItem(TOKEN_KEY, token); }
  else { localStorage.removeItem(TOKEN_KEY); }
}

function authHeaders(extra){
  return Object.assign({}, extra || {}, (getToken() ? {'Authorization': 'Bearer ' + getToken()} : {}));
}

async function authFetch(path, options){
  const opts = Object.assign({}, options || {}, { headers: authHeaders((options || {}).headers) });
  const res = await fetch(path, opts);
  if (res.status === 401 && getToken()){
    // Expired, revoked or deactivated session: drop it and ask again.
    setToken(null);
    renderSession(null);
  }
  return res;
}

const AUTH_PANES = {
  signin:   'paneSignin',
  register: 'paneRegister',
  forgot:   'paneForgot',
  reset:    'paneReset',
};

// The auth UI is a modal over whatever view you were on, so opening it never
// loses your place.
function openAuth(mode){
  const pane = AUTH_PANES[mode] || AUTH_PANES.signin;
  document.querySelectorAll('.auth-pane').forEach(p => p.classList.add('hidden'));
  document.getElementById(pane).classList.remove('hidden');
  document.getElementById('authModal').classList.remove('hidden');
  const first = document.querySelector('#' + pane + ' input');
  if (first) setTimeout(() => first.focus(), 40);
}

function closeAuth(){
  document.getElementById('authModal').classList.add('hidden');
  ['loginError','registerError','forgotError','resetError'].forEach(id => {
    document.getElementById(id).textContent = '';
  });
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeAuth();
});

document.getElementById('authModal').addEventListener('click', e => {
  if (e.target.id === 'authModal') closeAuth();
});

function renderSession(me){
  const signedOut = document.getElementById('headerSignedOut');
  const signedIn = document.getElementById('headerSignedIn');
  if (!me){
    signedOut.classList.remove('hidden');
    signedIn.classList.add('hidden');
    document.getElementById('submitBtn').disabled = true;
    return;
  }
  signedOut.classList.add('hidden');
  signedIn.classList.remove('hidden');
  document.getElementById('submitBtn').disabled = false;

  currentUser = me;
  const isAdmin = me.user.role === 'admin';
  document.getElementById('adminNavBtn').classList.toggle('hidden', !isAdmin);
  document.getElementById('pmAdminItem').classList.toggle('hidden', !isAdmin);

  const displayName = me.user.full_name || me.user.username;
  document.getElementById('pmName').textContent = displayName;
  document.getElementById('pmMail').textContent = me.user.email;
  document.getElementById('pmPlan').textContent = me.plan.name;
  document.getElementById('avatarBtn').textContent =
    (me.user.full_name || me.user.username).trim().charAt(0).toUpperCase() || '?';

  const used = me.usage.used_this_month;
  const limit = me.usage.monthly_limit;
  document.getElementById('pmQuota').textContent =
    (limit === null || limit === undefined)
      ? used + ' queries this month · unlimited'
      : used + ' / ' + limit + ' queries used this month';
  const fill = document.getElementById('pmQuotaFill');
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 100;
  fill.style.width = pct + '%';
  // Warn in the last 20% so a clinician sees the ceiling before hitting it.
  fill.className = 'quota-fill' + (limit && pct >= 80 ? ' warn' : '');
}

async function refreshSession(){
  if (!getToken()){ renderSession(null); showView('landing'); return; }
  try {
    const res = await fetch('/api/auth/me', { headers: authHeaders() });
    if (!res.ok){ setToken(null); renderSession(null); showView('landing'); return; }
    renderSession(await res.json());
    // Landing is what a signed-out visitor sees; once signed in we go to the
    // workspace unless a landing example already queued a query.
    if (pendingQuery){
      const q = pendingQuery; pendingQuery = null;
      document.getElementById('query').value = q;
      showView('workspace');
    } else if (currentView === 'landing'){
      showView('workspace');
    }
  } catch (err){
    setToken(null);
    renderSession(null);
    showView('landing');
  }
}

function describeAuthError(data, fallback){
  if (!data) return fallback;
  if (typeof data.detail === 'string') return data.detail;
  // FastAPI validation errors arrive as a list of field problems.
  if (Array.isArray(data.detail) && data.detail.length){
    return data.detail.map(d => (d.loc || []).slice(1).join('.') + ': ' + d.msg).join(' ');
  }
  return fallback;
}

async function doLogin(event){
  event.preventDefault();
  const btn = document.getElementById('loginBtn');
  const err = document.getElementById('loginError');
  err.textContent = '';
  btn.disabled = true;
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('loginUsername').value.trim(),
        password: document.getElementById('loginPassword').value
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(data, 'Sign in failed.'));
    setToken(data.access_token);
    document.getElementById('loginPassword').value = '';
    await refreshSession();
  } catch (e){
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
  return false;
}

async function doRegister(event){
  event.preventDefault();
  const btn = document.getElementById('registerBtn');
  const err = document.getElementById('registerError');
  err.textContent = '';
  btn.disabled = true;
  try {
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('regUsername').value.trim(),
        email: document.getElementById('regEmail').value.trim(),
        password: document.getElementById('regPassword').value
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(data, 'Registration failed.'));
    // Register returns the account but no token, so sign in immediately.
    const login = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('regUsername').value.trim(),
        password: document.getElementById('regPassword').value
      })
    });
    const session = await login.json().catch(() => ({}));
    if (!login.ok) throw new Error(describeAuthError(session, 'Registration failed.'));
    setToken(session.access_token);
    document.getElementById('regPassword').value = '';
    await refreshSession();
  } catch (e){
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
  return false;
}

async function doForgotPassword(event){
  event.preventDefault();
  const btn = document.getElementById('forgotBtn');
  const err = document.getElementById('forgotError');
  err.textContent = '';
  btn.disabled = true;
  const email = document.getElementById('forgotEmail').value.trim();
  try {
    const res = await fetch('/api/auth/password-reset/request', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ email })
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(body, 'Could not send a code.'));
    openAuth('reset');
    // In the log transport the code comes back so the flow is usable locally.
    // With a real mail transport this is null and the user checks their inbox.
    const devBox = document.getElementById('resetDevCode');
    if (body.dev_code){
      devBox.classList.remove('hidden');
      devBox.textContent = 'Development mode — your code is ' + body.dev_code;
      document.getElementById('resetCode').value = body.dev_code;
    } else {
      devBox.classList.add('hidden');
      document.getElementById('resetCode').value = '';
    }
  } catch (e){
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
  return false;
}

async function doConfirmReset(event){
  event.preventDefault();
  const btn = document.getElementById('resetBtn');
  const err = document.getElementById('resetError');
  err.textContent = '';
  btn.disabled = true;
  try {
    const res = await fetch('/api/auth/password-reset/confirm', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: document.getElementById('forgotEmail').value.trim(),
        code: document.getElementById('resetCode').value.trim(),
        new_password: document.getElementById('resetPassword').value
      })
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(body, 'Could not reset the password.'));
    // The reset endpoint mints a session, so we go straight in.
    setToken(body.access_token);
    closeAuth();
    renderSession(body);
    showView('workspace');
  } catch (e){
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
  return false;
}

function logout(){
  setToken(null);
  currentUser = null;
  renderSession(null);
  closeAuth();
  latestResult = null;
  document.getElementById('results').style.display = 'none';
  document.getElementById('empty').style.display = 'block';
  showView('landing');
}

// ---- Admin: shared state + account controls ------------------------
// Rendered on the dedicated admin page. Every call below is authorised
// server-side by require_admin(), so hiding anything here is a UI affordance,
// not a control.
let adminPlans = [];

function renderPlanCards(plans){
  const el = document.getElementById('adminPlanCards');
  el.innerHTML = plans.map(p => {
    const price = p.price_cents === null || p.price_cents === undefined
      ? 'Custom'
      : '$' + (p.price_cents / 100).toFixed(2);
    const limit = p.is_unlimited ? 'Unlimited' : (p.monthly_query_limit + '/mo');
    return '<div class="plan-card">'
      + '<div class="pc-name">' + escapeHtml(p.name) + '</div>'
      + '<div class="pc-meta">' + escapeHtml(price) + ' · ' + escapeHtml(limit) + '</div>'
      + '<div class="pc-meta">' + p.subscribers + ' account' + (p.subscribers === 1 ? '' : 's') + '</div>'
      + '</div>';
  }).join('');
}

function planOptions(selectedId){
  return adminPlans.map(p =>
    '<option value="' + p.id + '"' + (p.id === selectedId ? ' selected' : '') + '>'
    + escapeHtml(p.name) + '</option>').join('');
}

function renderAdminUsers(users){
  const body = document.getElementById('adminUserBody');
  body.innerHTML = '';
  users.forEach(u => {
    const tr = document.createElement('tr');
    if (!u.is_active){ tr.className = 'inactive'; }
    tr.innerHTML =
      '<td><div>' + escapeHtml(u.username) + '</div>'
      + '<div style="color:var(--ink-faint);font-size:11px;">' + escapeHtml(u.email) + '</div>'
      + '<div style="color:var(--ink-faint);font-size:10.5px;">' + escapeHtml(u.role) + '</div></td>'
      + '<td><select class="admin-select" onchange="adminChangePlan(' + u.id + ', this.value)">'
      + planOptions(u.plan_id) + '</select></td>'
      + '<td>' + u.used_this_month + '</td>'
      + '<td><button class="admin-btn" onclick="adminToggleActive(' + u.id + ', '
      + u.is_active + ')">' + (u.is_active ? 'Deactivate' : 'Activate') + '</button></td>';
    body.appendChild(tr);
  });
}

async function adminChangePlan(userId, planId){
  adminMessage('Saving…');
  const res = await authFetch('/api/admin/users/' + userId, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ plan_id: Number(planId) })
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok){
    adminMessage(describeAuthError(body, 'Could not change the plan.'), true);
    await loadAdminPanel();  // snap the select back to the stored value
    return;
  }
  adminMessage('Plan updated.');
  await loadAdminPanel();
}

async function adminToggleActive(userId, currentlyActive){
  const next = !currentlyActive;
  adminMessage(next ? 'Activating…' : 'Deactivating…');
  const res = await authFetch('/api/admin/users/' + userId, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ is_active: next })
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok){
    // 409 here means the guard refused to remove the last active admin.
    adminMessage(describeAuthError(body, 'Could not change the account state.'), true);
    await loadAdminPanel();
    return;
  }
  adminMessage(next ? 'Account activated.' : 'Account deactivated.');
  await loadAdminPanel();
}

// ---- Profile page ----------------------------------------------------

function setMsg(id, text, kind){
  const el = document.getElementById(id);
  el.textContent = text || '';
  el.className = 'form-msg' + (kind ? ' ' + kind : '');
}

async function loadProfile(){
  if (!getToken()){ openAuth('signin'); return; }
  const res = await authFetch('/api/auth/me');
  if (!res.ok){ openAuth('signin'); return; }
  const me = await res.json();
  currentUser = me;
  await loadMyUpgradeRequests();
  document.getElementById('pfUsername').value = me.user.username;
  document.getElementById('pfFullName').value = me.user.full_name || '';
  document.getElementById('pfEmail').value = me.user.email;
  document.getElementById('pfCurrentPw').value = '';
  renderPlanBox(me);
}

function renderPlanBox(me){
  const price = p => p.price_cents === null || p.price_cents === undefined
    ? 'Custom pricing'
    : '$' + (p.price_cents / 100).toFixed(2) + ' / month';
  const limit = p => p.is_unlimited ? 'Unlimited queries'
    : (p.monthly_query_limit + ' queries / month');
  const box = document.getElementById('pfPlanBox');

  const openRequest = (me.upgrades || []).find(
    r => r.status === 'pending' || r.status === 'contacted');
  if (openRequest){
    box.innerHTML = '<div class="queue"><div class="queue-head">'
      + '<div><div class="queue-who">Upgrade to ' + escapeHtml(openRequest.target_plan) + '</div>'
      + '<div class="queue-mail">Status: ' + escapeHtml(openRequest.status) + '</div></div>'
      + '<span class="status-pill ' + escapeHtml(openRequest.status) + '">'
      + escapeHtml(openRequest.status) + '</span></div>'
      + (openRequest.admin_note
         ? '<div class="queue-admin-note">' + escapeHtml(openRequest.admin_note) + '</div>' : '')
      + '<p class="field-hint">An administrator reviews every request. Your current '
      + 'plan stays active meanwhile.</p></div>';
  } else {
    box.innerHTML = '';
  }

  const cards = adminPlans.map(p => {
    const current = p.id === me.plan.id;
    return '<div class="plan-option' + (current ? ' current' : '') + '">'
      + '<h4>' + escapeHtml(p.name)
      + (current ? ' <span class="plan-badge">current</span>' : '') + '</h4>'
      + '<div class="plan-price">' + escapeHtml(price(p)) + ' · '
      + escapeHtml(limit(p)) + '</div>'
      + '<ul class="plan-feats"><li>'
      + (p.llm_enabled ? 'LLM-assisted reasoning' : 'Rule-based reasoning')
      + '</li><li>API access' + (p.api_access ? '' : ' (not included)')
      + '</li><li>Audit export' + (p.audit_export ? '' : ' (not included)')
      + '</li></ul>'
      + (current ? ''
         : '<button class="btn-primary" style="width:100%;" onclick="requestUpgrade('
           + p.id + ')">Request upgrade</button>')
      + '</div>';
  }).join('');

  box.innerHTML += '<div class="plan-compare">' + cards + '</div>'
    + '<p class="field-hint">Upgrade requests go to an administrator, who approves '
    + 'or declines them. No payment is taken here.</p>';
}
// ---- Admin page ------------------------------------------------------

async function loadAdminPage(){
  const [analyticsRes, queueRes, usersRes, plansRes] = await Promise.all([
    authFetch('/api/admin/analytics'),
    authFetch('/api/admin/upgrade-requests'),
    authFetch('/api/admin/users?limit=100'),
    authFetch('/api/admin/plans')
  ]);
  // A clinician reaching this view is expected, not an error to shout about.
  if (analyticsRes.status === 403 || queueRes.status === 403){
    adminMessage('Admin role required. This account cannot reach the dashboard.', true);
    return;
  }
  if (!analyticsRes.ok || !queueRes.ok || !usersRes.ok || !plansRes.ok) return;

  const analytics = await analyticsRes.json();
  renderStats(analytics);
  renderChart(analytics.daily_usage);
  renderQueue(await queueRes.json());
  adminPlans = (await plansRes.json()).plans;
  renderPlanCards(adminPlans);
  renderAdminUsers((await usersRes.json()).users);
  adminMessage('');
}

function adminMessage(text, isError){
  const el = document.getElementById('adminPageMsg');
  el.textContent = text || '';
  el.className = 'admin-msg' + (isError ? ' err' : '');
}

function renderStats(a){
  const cards = [
    { n: a.accounts.total, label: 'Accounts' },
    { n: a.accounts.active, label: 'Active' },
    { n: a.accounts.new_this_month, label: 'New this month' },
    { n: a.usage.queries_this_month, label: 'Queries this month' },
    { n: a.usage.queries_today, label: 'Queries today' },
    { n: a.upgrades.open || 0, label: 'Open upgrades', alert: (a.upgrades.open || 0) > 0 },
    { n: a.signoffs.total, label: 'Sign-offs' }
  ];
  document.getElementById('statRow').innerHTML = cards.map(c =>
    '<div class="stat-card' + (c.alert ? ' alert' : '') + '">'
    + '<div class="stat-n">' + c.n + '</div>'
    + '<div class="stat-label">' + c.label + '</div></div>').join('');
}

function renderChart(series){
  const el = document.getElementById('usageChart');
  const peak = Math.max(...series.map(d => d.queries), 0);
  if (!peak){
    el.innerHTML = '<div class="chart-empty">No queries recorded in this window yet.</div>';
    return;
  }
  // Gap-filled bars, so a quiet day reads as a real dip rather than vanishing.
  el.innerHTML = series.map(d =>
    '<div class="chart-col' + (d.queries ? '' : ' zero') + '" style="height:'
    + Math.max(2, Math.round((d.queries / peak) * 118)) + 'px" title="'
    + d.date + ': ' + d.queries + ' queries"></div>').join('')
    + '<div class="chart-axis"><span>' + series[0].date + '</span><span>'
    + series[series.length - 1].date + '</span></div>';
}


function renderQueue(body){
  const el = document.getElementById('upgradeQueue');
  if (!body.requests.length){
    el.innerHTML = '<div class="queue-empty">No upgrade requests.</div>';
    return;
  }
  el.innerHTML = body.requests.map(r => {
    const open = r.status === 'pending' || r.status === 'contacted';
    const actions = open
      ? '<div class="queue-actions">'
        + '<button class="admin-btn" onclick="decideUpgrade(' + r.id
        + ',\'contacted\')">Mark contacted</button>'
        + '<button class="admin-btn" onclick="decideUpgrade(' + r.id
        + ',\'approved\')">Approve</button>'
        + '<button class="admin-btn" onclick="decideUpgrade(' + r.id
        + ',\'declined\')">Decline</button></div>'
      : '';
    return '<div class="queue"><div class="queue-head"><div>'
      + '<div class="queue-who">' + escapeHtml(r.username) + ' &rarr; '
      + escapeHtml(r.target_plan) + '</div>'
      + '<div class="queue-mail">' + escapeHtml(r.email) + ' · currently '
      + escapeHtml(r.current_plan) + '</div></div>'
      + '<span class="status-pill ' + escapeHtml(r.status) + '">'
      + escapeHtml(r.status) + '</span></div>'
      + (r.note ? '<div class="queue-note">' + escapeHtml(r.note) + '</div>' : '')
      + (r.admin_note ? '<div class="queue-admin-note">'
                       + escapeHtml(r.admin_note) + '</div>' : '')
      + actions + '</div>';
  }).join('');
}

async function decideUpgrade(id, status){
  const note = window.prompt('Optional note for the user:', '');
  if (note === null) return;
  const res = await authFetch('/api/admin/upgrade-requests/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ status, admin_note: note || null })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    // 409 means the request was already decided.
    adminMessage(describeAuthError(data, 'Could not record that decision.'), true);
    return;
  }
  await loadAdminPage();
}


async function saveProfile(){
  setMsg('pfMsg', 'Saving…');
  const body = { full_name: document.getElementById('pfFullName').value };
  const email = document.getElementById('pfEmail').value.trim();
  const currentPw = document.getElementById('pfCurrentPw').value;
  // The API demands the current password for an email change; sending it only
  // when the address actually changed keeps a cosmetic save frictionless.
  if (email && email !== (currentUser && currentUser.user.email)){
    body.email = email;
    if (currentPw) body.current_password = currentPw;
  }
  try {
    const res = await authFetch('/api/auth/me', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(data, 'Could not save your details.'));
    renderSession(data);
    document.getElementById('pfCurrentPw').value = '';
    setMsg('pfMsg', 'Saved.', 'ok');
    renderPlanBox(data);
  } catch (e){
    setMsg('pfMsg', e.message, 'err');
  }
}

async function changeOwnPassword(){
  setMsg('pwMsg', 'Changing…');
  try {
    const res = await authFetch('/api/auth/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        current_password: document.getElementById('pfOldPw').value,
        new_password: document.getElementById('pfNewPw').value
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(data, 'Could not change your password.'));
    document.getElementById('pfOldPw').value = '';
    document.getElementById('pfNewPw').value = '';
    setMsg('pwMsg', 'Password changed.', 'ok');
  } catch (e){
    setMsg('pwMsg', e.message, 'err');
  }
}

async function loadMyUpgradeRequests(){
  if (!currentUser) return;
  const res = await authFetch('/api/upgrade-requests/mine');
  if (!res.ok) return;
  currentUser.upgrades = (await res.json()).requests;
}

async function requestUpgrade(planId){
  const plan = adminPlans.find(p => p.id === planId);
  const note = window.prompt(
    'Tell the administrator why you need ' + (plan ? plan.name : 'this plan') +
    ' (optional):', '');
  if (note === null) return;
  const res = await authFetch('/api/upgrade-requests', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ target_plan_id: planId, note: note || null })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    alert(describeAuthError(data, 'Could not submit the request.'));
    return;
  }
  await loadMyUpgradeRequests();
  await loadProfile();
  await refreshSession();
}

function escapeHtml(s){
  if (s === null || s === undefined) return '—';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtNum(v, digits){
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return digits !== undefined ? n.toFixed(digits) : String(n);
}

function classColors(cls){
  const c = (cls || '').toLowerCase();
  if (c.includes('pathogenic')) return {bar:'var(--pathogenic)', fill:'var(--pathogenic)', pill:'pill-path'};
  if (c.includes('benign')) return {bar:'var(--benign)', fill:'var(--benign)', pill:'pill-benign'};
  return {bar:'var(--vus)', fill:'var(--vus)', pill:'pill-vus'};
}

function updateSignoffStatus(signoff){
  const el = document.getElementById('signoffStatus');
  if (signoff && signoff.reviewer_name && signoff.review_date){
    el.textContent = 'Reviewed ' + signoff.review_date + ' — ' + signoff.reviewer_name;
    el.className = 'signoff-status reviewed';
  } else {
    el.textContent = 'Pending counselor review';
    el.className = 'signoff-status pending';
  }
}

function loadReviewIntoForm(signoff){
  document.getElementById('reviewerName').value = signoff?.reviewer_name || '';
  document.getElementById('reviewDate').value = signoff?.review_date || '';
  document.getElementById('reviewSignature').value = signoff?.signature || '';
  updateSignoffStatus(signoff);
}

async function saveReview(){
  if (!latestResult?.variant_id){ return; }
  const reviewer = document.getElementById('reviewerName').value.trim();
  const reviewDate = document.getElementById('reviewDate').value;
  const signature = document.getElementById('reviewSignature').value.trim();
  if (!reviewer || !reviewDate){
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = 'Enter a reviewer name and review date to save sign-off.';
    return;
  }
  try {
    // Sign-offs are stored in PostgreSQL behind the account layer, so this call
    // needs the bearer token just like /api/classify.
    const res = await authFetch(`/api/review/${encodeURIComponent(latestResult.variant_id)}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ reviewer_name: reviewer, review_date: reviewDate, signature: signature || null })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not save review sign-off.');
    latestResult.review_signoff = data.review_signoff;
    updateSignoffStatus(data.review_signoff);
  } catch (err) {
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = err.message;
  }
}

async function downloadPDF(){
  if (!latestResult) return;
  const btn = document.getElementById('pdfBtn');
  btn.disabled = true; btn.textContent = 'Generating…';
  try {
    const res = await authFetch('/api/report/pdf', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        variant_id: latestResult.variant_id || 'unknown',
        classification: latestResult.classification || 'Unknown',
        confidence: latestResult.confidence || 'none',
        confidence_numeric: Number(latestResult.confidence_numeric || 0),
        reasoning_mode: latestResult.reasoning_mode || 'rule_based',
        consensus: latestResult.consensus || null,
        report_text: latestResult.report_text || '',
        evidence_summary: latestResult.evidence_summary || '',
        criteria_applied: latestResult.criteria_applied || [],
        acmg_criteria_applied: latestResult.acmg_criteria_applied || latestResult.criteria_applied || [],
        computational_annotations: latestResult.computational_annotations || [],
        clinvar_context: latestResult.clinvar_context || [],
        population_observation: latestResult.population_observation || null,
        top_k_results: latestResult.top_k_results || [],
        reviewer_name: document.getElementById('reviewerName').value.trim() || null,
        review_date: document.getElementById('reviewDate').value || null,
        signature: document.getElementById('reviewSignature').value.trim() || null
      })
    });
    if (!res.ok){ const e = await res.json().catch(()=>({})); throw new Error(e.detail || 'PDF generation failed.'); }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'variant_interpretation_report.pdf'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) {
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Download PDF';
  }
}

function copyReport(){
  if (!latestResult) return;
  navigator.clipboard?.writeText(latestResult.report_text || '');
}

async function classifyVariant(){
  const query = document.getElementById('query').value.trim();
  if (!query){ return; }
  if (!getToken()){
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = 'Sign in to run a variant analysis.';
    return;
  }

  document.getElementById('empty').style.display = 'none';
  document.getElementById('results').style.display = 'none';
  document.getElementById('error').style.display = 'none';
  document.getElementById('loading').style.display = 'block';
  document.getElementById('submitBtn').disabled = true;

  const msgs = ['Authenticating request…','Extracting entities…','Retrieving evidence…','Applying classification rules…'];
  let mi = 0;
  const msgEl = document.getElementById('loadingMsg');
  const timer = setInterval(() => { mi = (mi+1) % msgs.length; msgEl.textContent = msgs[mi]; }, 900);

  try {
    // JWT bearer auth: the token identifies the account whose plan and quota
    // gate this run. The agent layer still applies its own API-key check.
    const response = await authFetch('/api/classify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query })
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 429){
      throw new Error(data.detail || 'Monthly quota exhausted. Upgrade the plan to continue.');
    }
    if (!response.ok){
      throw new Error(data.detail || data.error || 'Analysis failed.');
    }

    latestResult = data;
    renderResult(data);
    // Refresh the quota bar from the entitlement block on the response.
    refreshSession();
  } catch (err) {
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = err.message || 'Something went wrong.';
  } finally {
    clearInterval(timer);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('submitBtn').disabled = false;
  }
}

function renderResult(data){
  document.getElementById('results').style.display = 'block';

  document.getElementById('repVariantId').textContent = data.variant_id || '—';
  document.getElementById('repTimestamp').textContent = new Date().toLocaleString();
  document.getElementById('repMode').textContent = 'Reasoning: ' + (data.reasoning_mode === 'llm' ? 'LLM-assisted' : 'Rule-based');

  // Assemble every caveat the pipeline flagged into one banner, so the
  // human-review and coverage flags are surfaced instead of being dropped.
  const caveatBanner = document.getElementById('caveatBanner');
  const caveats = [];
  if (data.review_required){
    caveats.push('<b>Human review required.</b> The evidence-based call disagrees with the ClinVar reviewed consensus, so a qualified reviewer should confirm the final classification.');
  }
  if (data.coverage_fallback){
    caveats.push('<b>Reduced evidence coverage.</b> ' + escapeHtml(data.evidence_limitation ||
      'This variant was resolved from the ClinVar coverage index rather than the study workbook, so functional predictors and allele frequencies may be unavailable.'));
  }
  if (data.rsid_lookup_unsupported){
    caveats.push('<b>Unverified match.</b> ' + escapeHtml(data.retrieval_caveat ||
      'The rsID could not be resolved directly against the loaded variant index, so the result below is a free-text approximation rather than a verified match — confirm it against dbSNP/ClinVar before relying on it.'));
  }
  if (caveats.length){
    caveatBanner.style.display = 'block';
    caveatBanner.innerHTML = caveats.join('<br><br>');
  } else {
    caveatBanner.style.display = 'none';
  }

  const isGeneOverview = !!data.is_gene_overview;
  const banner = document.getElementById('geneOverviewBanner');
  const verdictSection = document.getElementById('verdictSection');
  const evidenceTitle = document.getElementById('evidenceSectionTitle');

  if (isGeneOverview){
    banner.style.display = 'block';
    banner.innerHTML = '<b>Gene overview, not a single verdict.</b> ' +
      escapeHtml(data.total_variants_in_gene ?? '?') + ' variants are indexed for ' +
      escapeHtml((data.extracted_entities||{}).gene || 'this gene') +
      ' — showing the ' + (data.top_k_results||[]).length + ' most evidence-complete below. ' +
      'BRCA1/BRCA2 variants vary widely in significance; this list is evidence facts, not a ' +
      'pathogenicity classification of the gene. Ask about a specific HGVS notation or rsID for ' +
      'a full single-variant classification.';
    verdictSection.style.display = 'none';
    evidenceTitle.textContent = 'Most evidence-complete variant in this gene';
  } else {
    banner.style.display = 'none';
    verdictSection.style.display = 'block';
    evidenceTitle.textContent = 'Evidence considered';

    const colors = classColors(data.classification);
    document.getElementById('verdictBar').style.background = colors.bar;
    document.getElementById('verdictClass').innerHTML = escapeHtml(data.classification) +
      ' <span class="pill ' + colors.pill + '">' + escapeHtml(data.confidence) + ' confidence</span>';
    const pct = Math.max(0, Math.min(100, data.confidence_numeric || 0));
    document.getElementById('confFill').style.width = pct + '%';
    document.getElementById('confFill').style.background = colors.fill;
    document.getElementById('confText').innerHTML = '<b>' + fmtNum(pct,0) + '/100</b> algorithmic confidence';
    // Provenance: is this label the tool's own evidence-based call, or a
    // ClinVar reviewed consensus it deferred to? Shown as its own short
    // row below the confidence bar (never inside the flex row above it).
    const consensus = data.consensus || {};
    const basisEl = document.getElementById('confBasis');
    const evidenceAloneNote = (suffix) =>
      consensus.evidence_based_confidence_numeric != null
        ? '<div>Evidence alone: ' + fmtNum(consensus.evidence_based_confidence_numeric, 0) +
          '/100 (' + escapeHtml(consensus.evidence_based_confidence || '') + ')' + suffix + '.</div>'
        : '';
    if (consensus.final_from === 'clinvar_consensus'){
      basisEl.innerHTML = '<div><b>Basis of final call:</b> deferred to ClinVar consensus (' +
        escapeHtml(String(consensus.clinvar_review_stars)) + '-star' +
        (consensus.clinvar_review_status ? ', ' + escapeHtml(consensus.clinvar_review_status) : '') +
        ').</div>' + evidenceAloneNote('');
    } else if (consensus.agreement === true){
      basisEl.innerHTML = '<div><b>Basis of final call:</b> evidence-based reasoning; ' +
        'agrees with ClinVar consensus.</div>' +
        evidenceAloneNote(' — confidence follows the ' +
          escapeHtml(String(consensus.clinvar_review_stars)) + '-star consensus');
    } else if (consensus.agreement === false){
      basisEl.innerHTML = '<div><b>Basis of final call:</b> evidence-based reasoning; ' +
        'disagrees with ClinVar consensus — flagged for human review.</div>';
    } else {
      basisEl.innerHTML = '<div><b>Basis of final call:</b> evidence-based ' +
        'ACMG-style reasoning.</div>';
    }
    loadReviewIntoForm(data.review_signoff);
  }

  // Extracted entities
  const ent = data.extracted_entities || {};
  const entitiesGrid = document.getElementById('entitiesGrid');
  entitiesGrid.innerHTML = '';
  [['Intent', data.intent], ['Gene', ent.gene], ['HGVS', ent.hgvs], ['Position', ent.position], ['rsID', ent.rsid]]
    .forEach(([k,v]) => {
      entitiesGrid.innerHTML += '<div class="fact"><div class="fact-k">'+k+'</div><div class="fact-v mono">'+escapeHtml(v)+'</div></div>';
    });

  // Evidence summary (human-readable extractive/LLM summary from the report agent)
  document.getElementById('evidenceSummary').textContent = data.evidence_summary || '';
  const evGrid = document.getElementById('evidenceGrid');
  evGrid.innerHTML = '';
  const top = (data.top_k_results && data.top_k_results[0]) || {};
  const evFacts = [
    ['Consequence', top.impact ? top.impact : '—'],
    ['SIFT', top.sift_prediction],
    ['PolyPhen', top.polyphen_prediction],
    ['CADD score', top.cadd_score],
  ];
  evFacts.forEach(([k,v]) => {
    evGrid.innerHTML += '<div class="fact"><div class="fact-k">'+k+'</div><div class="fact-v">'+escapeHtml(v)+'</div></div>';
  });
  if (top.conflicting_classification_flag){
    evGrid.innerHTML += '<div class="fact"><div class="fact-k">Submitter agreement</div><div class="fact-v flag">Disagreement on record</div></div>';
  }

  // ACMG criteria (validated codes only) + separated supporting context
  const critList = document.getElementById('criteriaList');
  critList.innerHTML = '';
  const acmgOnly = data.acmg_criteria_applied || data.criteria_applied || [];
  if (acmgOnly.length){
    acmgOnly.forEach(c => {
      const parts = String(c).split(' (');
      critList.innerHTML += '<span class="crit-chip"><b>'+escapeHtml(parts[0])+'</b>'+(parts[1] ? ' ('+escapeHtml(parts[1]) : '')+'</span>';
    });
  } else {
    critList.innerHTML = '<span class="section-sub">None. No definitive pathogenic or benign ACMG criterion was met.</span>';
  }
  const comp = data.computational_annotations || [];
  if (comp.length){
    critList.innerHTML += '<div class="section-sub" style="width:100%;margin-top:6px;">Computational annotations (supporting only, not ACMG criteria): '
      + comp.map(escapeHtml).join(' · ') + '</div>';
  }
  if (data.population_observation){
    critList.innerHTML += '<div class="section-sub" style="width:100%;margin-top:4px;">Population observation (BS1 unestablished): '
      + escapeHtml(data.population_observation) + '</div>';
  }
  const clinCtx = data.clinvar_context || [];
  if (clinCtx.length){
    critList.innerHTML += '<div class="section-sub" style="width:100%;margin-top:4px;">ClinVar context (not an ACMG criterion): '
      + clinCtx.map(escapeHtml).join(' · ') + '</div>';
  }

  // Top-K table
  const irMeta = data.ir_metadata || {};
  document.getElementById('irSummary').textContent = (irMeta.top_k_returned || (data.top_k_results||[]).length) + ' of ' + (irMeta.candidates_retrieved ?? '?') + ' candidates retrieved';
  const tbody = document.getElementById('topkBody');
  tbody.innerHTML = '';
  (data.top_k_results || []).forEach((r, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><span class="rank-badge">'+(i+1)+'</span></td>' +
      '<td class="mono">'+escapeHtml(r.gene)+' : '+escapeHtml(r.hgvs)+(r.exact_match ? ' <span class="exact-tag">EXACT</span>' : '')+'</td>' +
      '<td>'+escapeHtml(r.match_type)+'</td>' +
      '<td>'+fmtNum(r.relevance_score,2)+'</td>' +
      '<td>'+escapeHtml(r.sift_prediction)+' / '+escapeHtml(r.polyphen_prediction)+'</td>';
    tbody.appendChild(tr);
  });
  if (!(data.top_k_results||[]).length){
    tbody.innerHTML = '<tr><td colspan="5" class="section-sub">No ranked evidence returned.</td></tr>';
  }

  // IR metadata transparency
  const scoring = irMeta.scoring_components || {};
  let irHtml = '<p style="margin:6px 0 10px;">'+escapeHtml(irMeta.retrieval_strategy)+' — '+escapeHtml(irMeta.ranking_method)+'</p>';
  irHtml += '<table><tbody>';
  Object.entries(scoring).forEach(([k,v]) => {
    irHtml += '<tr><td style="color:var(--ink-faint);">'+escapeHtml(k.replace(/_/g,' '))+'</td><td>'+escapeHtml(v)+'</td></tr>';
  });
  irHtml += '</tbody></table>';
  document.getElementById('irMetaBody').innerHTML = irHtml;

  // Full report text
  document.getElementById('reportTextBody').textContent = data.report_text || '(no report text)';

  // Pipeline steps
  const stepsEl = document.getElementById('pipelineSteps');
  stepsEl.innerHTML = '';
  (data.pipeline_steps || []).forEach((step, i) => {
    stepsEl.innerHTML +=
      '<div class="step"><div class="step-n">'+(i+1)+'</div><div>' +
      '<div class="step-name">'+escapeHtml((step.agent||'').replace(/_/g,' '))+' — '+escapeHtml(step.stage)+'</div>' +
      '<div class="step-meta">Type: '+escapeHtml(step.output?.message_type)+' · Encrypted: '+(step.output?.encrypted ? 'yes' : 'no')+'</div>' +
      '</div></div>';
  });

  // Audit trail
  const trailEl = document.getElementById('auditTrail');
  trailEl.innerHTML = '';
  (data.audit_trail || []).forEach(m => {
    trailEl.innerHTML += '<div class="trail-row mono"><span>'+escapeHtml(m.sender)+'</span><span class="arrow">→</span><span>'+escapeHtml(m.receiver)+'</span><span>'+escapeHtml(m.message_type)+'</span><span>'+escapeHtml(m.encrypted ? 'encrypted' : 'plain')+'</span></div>';
  });
  if (!(data.audit_trail||[]).length){
    trailEl.innerHTML = '<span class="section-sub">No audit trail recorded.</span>';
  }

  // Responsible AI: clinical disclaimer + explicit fairness limitation
  const ra = data.responsible_ai || {};
  document.getElementById('clinicalDisclaimer').textContent = ra.clinical_disclaimer ||
    'This system is a decision-support tool. Results must be reviewed by a qualified genetic counselor or clinical geneticist before any clinical decision.';

  const fairnessEl = document.getElementById('fairnessLimitation');
  if (ra.fairness_limitation){
    fairnessEl.textContent = ra.fairness_limitation;
    fairnessEl.style.display = 'block';
  } else {
    fairnessEl.style.display = 'none';
  }
}

// Restore an existing session on load (if the stored token is still valid) and
// otherwise land on the marketing page with sign-in available in the header.
refreshSession();
