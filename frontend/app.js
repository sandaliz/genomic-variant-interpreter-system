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
    if (hasSession()){
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
// Guards the one-time "send a newly signed-in user to their home view" redirect,
// so a later session refresh cannot drag them out of a page they chose.
let sessionBootstrapped = false;

// ---- Loading feedback -------------------------------------------------
// The profile page and the admin dashboard each wait on several API calls, and
// Neon scales its compute to zero between requests, so the first call of a
// session can take seconds. Without a signal the page just sits there and then
// snaps into place, which reads as a broken app rather than as work happening.

// Guards against a slow response painting over a view the user has since left.
let loadSeq = 0;

// Hold a skeleton for long enough to be legible. Showing it for 80ms and then
// yanking it away is worse than showing nothing at all.
const MIN_SKELETON_MS = 250;

function startLoadBar(){
  const bar = document.getElementById('loadBar');
  if (!bar) return;
  bar.classList.remove('done');
  // Reflow so a restart animates even when the bar is already mid-width.
  void bar.offsetWidth;
  bar.classList.add('on');
}

function stopLoadBar(){
  const bar = document.getElementById('loadBar');
  if (!bar) return;
  bar.classList.add('done');
  bar.classList.remove('on');
}

// Fills containers with skeleton blocks, and returns how long to wait before
// the real content may be painted, so a fast response does not make the
// placeholder flash for a single frame.
function showSkeletons(map){
  const started = Date.now();
  Object.entries(map).forEach(([id, html]) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  });
  return Math.max(0, MIN_SKELETON_MS - (Date.now() - started));
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

// Some regions hold real, static markup (the profile form fields) that must
// survive the load. Their markup is stashed before the skeleton goes in and put
// back afterwards, so the inputs the caller then populates are the real ones.
function showSkeletonHost(id, html){
  const el = document.getElementById(id);
  if (!el || el.dataset.stashed) return;
  el.dataset.stashed = el.innerHTML;
  el.innerHTML = html;
}

function restoreSkeletonHost(id){
  const el = document.getElementById(id);
  if (!el || !el.dataset.stashed) return;
  el.innerHTML = el.dataset.stashed;
  delete el.dataset.stashed;
}

// Reusable multi-line placeholder block.
const SKELETON_LINES =
  '<div class="skeleton skeleton-line medium"></div>'
  + '<div class="skeleton skeleton-line"></div>'
  + '<div class="skeleton skeleton-line short"></div>';

// ---- Confirmation dialog ---------------------------------------------
// Every consequential action goes through here rather than window.prompt /
// window.confirm / window.alert, which cannot express a required justification,
// cannot be styled, and read as browser chrome rather than as the product.
let confirmResolver = null;

// Puts a button into a working state and back, so an action always shows that
// something is happening instead of appearing to do nothing.
function setBusy(btn, busy, label){
  if (!btn) return;
  if (busy){
    if (!btn.dataset.idleLabel) btn.dataset.idleLabel = btn.textContent;
    btn.disabled = true;
    if (label) btn.textContent = label;
  } else {
    btn.disabled = false;
    if (btn.dataset.idleLabel){
      btn.textContent = btn.dataset.idleLabel;
      delete btn.dataset.idleLabel;
    }
  }
}

/**
 * Ask the user to confirm an action, optionally collecting a note.
 *
 * Resolves to { confirmed: boolean, note: string | null }.
 *
 * opts:
 *   title, message          - required copy
 *   confirmLabel            - button text (default "Confirm")
 *   danger                  - styles the confirm button as destructive
 *   note                    - null, or:
 *                               { label, placeholder, hint, required,
 *                                 minLength, value }
 */
function askConfirm(opts){
  const o = opts || {};
  return new Promise(resolve => {
    const modal = document.getElementById('confirmModal');
    const okBtn = document.getElementById('confirmOk');
    const cancelBtn = document.getElementById('confirmCancel');
    const wrap = document.getElementById('confirmNoteWrap');
    const textarea = document.getElementById('confirmNote');
    const counter = document.getElementById('confirmNoteCount');
    const spec = o.note || null;

    document.getElementById('confirmTitle').textContent = o.title || 'Are you sure?';
    document.getElementById('confirmMsg').textContent = o.message || '';

    okBtn.textContent = o.confirmLabel || 'Confirm';
    okBtn.classList.toggle('danger', !!o.danger);

    if (spec){
      wrap.classList.remove('hidden');
      document.getElementById('confirmNoteLabel').textContent = spec.label || 'Note';
      document.getElementById('confirmNoteHint').textContent =
        spec.hint || (spec.required ? 'This is required.' : 'Optional.');
      textarea.placeholder = spec.placeholder || '';
      textarea.value = spec.value || '';
    } else {
      wrap.classList.add('hidden');
      textarea.value = '';
    }

    function updateCounter(){
      const max = Number(textarea.getAttribute('maxlength')) || 500;
      const used = textarea.value.trim().length;
      const min = (spec && spec.minLength) || 0;
      counter.textContent = used + ' / ' + max;
      const short = min > 0 && used > 0 && used < min;
      counter.classList.toggle('short', short || (min > 0 && used === 0 && spec.required));
      okBtn.disabled = !!spec && spec.required && used < min;
      if (short) counter.textContent = used + ' / ' + max + ' (min ' + min + ')';
    }

    // Called on EVERY open, not just when there is a note. updateCounter() is the
    // only thing that writes okBtn.disabled, so a dialog that skips it inherits
    // whatever the previous dialog left behind -- which is how a required-note
    // dialog (upgrade justification, admin decline) could permanently lock the
    // Confirm button on every later dialog that has no note, including the
    // clinical sign-off. With no spec, min is 0 and this always re-enables.
    updateCounter();

    function finish(confirmed){
      const note = spec ? (textarea.value.trim() || null) : null;
      modal.classList.add('hidden');
      confirmResolver = null;
      // Backstop: never hand the next dialog an inherited disabled button.
      okBtn.disabled = false;
      counter.classList.remove('short');
      document.removeEventListener('keydown', onKey, true);
      if (okBtn._onclick) okBtn.removeEventListener('click', okBtn._onclick);
      if (cancelBtn._onclick) cancelBtn.removeEventListener('click', cancelBtn._onclick);
      if (textarea._oninput) textarea.removeEventListener('input', textarea._oninput);
      resolve({ confirmed, note });
    }

    function onKey(e){
      if (e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); finish(false); }
    }

    okBtn._onclick = () => finish(true);
    cancelBtn._onclick = () => finish(false);
    okBtn.addEventListener('click', okBtn._onclick);
    cancelBtn.addEventListener('click', cancelBtn._onclick);
    textarea._oninput = updateCounter;
    textarea.addEventListener('input', textarea._oninput);
    // Clicking the backdrop cancels, like the auth modal.
    modal.onclick = e => { if (e.target === modal) finish(false); };
    document.addEventListener('keydown', onKey, true);
    confirmResolver = finish;

    modal.classList.remove('hidden');
    if (spec){ setTimeout(() => textarea.focus(), 30); }
    else setTimeout(() => okBtn.focus(), 30);
  });
}

// Lets Escape close a confirmation even when focus is inside it.
function closeConfirm(){
  if (confirmResolver) confirmResolver(false);
}

function showView(name, opts){
  const fromHistory = opts && opts.fromHistory;
  if (name === 'workspace' && !hasSession()){
    // Never show the query surface to someone who cannot use it yet.
    pendingQuery = document.getElementById('query').value.trim() || null;
    openAuth('signin');
    return;
  }
  if (name === 'admin' && !(currentUser && currentUser.user.role === 'admin')){
    // UI convenience only -- the endpoints enforce require_admin regardless.
    if (hasSession()){
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
  // Keep the address bar in step, unless the browser back button drove this.
  if (!fromHistory) pushViewUrl(name);
  window.scrollTo({ top: 0, behavior: 'auto' });
  if (name === 'profile') loadProfile();
  if (name === 'admin') loadAdminPage();
}

// ---- Routing ------------------------------------------------------------
// The four views are one document, so the URL is driven by history.pushState
// rather than by server round trips. Every view has a real slug, which means the
// back button, a refresh and a pasted link all land in the right place.
const VIEW_SLUGS = {
  landing: '/',
  workspace: '/analyse',
  profile: '/profile',
  admin: '/admin-dashboard',
};

function slugFor(view){
  return VIEW_SLUGS[view] || '/';
}

function viewForSlug(pathname){
  const slug = (pathname || '/').replace(/\/+$/, '').toLowerCase() || '/';
  return Object.keys(VIEW_SLUGS).find(v => VIEW_SLUGS[v] === slug) || null;
}

// Push the URL without reloading. Skipped when the URL already matches, so
// clicking the current view's nav item does not pile up history entries.
function pushViewUrl(view){
  const target = slugFor(view);
  if (window.location.pathname === target) return;
  try {
    window.history.pushState({ view }, '', target);
  } catch (err){
    // file:// origins reject pushState. The app still works without deep links.
  }
}

window.addEventListener('popstate', () => {
  const view = viewForSlug(window.location.pathname);
  if (view) showView(view, { fromHistory: true });
});

// Where a signed-in user should land straight after signing in. An
// administrator's primary surface is the dashboard, not the query box.
function homeViewFor(me){
  if (!me) return 'landing';
  return (me.user && me.user.role === 'admin') ? 'admin' : 'workspace';
}

// The logo means "home" -- the marketing page -- for everyone.
//
// It used to double as a shortcut into the query surface, which made the header
// ambiguous: two different things were called "home". The dedicated Analyse
// button now owns the workspace, the Admin button owns the dashboard, and the
// profile lives in the avatar menu.
function goHome(){
  showView('landing');
}

// Header/footer navigation to a landing-page section.
//
// The nav is global, so a link may be clicked from the workspace or the admin
// dashboard, where the target section does not exist. Switch to the landing view
// first, then scroll -- doing it in that order is what makes the anchor resolve.
function goToSection(id){
  const scroll = () => {
    const target = document.getElementById(id);
    if (!target) return;
    const header = document.querySelector('.site-header');
    const offset = header ? header.offsetHeight + 16 : 16;
    const top = target.getBoundingClientRect().top + window.pageYOffset - offset;
    window.scrollTo({ top, behavior: 'smooth' });
  };

  if (currentView !== 'landing'){
    showView('landing');
    // Let the view swap render before measuring.
    requestAnimationFrame(() => requestAnimationFrame(scroll));
  } else {
    scroll();
  }
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
// The session token lives in an HttpOnly cookie, which JavaScript cannot read.
// That is the point: an XSS bug elsewhere on the page can no longer exfiltrate a
// working credential, because there is nothing in script to steal.
//
// A second, deliberately readable cookie holds the CSRF token. Its presence is
// what tells us a session exists -- the server never has to expose the token
// itself for the client to answer "am I signed in?".
//
// Bearer tokens still work for scripted clients (curl, the test suite): the API
// accepts either transport, and only cookie-authenticated writes need the CSRF
// header.
const CSRF_COOKIE = 'variantlens_csrf';
const CSRF_HEADER = 'X-CSRF-Token';

function readCookie(name){
  const match = document.cookie.match(
    new RegExp('(?:^|;\\s*)' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : null;
}

// "Do we have a session?" -- answered without ever seeing the token itself.
function hasSession(){ return !!readCookie(CSRF_COOKIE); }

function csrfToken(){ return readCookie(CSRF_COOKIE); }

// The server clears both cookies on sign-out. This has to cover the case the
// server cannot: an expired session cookie leaves the CSRF cookie behind, and
// because that cookie is deliberately readable we can and should drop it here.
// Otherwise hasSession() keeps reporting a session that no longer exists, and
// the UI oscillates between signed-in and signed-out.
function clearSession(){
  localStorage.removeItem('variantlens_token');
  document.cookie = CSRF_COOKIE + '=; Max-Age=0; Path=/; SameSite=lax';
}

function authHeaders(extra){
  const headers = Object.assign({}, extra || {});
  // Sent on every request so cookie-authenticated writes can be verified.
  const csrf = csrfToken();
  if (csrf) headers[CSRF_HEADER] = csrf;
  return headers;
}

async function authFetch(path, options){
  const opts = Object.assign({}, options || {}, {
    headers: authHeaders((options || {}).headers),
    // Send the session cookie. Same-origin, so no cross-site credential.
    credentials: 'same-origin',
  });
  const res = await fetch(path, opts);
  if (res.status === 401 && hasSession()){
    // Expired, revoked or deactivated session: drop it and ask again.
    clearSession();
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

function applySessionVisibility(me){
  /*
   * Drives every [data-when] element: "out" is for anonymous visitors, "in" for
   * a signed-in account. The landing page is a marketing page as well as a
   * product one, so offering "Create a free account" and "Sign in" to someone
   * who is already signed in is both confusing and faintly insulting.
   *
   * Declared in the markup rather than wired up id by id in JS, so adding a
   * conditional CTA is a one-attribute change.
   */
  const isIn = !!me;
  document.querySelectorAll('[data-when]').forEach(el => {
    el.classList.toggle('hidden', (el.dataset.when === 'in') !== isIn);
  });
  const out = document.getElementById('footerAccountOut');
  const inCol = document.getElementById('footerAccountIn');
  if (out) out.classList.toggle('hidden', isIn);
  if (inCol) inCol.classList.toggle('hidden', !isIn);
  if (me){
    const adminLink = document.getElementById('footerAdminLink');
    if (adminLink) adminLink.classList.toggle('hidden', me.user.role !== 'admin');
  }
}

function renderSession(me){
  applySessionVisibility(me);
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
  document.getElementById('analyseNavBtn').classList.remove('hidden');
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

  // Landing-page copy that should not offer a plan the visitor is already on.
  const heroNote = document.getElementById('heroSignedInNote');
  if (heroNote){
    heroNote.textContent = (limit === null || limit === undefined)
      ? 'Signed in as ' + displayName + ' · ' + me.plan.name + ' plan · unlimited queries'
      : 'Signed in as ' + displayName + ' · ' + me.plan.name + ' plan · '
        + used + ' / ' + limit + ' queries used this month';
  }
  const freeNote = document.getElementById('freePlanNote');
  if (freeNote){
    const onFree = String(me.plan.name).trim().toLowerCase() === 'free';
    freeNote.textContent = onFree
      ? 'You are on this plan.'
      : 'You are on ' + me.plan.name + '. Switch plans from your account.';
  }
}

async function refreshSession(opts){
  const silent = opts && opts.silent;
  if (!hasSession()){ renderSession(null); showView('landing'); return; }
  // A stored token means the first call of the session is a round trip to a
  // possibly-waking database. Show the bar so the landing page does not just sit
  // there before jumping to the workspace. Background refreshes (after a query)
  // skip it -- flashing a progress bar over a finished result is noise.
  if (!silent) startLoadBar();
  try {
    const res = await fetch('/api/auth/me', { headers: authHeaders() });
    if (!res.ok){ clearSession(); renderSession(null); showView('landing'); return; }
    const me = await res.json();
    renderSession(me);
    // Landing is what a signed-out visitor sees; once signed in we go to the
    // workspace unless a landing example already queued a query.
    if (pendingQuery){
      const q = pendingQuery; pendingQuery = null;
      document.getElementById('query').value = q;
      showView('workspace');
    } else if (currentView === 'landing' && !sessionBootstrapped){
      // Only on the very first load of the session. Without this guard, a
      // signed-in user who deliberately clicked the logo to read the landing
      // page would be dragged back into the tool by the next session refresh --
      // which also runs after every query, to update the quota bar.
      sessionBootstrapped = true;
      // Honour a deep link first: someone who pasted /profile or
      // /admin-dashboard should land where they asked, not on the default.
      const requested = viewForSlug(window.location.pathname);
      if (requested && requested !== 'landing'){
        showView(requested);
      } else {
        showView(homeViewFor(me));
      }
    }
  } catch (err){
    clearSession();
    renderSession(null);
    showView('landing');
  } finally {
    if (!silent) stopLoadBar();
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
  setBusy(btn, true, 'Signing in…');
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: document.getElementById('loginUsername').value.trim(),
        password: document.getElementById('loginPassword').value
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(describeAuthError(data, 'Sign in failed.'));
    // The session cookie is already set by the response; nothing to store here.
    document.getElementById('loginPassword').value = '';
    await refreshSession();
    // Dismiss the modal only once the session is actually established, so a
    // failure leaves the form up with its error still visible.
    closeAuth();
  } catch (e){
    err.textContent = e.message;
  } finally {
    setBusy(btn, false);
  }
  return false;
}

async function doRegister(event){
  event.preventDefault();
  const btn = document.getElementById('registerBtn');
  const err = document.getElementById('registerError');
  err.textContent = '';
  setBusy(btn, true, 'Creating account…');
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
    // The session cookie is already set by the response.
    document.getElementById('regPassword').value = '';
    await refreshSession();
  } catch (e){
    err.textContent = e.message;
  } finally {
    setBusy(btn, false);
  }
  return false;
}

async function doForgotPassword(event){
  event.preventDefault();
  const btn = document.getElementById('forgotBtn');
  const err = document.getElementById('forgotError');
  err.textContent = '';
  setBusy(btn, true, 'Sending…');
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
    setBusy(btn, false);
  }
  return false;
}

async function doConfirmReset(event){
  event.preventDefault();
  const btn = document.getElementById('resetBtn');
  const err = document.getElementById('resetError');
  err.textContent = '';
  setBusy(btn, true, 'Updating…');
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
    closeAuth();
    renderSession(body);
    showView('workspace');
  } catch (e){
    err.textContent = e.message;
  } finally {
    setBusy(btn, false);
  }
  return false;
}

function logout(){
  // Ask the server to clear the cookies first. Locally forgetting things is not
  // enough: the HttpOnly cookie is unreadable from script and would simply be
  // re-sent on the next request.
  authFetch('/api/auth/logout', { method: 'POST' })
    .catch(() => {})
    .finally(() => {
      currentUser = null;
      // So the next person to sign in is routed to their home view again.
      sessionBootstrapped = false;
      renderSession(null);
      closeAuth();
      latestResult = null;
      document.getElementById('results').style.display = 'none';
      document.getElementById('empty').style.display = 'block';
      showView('landing');
    });
}

// ---- Admin: shared state + account controls ------------------------
// Rendered on the dedicated admin page. Every call below is authorised
// server-side by require_admin(), so hiding anything here is a UI affordance,
// not a control.
let adminPlans = [];

function renderPlanCards(plans){
  const el = document.getElementById('adminPlanCards');
  if (!el) return;
  el.innerHTML = plans.map(p => {
    const price = p.price_cents === null || p.price_cents === undefined
      ? 'Custom'
      : '$' + (p.price_cents / 100).toFixed(2);
    const limit = p.is_unlimited ? 'Unlimited' : (p.monthly_query_limit + '/mo');
    // Mark the plan the signed-in user is actually on, if we know it.
    const isCurrent = !!(currentUser && currentUser.plan
      && currentUser.plan.id === p.id);
    return '<div class="plan-card' + (isCurrent ? ' active' : '') + '">'
      + '<div class="pc-name">' + escapeHtml(p.name)
      + (isCurrent ? '<span class="pc-flag">Your plan</span>' : '') + '</div>'
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
      + '<td><select class="admin-select" onchange="adminChangePlan(' + u.id
      + ', this.value, this)">' + planOptions(u.plan_id) + '</select></td>'
      + '<td>' + u.used_this_month + '</td>'
      + '<td><button class="admin-btn" onclick="adminToggleActive(' + u.id + ', '
      + u.is_active + ')">' + (u.is_active ? 'Deactivate' : 'Activate') + '</button></td>';
    body.appendChild(tr);
  });
}

async function adminChangePlan(userId, planId, selectEl){
  const target = adminPlans.find(p => String(p.id) === String(planId));
  const answer = await askConfirm({
    title: 'Change plan?',
    message: 'This moves the account onto the '
      + (target ? target.name : 'selected') + ' plan straight away, changing their '
      + 'monthly query allowance and which reasoning engine is available.',
    confirmLabel: 'Change plan',
  });
  if (!answer.confirmed){
    // Put the select back where it was; the user did not agree to the change.
    await loadAdminPanel();
    return;
  }

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
  const answer = await askConfirm({
    title: next ? 'Activate this account?' : 'Deactivate this account?',
    message: next
      ? 'The account can sign in and run queries again immediately.'
      : 'The account is signed out and blocked from signing in again. Their stored '
        + 'data and sign-off history are kept.',
    confirmLabel: next ? 'Activate account' : 'Deactivate account',
    danger: !next,
  });
  if (!answer.confirmed) return;

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
  if (!el) return;
  el.textContent = text || '';
  el.className = 'form-msg' + (kind ? ' ' + kind : '');
}

// Writes a value into a field, tolerating a missing element.
//
// The profile form lives inside skeleton hosts whose innerHTML is replaced
// wholesale, so a field can genuinely be absent at the moment it is written to.
// Setting .value on null threw "Cannot set properties of null" and aborted the
// whole load, which is why "Your plan" used to render blank.
function setFieldValue(id, value){
  const el = document.getElementById(id);
  if (!el){
    console.warn('setFieldValue: #' + id + ' is not in the document');
    return false;
  }
  el.value = value == null ? '' : value;
  return true;
}

function setFieldDisabled(id, disabled){
  const el = document.getElementById(id);
  if (el) el.disabled = disabled;
}

async function loadProfile(){
  if (!hasSession()){ openAuth('signin'); return; }
  // The plan catalogue used to come from the admin-only endpoint, which left a
  // regular user with an empty comparison and no upgrade button. Fetch it from
  // the user-accessible /api/plans alongside the account details.
  const seq = ++loadSeq;
  startLoadBar();
  // The plan box is the slow part: three round trips including the upgrade
  // history. The account fields fill quickly but must not appear to be editable
  // before we know who is signed in.
  const settle = showSkeletons({
    pfPlanBox: '<div class="skeleton skeleton-row">'
      + '<div class="skeleton skeleton-line short"></div>'
      + '<div class="skeleton skeleton-line medium"></div></div>'
      + '<div class="skeleton skeleton-block"></div>'
  });
  showSkeletonHost('pfAccount', SKELETON_LINES);
  showSkeletonHost('pfPassword', SKELETON_LINES);
  ['pfUsername', 'pfFullName', 'pfEmail', 'pfCurrentPw', 'pfOldPw', 'pfNewPw']
    .forEach(id => setFieldDisabled(id, true));

  try {
    const [res, plansRes] = await Promise.all([
      authFetch('/api/auth/me'),
      authFetch('/api/plans')
    ]);
    if (!res.ok){ openAuth('signin'); return; }
    const me = await res.json();
    currentUser = me;
    if (plansRes.ok){
      adminPlans = (await plansRes.json()).plans || [];
    }
    await loadMyUpgradeRequests();
    if (seq !== loadSeq) return;
    await wait(settle);

    // Put the real form fields back BEFORE writing to them. showSkeletonHost()
    // replaced their markup wholesale, so right now every #pf* input is a stale
    // reference to a detached node. Restoring after the assignments -- which is
    // what the finally block used to do -- is what produced
    // "Cannot set properties of null (setting 'value')" and a blank plan card.
    restoreSkeletonHost('pfAccount');
    restoreSkeletonHost('pfPassword');

    setFieldValue('pfUsername', me.user.username);
    setFieldValue('pfFullName', me.user.full_name);
    setFieldValue('pfEmail', me.user.email);
    setFieldValue('pfCurrentPw', '');
    renderPlanBox(me);
  } catch (err){
    if (seq !== loadSeq) return;
    // Log it: the previous version surfaced the failure into a box that
    // clearProfileSkeletons() immediately emptied, so a real error rendered as
    // a blank card with no explanation anywhere.
    console.error('loadProfile failed:', err);
    clearProfileSkeletons();
    // Written last, and never cleared afterwards, so the user sees why.
    const box = document.getElementById('pfPlanBox');
    if (box){
      box.innerHTML = '<div class="form-msg err">Could not load your plan: '
        + escapeHtml(err.message || String(err))
        + '</div>';
    }
  } finally {
    if (seq === loadSeq){
      stopLoadBar();
      // Idempotent safety net: covers the early-return paths above, which bail
      // out before the restore above could run.
      restoreSkeletonHost('pfAccount');
      restoreSkeletonHost('pfPassword');
      // pfUsername is deliberately disabled by design -- it is the account
      // identifier -- so only the rest are re-enabled here.
      ['pfFullName', 'pfEmail', 'pfCurrentPw', 'pfOldPw', 'pfNewPw']
        .forEach(id => setFieldDisabled(id, false));
    }
  }
}

function clearProfileSkeletons(){
  // Restores the real form fields and removes the plan-box placeholder. It must
  // not be called after an error message has been written into the plan box.
  restoreSkeletonHost('pfAccount');
  restoreSkeletonHost('pfPassword');
  const box = document.getElementById('pfPlanBox');
  if (box) box.innerHTML = '';
}

function currentPlanSummary(me){
  // Rendered from /api/auth/me alone, so it works even when the plan catalogue
  // request failed.
  const plan = (me && me.plan) || null;
  const usage = (me && me.usage) || null;
  if (!plan) return '';
  const cap = plan.monthly_query_limit;
  const allowance = plan.is_unlimited || cap === null || cap === undefined
    ? 'Unlimited queries'
    : cap + ' queries / month';
  const used = usage && usage.used_this_month !== undefined
    ? usage.used_this_month : null;
  const pct = used !== null && cap ? Math.min(100, Math.round((used / cap) * 100)) : null;

  return '<div class="current-plan">'
    + '<div class="cp-head">'
    + '<span class="cp-label">Current plan</span>'
    + '<span class="cp-name">' + escapeHtml(plan.name) + '</span>'
    + '</div>'
    + '<div class="cp-meta">' + escapeHtml(allowance)
    + (plan.price_cents === null || plan.price_cents === undefined
        ? ' · custom pricing'
        : ' · $' + (plan.price_cents / 100).toFixed(2) + ' / month')
    + '</div>'
    + (pct !== null
        ? '<div class="cp-usage"><div class="quota-bar">'
          + '<div class="quota-fill' + (pct >= 80 ? ' warn' : '') + '" style="width:'
          + pct + '%"></div></div>'
          + '<span class="cp-usage-text">' + used + ' of ' + cap + ' used this month</span></div>'
        : '')
    + '</div>';
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

  // Always lead with what the account is actually on. The comparison below can
  // fail -- the catalogue endpoint hiccups, a plan object is missing a field --
  // and a blank "Your plan" card reads as a broken page rather than a failed
  // fetch. The current tier comes from /api/auth/me and does not depend on the
  // catalogue at all.
  box.innerHTML += currentPlanSummary(me);

  if (!adminPlans.length){
    box.innerHTML += '<p class="field-hint">Plan options are unavailable right now. '
      + 'Please try again shortly.</p>';
    return;
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
         : '<button class="btn-primary" onclick="requestUpgrade('
           + p.id + ')">Request upgrade</button>')
      + '</div>';
  }).join('');

  box.innerHTML += '<div class="plan-compare">' + cards + '</div>'
    + '<p class="field-hint">Upgrade requests go to an administrator, who approves '
    + 'or declines them. No payment is taken here.</p>';
}
// ---- Admin page ------------------------------------------------------

async function loadAdminPage(){
  // Four calls, one of which aggregates over the usage table. On a cold Neon
  // compute start this is the slowest screen in the app, so every region gets a
  // placeholder shaped like the content that is coming.
  const seq = ++loadSeq;
  startLoadBar();
  const settle = showSkeletons({
    statRow: '<div class="skeleton skeleton-card"></div>'.repeat(4),
    usageChart: '<div class="skeleton skeleton-block"></div>',
    upgradeQueue: '<div class="skeleton skeleton-row">' + SKELETON_LINES + '</div>'.repeat(2),
    adminPlanCards: '<div class="skeleton skeleton-card"></div>'.repeat(3),
    adminUserBody: '<tr><td colspan="4">'
      + '<div class="skeleton skeleton-line medium"></div></td></tr>'.repeat(4)
  });

  try {
    const [analyticsRes, queueRes, usersRes, plansRes] = await Promise.all([
      authFetch('/api/admin/analytics'),
      authFetch('/api/admin/upgrade-requests'),
      authFetch('/api/admin/users?limit=100'),
      authFetch('/api/admin/plans')
    ]);
    // A clinician reaching this view is expected, not an error to shout about.
    if (analyticsRes.status === 403 || queueRes.status === 403){
      adminMessage('Admin role required. This account cannot reach the dashboard.', true);
      clearAdminSkeletons();
      return;
    }
    const failed = [
      ['analytics', analyticsRes], ['upgrade requests', queueRes],
      ['accounts', usersRes], ['plans', plansRes]
    ].filter(([, r]) => !r.ok).map(([label, r]) => `${label} (${r.status})`);
    if (failed.length){
      // Previously this returned silently, leaving a blank dashboard with no
      // explanation at all.
      adminMessage('Could not load the dashboard: ' + failed.join(', ')
        + '. Please retry.', true);
      clearAdminSkeletons();
      return;
    }

    const analytics = await analyticsRes.json();
    const queue = await queueRes.json();
    const plans = (await plansRes.json()).plans;
    const users = (await usersRes.json()).users;

    // The user may have navigated away while these were in flight.
    if (seq !== loadSeq) return;
    await wait(settle);

    renderStats(analytics);
    renderChart(analytics.daily_usage);
    renderQueue(queue);
    adminPlans = plans;
    renderPlanCards(adminPlans);
    renderAdminUsers(users);
    adminMessage('');
  } catch (err){
    if (seq !== loadSeq) return;
    adminMessage('Could not load the dashboard: ' + err.message, true);
    clearAdminSkeletons();
  } finally {
    if (seq === loadSeq) stopLoadBar();
  }
}

function clearAdminSkeletons(){
  ['statRow', 'usageChart', 'upgradeQueue', 'adminPlanCards', 'adminUserBody']
    .forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '';
    });
}

// Lightweight refresh of just the accounts table.
//
// The plan/active controls call this to snap their own UI back to the stored
// value after a change. It used to be called `loadAdminPanel`, which was never
// defined -- so every plan change and every activate/deactivate threw a
// ReferenceError and left the table showing the value that had just been
// rejected. Reloading only this table also avoids flashing the whole dashboard's
// skeletons for a one-row edit.
async function loadAdminPanel(){
  const res = await authFetch('/api/admin/users?limit=100');
  if (!res.ok){
    adminMessage('Could not refresh the account list.', true);
    return;
  }
  renderAdminUsers((await res.json()).users);
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
        + '<button class="admin-btn primary" onclick="decideUpgrade(' + r.id
        + ',\'approved\')">Approve</button>'
        + '<button class="admin-btn danger" onclick="decideUpgrade(' + r.id
        + ',\'declined\')">Decline</button></div>'
      : '';
    // The justification is the reason this queue exists, so it is given the
    // strongest visual weight on the card. Requests raised before the field
    // became required have none; say so rather than showing an empty block.
    const justification = r.note
      ? '<div class="justification"><p class="justification-label">'
        + 'Why they need it</p>'
        + '<p class="justification-text">' + escapeHtml(r.note) + '</p></div>'
      : '<div class="justification missing"><p class="justification-label">'
        + 'Why they need it</p>'
        + '<p class="justification-text">No justification was recorded for this '
        + 'request.</p></div>';

    return '<div class="queue"><div class="queue-head"><div>'
      + '<div class="queue-who">' + escapeHtml(r.username) + ' &rarr; '
      + escapeHtml(r.target_plan) + '</div>'
      + '<div class="queue-mail">' + escapeHtml(r.email) + ' · currently '
      + escapeHtml(r.current_plan) + '</div></div>'
      + '<span class="status-pill ' + escapeHtml(r.status) + '">'
      + escapeHtml(r.status) + '</span></div>'
      + justification
      + (r.admin_note ? '<div class="queue-admin-note">'
        + '<span class="queue-admin-note-label">Your note</span>'
        + escapeHtml(r.admin_note) + '</div>' : '')
      + actions + '</div>';
  }).join('');
}

const UPGRADE_DECISIONS = {
  contacted: {
    label: 'Mark as contacted',
    verb: 'Mark contacted',
    blurb: 'Records that you reached out to the requester. The request stays open '
      + 'and they can see your note.',
    confirmLabel: 'Mark contacted',
    danger: false,
    noteLabel: 'What did you tell them? (optional)',
    noteHint: 'Shown to the requester.',
    required: false,
  },
  approved: {
    label: 'Approve',
    verb: 'Approve this upgrade',
    blurb: 'This moves the requester onto the new plan immediately and closes the '
      + 'request. It cannot be undone from this screen.',
    confirmLabel: 'Approve upgrade',
    danger: false,
    noteLabel: 'Note for the requester (optional)',
    noteHint: 'Shown to the requester alongside the new plan.',
    required: false,
  },
  declined: {
    label: 'Decline',
    verb: 'Decline this upgrade',
    blurb: 'The requester stays on their current plan. They can read your note, so '
      + 'saying why is worth the effort.',
    confirmLabel: 'Decline request',
    danger: true,
    noteLabel: 'Reason for declining (optional)',
    noteHint: 'Shown to the requester. Explaining the decision saves a follow-up email.',
    required: false,
  },
};

async function decideUpgrade(id, status){
  const spec = UPGRADE_DECISIONS[status];
  if (!spec) return;

  const answer = await askConfirm({
    title: spec.verb + '?',
    message: spec.blurb,
    confirmLabel: spec.confirmLabel,
    danger: spec.danger,
    note: {
      label: spec.noteLabel,
      placeholder: status === 'declined'
        ? 'e.g. Budget is committed to Q3. Re-request once the new quarter opens.'
        : '',
      hint: spec.noteHint,
      required: spec.required
    }
  });
  if (!answer.confirmed) return;

  const res = await authFetch('/api/admin/upgrade-requests/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ status, admin_note: answer.note })
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
  const body = { full_name: document.getElementById('pfFullName').value };
  const email = document.getElementById('pfEmail').value.trim();
  const currentPw = document.getElementById('pfCurrentPw').value;
  // The API demands the current password for an email change; sending it only
  // when the address actually changed keeps a cosmetic save frictionless.
  const emailChanged = email && email !== (currentUser && currentUser.user.email);
  if (emailChanged){
    body.email = email;
    if (currentPw) body.current_password = currentPw;
  }

  // An email change is the one profile edit with consequences beyond this page:
  // it moves the sign-in identifier and the password-recovery address, and the
  // old one stops working immediately.
  if (emailChanged){
    const answer = await askConfirm({
      title: 'Change your email address?',
      message: 'You will sign in with ' + email + ' from now on, and password '
        + 'recovery codes will be sent there. Your current address stops working '
        + 'for both straight away.',
      confirmLabel: 'Change email',
      danger: true,
    });
    if (!answer.confirmed) return;
  }

  setMsg('pfMsg', 'Saving…');
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
  const oldPw = document.getElementById('pfOldPw').value;
  const newPw = document.getElementById('pfNewPw').value;
  if (!oldPw || !newPw){
    setMsg('pwMsg', 'Enter both your current and your new password.', 'err');
    return;
  }
  const answer = await askConfirm({
    title: 'Change your password?',
    message: 'Your current password stops working immediately. If you are signed '
      + 'in anywhere else, you will need the new password there.',
    confirmLabel: 'Change password',
  });
  if (!answer.confirmed) return;

  setMsg('pwMsg', 'Changing…');
  try {
    const res = await authFetch('/api/auth/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ current_password: oldPw, new_password: newPw })
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
  const name = plan ? plan.name : 'this plan';
  const limit = plan
    ? (plan.is_unlimited ? 'unlimited queries' : (plan.monthly_query_limit + ' queries a month'))
    : '';

  const answer = await askConfirm({
    title: 'Request the ' + name + ' plan',
    message: 'An administrator reviews every upgrade request and records a decision. '
      + 'Your current plan and quota stay active while you wait'
      + (limit ? ' (' + limit + ' is what you are asking for).' : '.'),
    confirmLabel: 'Send request',
    note: {
      label: 'Why do you need this plan?',
      placeholder: 'e.g. We screen 60 BRCA variants a week and have run out of the '
        + '10 monthly queries. LLM-assisted reasoning is needed for the ambiguous '
        + 'cases the rules cannot classify on their own.',
      hint: 'The administrator reads this to make their decision, so be specific.',
      required: true,
      minLength: 10,
    }
  });
  if (!answer.confirmed) return;

  const res = await authFetch('/api/upgrade-requests', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ target_plan_id: planId, note: answer.note })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok){
    await askConfirm({
      title: 'Could not send the request',
      message: describeAuthError(data, 'Please try again.'),
      confirmLabel: 'Close'
    });
    return;
  }
  await loadMyUpgradeRequests();
  await loadProfile();
  // loadProfile() has already shown the loading bar for this screen.
  await refreshSession({ silent: true });
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
  // A sign-off is an attestation about patient-relevant output, so it gets an
  // explicit confirmation that names who is being recorded as the reviewer.
  const answer = await askConfirm({
    title: 'Record this sign-off?',
    message: 'This records ' + reviewer + ' as having reviewed '
      + latestResult.variant_id + ' on ' + reviewDate
      + '. It is stored with your account and timestamped in the audit register.',
    confirmLabel: 'Record sign-off',
  });
  if (!answer.confirmed) return;
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
  if (!hasSession()){
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
    // Refresh the quota bar from the entitlement block on the response. Silent:
    // the result is already on screen and a progress bar would be noise.
    refreshSession({ silent: true });
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
