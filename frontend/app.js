/* ==========================================================================
   Landing page imagery.

   ONE place to swap the pictures. Every <img> on the marketing page carries a
   data-img key instead of a src, and applyLandingImages() fills them in from
   this object at startup -- so replacing a photograph is a one-line edit here
   rather than a hunt through the markup.

   Drop the files into frontend/images/landing/ and point the keys at them. The
   layout deliberately does not depend on their dimensions: each frame owns its
   own aspect-ratio and the image is object-fit:cover inside it, so a source of
   any shape crops correctly instead of distorting.

   The shipped artwork is SVG -- resolution independent, a few KB each, and drawn
   from the product itself rather than from stock photography: the workspace with
   a real query in it, the pipeline's own stages, the criteria its classification
   code applies, and the corpus it indexes. Rename any value to .jpg/.webp to swap
   in a photograph, but the drawings are the product, not stand-ins for one.
   ========================================================================== */
const LANDING_IMAGE_BASE = '/static/images/landing/';

const landingImages = {
  // Hero: the workspace, with the query the page itself suggests and the call it
  // produced.
  hero:            LANDING_IMAGE_BASE + 'hero.svg',
  // The three platform cards, in pipeline order.
  variantAnalysis: LANDING_IMAGE_BASE + 'variant-analysis.svg',
  genetics:        LANDING_IMAGE_BASE + 'genetics.svg',
  clinical:        LANDING_IMAGE_BASE + 'clinical-interpretation.svg',
  // The precision-analysis split section: the three rankers over one corpus.
  retrieval:       LANDING_IMAGE_BASE + 'retrieval.svg',
  // The closing call to action's background field: the indexed corpus.
  corpus:          LANDING_IMAGE_BASE + 'corpus.svg',
  // The three insight panels.
  solutionOne:     LANDING_IMAGE_BASE + 'solution-1.svg',
  solutionTwo:     LANDING_IMAGE_BASE + 'solution-2.svg',
  solutionThree:   LANDING_IMAGE_BASE + 'solution-3.svg',
};

// Fill every [data-img] placeholder from the map above. A key with no entry is
// left alone and named in the console, so a typo surfaces as one missing picture
// with a clear reason rather than as a page full of broken-image icons.
function applyLandingImages(){
  document.querySelectorAll('[data-img]').forEach(img => {
    const key = img.dataset.img;
    const src = landingImages[key];
    if (!src){
      console.warn('landingImages has no entry for "' + key + '"');
      return;
    }
    if (img.getAttribute('src') !== src) img.src = src;
  });
}

const EXAMPLES = [
  { label: 'Gene + variant',  q: "Is BRCA1 NC_000017.10:g.41244442A>G pathogenic?" },
  { label: 'Short form',      q: "Could you assess BRCA2:c.5946delT and highlight the key evidence relevant to its classification?" },
  { label: 'By rsID',         q: "I found rs80356943 in a patient. What does the available evidence suggest about this variant?" },
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

// ---- Transient confirmations ------------------------------------------
// Several actions used to complete in total silence -- copying the report,
// downloading the PDF, recording a sign-off -- so a success was indistinguishable
// from a click that missed. A toast says "that worked" without navigating away or
// interrupting whatever the user does next.
let toastTimer = null;

function showToast(message, kind){
  const wrap = document.getElementById('toastWrap');
  if (!wrap || !message) return;
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = message;
  // One at a time: a second toast replaces the first rather than stacking up.
  wrap.innerHTML = '';
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => { if (el.parentNode === wrap) wrap.removeChild(el); }, 260);
  }, 2800);
}

// ---- Keyboard shortcuts --------------------------------------------------
// A query tool earns its keep from the keyboard. "/" focuses the query box the
// way every search field on the web does, and modifier+Enter runs it without a
// trip to the mouse. Both are suppressed while a field already has focus, so
// typing a "/" into a password never jumps the cursor out of it.
function isTypingTarget(el){
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select'
    || el.isContentEditable === true;
}

document.addEventListener('keydown', e => {
  if (e.key === '/' && currentView === 'workspace'
      && !isTypingTarget(document.activeElement)){
    const box = document.getElementById('query');
    if (box){
      e.preventDefault();
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    }
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter'
      && currentView === 'workspace' && hasSession()){
    e.preventDefault();
    classifyVariant();
  }
});

// ---- Command palette -----------------------------------------------------
// Cmd/Ctrl+K from anywhere. Every entry here also has a button somewhere else:
// this is a second route to the same actions, not a parallel implementation of
// them, so the two can never drift apart in behaviour.
let paletteItems = [];
let paletteIndex = 0;

function paletteEntries(){
  const items = [
    { kind: 'View', label: 'Analyse a variant', desc: 'Open the query workspace',
      run: () => showView('workspace') },
    { kind: 'View', label: 'Your profile & plan', desc: 'Account, plan, API keys and compliance log',
      run: () => showView('profile') },
  ];
  if (currentUser && currentUser.user && currentUser.user.role === 'admin'){
    items.push({ kind: 'View', label: 'Admin dashboard',
      desc: 'Accounts, upgrade queue and query volume', run: () => showView('admin') });
  }
  items.push({ kind: 'View', label: 'Landing page', desc: 'What this is, and what it costs',
    run: () => showView('landing') });

  EXAMPLES.forEach(({ label, q }) => {
    items.push({ kind: 'Example', label: q, desc: label + ' — run the full pipeline',
      run: () => {
        // Same gate as the landing-page examples: a signed-out visitor is
        // asked to sign in with the query held, rather than dropped into a
        // workspace whose first request would come back 401.
        if (!hasSession()){
          pendingQuery = q;
          openAuth('signin');
          return;
        }
        document.getElementById('query').value = q;
        showView('workspace');
        classifyVariant();
      } });
  });

  if (latestResult){
    items.push({ kind: 'Action', label: 'Copy report', desc: 'Copy the narrative report to the clipboard',
      run: () => copyReport() });
    items.push({ kind: 'Action', label: 'Download PDF', desc: 'Print-ready report with the sign-off block',
      run: () => downloadPDF() });
  }
  items.push({ kind: 'Action', label: 'API documentation', desc: 'Open the interactive OpenAPI reference',
    run: () => window.open('/docs', '_blank', 'noopener') });
  if (hasSession()){
    items.push({ kind: 'Account', label: 'Sign out', desc: 'End this session on this device',
      run: () => logout() });
  }
  return items;
}

function renderPalette(filter){
  const list = document.getElementById('paletteList');
  if (!list) return;
  const needle = String(filter || '').trim().toLowerCase();
  paletteItems = paletteEntries().filter(it =>
    !needle || (it.kind + ' ' + it.label + ' ' + it.desc).toLowerCase().includes(needle));
  if (paletteIndex >= paletteItems.length) paletteIndex = 0;
  if (!paletteItems.length){
    list.innerHTML = '<div class="palette-empty">No command matches “'
      + escapeHtml(filter) + '”.</div>';
    return;
  }
  list.innerHTML = paletteItems.map((it, i) =>
    '<button type="button" class="palette-item'
      + (i === paletteIndex ? ' palette-active' : '') + '"'
      + ' onmouseenter="paletteHover(' + i + ')"'
      + ' onclick="paletteChoose(' + i + ')">'
      + '<span class="palette-kind">' + escapeHtml(it.kind) + '</span>'
      + '<span class="palette-label">' + escapeHtml(it.label) + '</span>'
      + '<span class="palette-desc">' + escapeHtml(it.desc) + '</span>'
      + '</button>').join('');
}

// Moving the cursor re-marks the existing rows rather than re-rendering them,
// so the row under the pointer cannot flicker as the markup is replaced.
function paletteHover(i){
  if (i === paletteIndex) return;
  paletteIndex = i;
  const list = document.getElementById('paletteList');
  if (!list) return;
  Array.prototype.forEach.call(list.children, (el, n) => {
    el.classList.toggle('palette-active', n === i);
  });
}

function paletteStep(delta){
  if (!paletteItems.length) return;
  paletteIndex = (paletteIndex + delta + paletteItems.length) % paletteItems.length;
  const input = document.getElementById('paletteInput');
  renderPalette(input ? input.value : '');
}

function paletteChoose(index){
  const item = paletteItems[index];
  if (!item) return;
  closePalette();
  item.run();
}

function openPalette(){
  const modal = document.getElementById('paletteModal');
  const input = document.getElementById('paletteInput');
  if (!modal || !input) return;
  paletteIndex = 0;
  input.value = '';
  renderPalette('');
  modal.classList.remove('hidden');
  setTimeout(() => input.focus(), 30);
}

function closePalette(){
  const modal = document.getElementById('paletteModal');
  if (modal) modal.classList.add('hidden');
}

(function initPalette(){
  const modal = document.getElementById('paletteModal');
  const input = document.getElementById('paletteInput');
  if (!modal || !input) return;
  input.addEventListener('input', () => {
    paletteIndex = 0;
    renderPalette(input.value);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown'){ e.preventDefault(); paletteStep(1); }
    else if (e.key === 'ArrowUp'){ e.preventDefault(); paletteStep(-1); }
    else if (e.key === 'Enter'){ e.preventDefault(); paletteChoose(paletteIndex); }
    else if (e.key === 'Escape'){ e.preventDefault(); closePalette(); }
  });
  // Clicking the backdrop closes it, like every other dialog here.
  modal.addEventListener('click', e => { if (e.target === modal) closePalette(); });
})();

document.addEventListener('keydown', e => {
  if (!((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K'))) return;
  if (!hasSession()) return;
  const modal = document.getElementById('paletteModal');
  if (!modal) return;
  e.preventDefault();
  if (modal.classList.contains('hidden')) openPalette(); else closePalette();
});

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

// ---- Mobile navigation -------------------------------------------------
// Below 1040px the section links, and below 720px the account actions, move
// into a panel behind the hamburger. Both copies live in this document and keep
// their own onclick, so this is only ever a visibility toggle -- there is no
// second set of handlers to drift out of step with the header.
function toggleMobileNav(event){
  if (event) event.stopPropagation();
  const panel = document.getElementById('mobileNav');
  const btn = document.getElementById('navToggle');
  if (!panel || !btn) return;
  const open = panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !open);
  btn.setAttribute('aria-expanded', String(open));
}

function closeMobileNav(){
  const panel = document.getElementById('mobileNav');
  const btn = document.getElementById('navToggle');
  if (panel) panel.classList.add('hidden');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

// A click anywhere else closes the panel, the way a native menu behaves. The
// toggle itself is excluded by closest(), not by id: the hamburger is three
// spans, so the click target is usually a bar rather than the button.
document.addEventListener('click', e => {
  const panel = document.getElementById('mobileNav');
  if (!panel || panel.classList.contains('hidden')) return;
  if (panel.contains(e.target)) return;
  if (e.target.closest && e.target.closest('#navToggle')) return;
  closeMobileNav();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeMobileNav();
});

// ---- Scroll reveal -----------------------------------------------------
// Subtle fade-up as sections arrive. Applied from here rather than declared in
// the markup on purpose: if this never runs -- no IntersectionObserver, a failed
// script load, reduced motion -- the page is simply fully visible, which is the
// only acceptable failure mode for content.
//
// The classes are dropped once the entry has finished, because a lingering
// transition-delay on the element would also delay its own hover transitions.
function initReveal(){
  const selector = [
    '.hero-copy', '.hero-media', '.trust-item', '.section-head-block',
    '.vcard', '.pcard', '.split-copy', '.split-media', '.point', '.cap',
    '.sb-item', '.about-body', '.about-facts', '.price-card', '.example-btn',
    '.faq-item', '.cta-inner > *'
  ].join(', ');

  const targets = document.querySelectorAll(selector);
  if (!targets.length) return;
  if (!('IntersectionObserver' in window)) return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const io = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const el = entry.target;
      el.classList.add('reveal-visible');
      io.unobserve(el);
      window.setTimeout(() => {
        el.classList.remove('reveal', 'reveal-visible');
        el.style.transitionDelay = '';
      }, 900);
    });
  }, { rootMargin: '0px 0px -6% 0px', threshold: 0.04 });

  targets.forEach((el, i) => {
    // Skip anything a [data-when] group has hidden for this session. A
    // display:none element never intersects, so it would keep the reveal class
    // and stay at opacity 0 if it were later shown by signing in.
    if (el.closest('.hidden')) return;
    el.classList.add('reveal');
    // A short stagger within each visible group, capped at four steps so a long
    // list never turns into a queue.
    el.style.transitionDelay = (i % 4) * 55 + 'ms';
    io.observe(el);
  });
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
    setRailUsage(null);
    // A one-time API key must not survive the session that revealed it.
    clearApiKeyUi();
    // Nor should one account's reading list survive into the next one.
    historyItems = [];
    const panel = document.getElementById('historyPanel');
    if (panel) panel.classList.add('hidden');
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

  // The same entitlement, shown in the rail -- on the screen where a query
  // actually costs one. It used to be visible only inside the avatar menu, so
  // the limit arrived as a surprise at the moment it was hit.
  setRailUsage(me);

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

  // The reading list belongs to whoever is signed in, so it is loaded from here
  // rather than from the workspace view: this runs after sign-in and after every
  // completed query (refreshSession follows a run), which is exactly when the
  // list can have gained a row.
  loadHistory();
}

// Mirrors the account's plan and quota into the rail. Takes the /api/auth/me
// payload, or null for a signed-out visitor.
function setRailUsage(me){
  const plan = document.getElementById('railPlan');
  if (!plan) return;
  const quota = document.getElementById('railQuota');
  const meter = document.getElementById('railMeter');
  if (!me){
    plan.textContent = '—';
    quota.textContent = 'Sign in to run a query';
    meter.className = 'quota-fill';
    meter.style.width = '0';
    return;
  }
  const used = me.usage.used_this_month;
  const limit = me.usage.monthly_limit;
  plan.textContent = me.plan.name;
  quota.textContent = (limit === null || limit === undefined)
    ? used + ' used · unlimited'
    : used + ' / ' + limit + ' this month';
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 100;
  meter.className = 'quota-fill' + (limit && pct >= 80 ? ' warn' : '');
  meter.style.width = pct + '%';
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
        email: document.getElementById('regEmail').value.trim(),
        password: document.getElementById('regPassword').value
      })
    });
    const session = await login.json().catch(() => ({}));
    if (!login.ok) throw new Error(describeAuthError(session, 'Registration failed.'));
    // The session cookie is already set by the response.
    document.getElementById('regPassword').value = '';
    await refreshSession();
    // Dismiss the modal only once the new session is established.
    closeAuth();
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
// Per-account 30-day series for the accounts table, keyed by user id. Kept
// beside the table's other state so a refresh of just the account list (after a
// plan change) can redraw the sparklines without re-fetching activity.
let accountActivity = {};

function renderPlanCards(plans){
  const el = document.getElementById('adminPlanCards');
  if (!el) return;
  el.innerHTML = plans.map(p => {
    const price = fmtPrice(p.price_cents);
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
    // Lets the drill-down mark which row it belongs to without re-rendering.
    tr.dataset.userId = u.id;
    // The activity map is keyed by user id; JSON object keys are strings even
    // when they were ints on the way out, so both spellings are tried.
    const activity = accountActivity[u.id] || accountActivity[String(u.id)];
    const spark = activity
      ? '<div class="acct-cell">' + sparkBars(activity.series)
        + '<span class="acct-total">' + activity.total + ' in 30d</span></div>'
      : '<span class="acct-total">&mdash;</span>';
    tr.innerHTML =
      '<td><div>' + escapeHtml(u.username) + '</div>'
      + '<div style="color:var(--ink-faint);font-size:11px;">' + escapeHtml(u.email) + '</div>'
      + '<div style="color:var(--ink-faint);font-size:10.5px;">' + escapeHtml(u.role) + '</div>'
      + '</td>'
      + '<td><select class="admin-select" onchange="adminChangePlan(' + u.id
      + ', this.value, this)">' + planOptions(u.plan_id) + '</select></td>'
      + '<td>' + u.used_this_month + '</td>'
      + '<td>' + spark + '</td>'
      + '<td><button class="admin-btn" onclick="adminToggleActive(' + u.id + ', '
      + u.is_active + ')">' + (u.is_active ? 'Deactivate' : 'Activate') + '</button></td>';
    body.appendChild(tr);
  });
}

// ---- Admin drill-down -------------------------------------------------
// What a person actually asked. The table above is counts; this is the log
// behind them, and reading it is itself an audited action server-side.
function renderAccountDetail(body, id){
  const host = document.getElementById('accountDetail');
  if (!host) return;
  const account = body.account || {};
  const history = body.history || [];
  const rows = history.length
    ? history.map(item => '<div class="hist-row">'
        + '<div class="hist-main">'
        + '<div class="hist-q">' + escapeHtml(item.query_text) + '</div>'
        + '<div class="hist-meta mono">'
        + escapeHtml([item.variant_id || 'no variant id', fmtWhen(item.created_at)]
            .join(' · '))
        + '</div></div>'
        + '<span class="hist-badge ' + histBadgeClass(item.classification) + '">'
        + escapeHtml(item.classification || 'Unknown') + '</span>'
        + '</div>').join('')
    : '<div class="hist-empty">This account has no recorded queries.</div>';
  host.innerHTML =
    '<div class="acct-head">'
    + '<div><div class="acct-name">' + escapeHtml(account.username || ('account ' + id))
    + '</div><div class="acct-sub">' + escapeHtml(account.email || '')
    + (account.plan ? ' &middot; ' + escapeHtml(account.plan) : '')
    + ' &middot; ' + (body.total || 0) + ' recorded queries</div></div>'
    + '<button class="acct-close" onclick="closeAccount()" aria-label="Close">&times;</button>'
    + '</div>'
    + '<div class="acct-section">Last 30 days</div>'
    + '<div class="chart acct-chart">' + chartBars(body.daily || [], 66) + '</div>'
    + '<div class="acct-section">Queries</div>'
    + '<div class="history-list">' + rows + '</div>';
}

async function openAccount(id){
  const host = document.getElementById('accountDetail');
  if (!host) return;
  // Mark the row this panel belongs to, so the table and the detail read as one
  // thing rather than a panel that appeared from nowhere.
  document.querySelectorAll('#adminUserBody tr').forEach(tr => {
    tr.classList.toggle('row-open', tr.dataset.userId === String(id));
  });
  host.classList.remove('hidden');
  host.innerHTML = '<div class="acct-body">Loading this account\'s queries&hellip;</div>';
  const res = await authFetch('/api/admin/accounts/' + id + '/history?limit=50');
  if (!res.ok){
    // 403 is the expected shape when a non-admin somehow got here; either way
    // the panel says so rather than sitting empty.
    host.innerHTML = '<div class="acct-body">Could not load this account\'s queries ('
      + res.status + ').</div>';
    return;
  }
  renderAccountDetail(await res.json(), id);
}

function closeAccount(){
  const host = document.getElementById('accountDetail');
  if (!host) return;
  document.querySelectorAll('#adminUserBody tr.row-open')
    .forEach(tr => tr.classList.remove('row-open'));
  host.classList.add('hidden');
  host.innerHTML = '';
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
    // Entitlement-gated surfaces. Each renders its own explanatory empty state,
    // so a Free account sees what the tier buys rather than a blank card.
    renderApiAccess(me);
    renderAuditAccess(me);
    // Deliberately not awaited: the profile is already usable, and the key list
    // is one small query that should not hold the page open.
    loadApiKeys().catch(() => {});
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
        : ' · ' + escapeHtml(fmtPrice(plan.price_cents)) + ' / month')
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
    : fmtPrice(p.price_cents) + ' / month';
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

  // Where the account currently sits in the catalogue, so the cards below can
  // tell "upgrade" from "downgrade". Plans are ranked by price, with custom
  // pricing (no price at all) treated as the top of the range.
  const rank = p => (p.price_cents === null || p.price_cents === undefined)
    ? Number.POSITIVE_INFINITY : p.price_cents;
  const onCatalogue = adminPlans.find(p => p.id === me.plan.id);
  const currentRank = onCatalogue ? rank(onCatalogue) : null;
  const isAdmin = me.user.role === 'admin';

  const cards = adminPlans.map(p => {
    const current = p.id === me.plan.id;
    // Only ever offer a step up. A cheaper tier is a downgrade, and an upgrade
    // request is the wrong instrument for it: nobody asks an administrator for
    // permission to spend less. An administrator never asks at all -- the
    // dashboard moves accounts between plans directly, including their own.
    let action = '';
    if (!current && !isAdmin && currentRank !== null && rank(p) > currentRank){
      action = '<button class="btn-primary" onclick="requestUpgrade('
        + p.id + ')">Request upgrade</button>';
    } else if (!current && !isAdmin && currentRank !== null){
      action = '<p class="field-hint">Downgrades are arranged with an administrator.</p>';
    }
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
      + action
      + '</div>';
  }).join('');

  const footnote = isAdmin
    ? 'You are an administrator, so accounts move between plans from the admin '
      + 'dashboard rather than by request. No payment is taken here.'
    : 'Upgrade requests go to an administrator, who approves or declines them. '
      + 'No payment is taken here.';
  box.innerHTML += '<div class="plan-compare">' + cards + '</div>'
    + '<p class="field-hint">' + footnote
    + (isAdmin
       ? ' <button class="inline-link" onclick="showView(\'admin\')">Open the '
         + 'admin dashboard</button>' : '')
    + '</p>';
}

// ---- Profile: API keys ---------------------------------------------------
// The key endpoints have existed since the API layer was built, but minting a key
// meant calling them by hand -- so the Professional tier's headline feature was
// invisible in the product that sells it. These functions are the UI for it.

// ---- Money -----------------------------------------------------------------
// Plans are priced in whole US cents in the database (the pricing table in the
// report is denominated in USD), but the product is sold to Sri Lankan clinics,
// so every price is rendered in rupees at one indicative rate and rounded to a
// whole hundred so a converted price never reads like a currency conversion.
const LKR_PER_USD = 300;

function fmtPrice(cents){
  if (cents === null || cents === undefined) return 'Custom';
  const rupees = Math.round((cents / 100) * LKR_PER_USD / 100) * 100;
  return 'Rs. ' + rupees.toLocaleString('en-LK');
}

function fmtDate(iso){
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString();
}

function renderApiAccess(me){
  const entitled = !!(me && me.entitlements && me.entitlements.api_access);
  const hint = document.getElementById('apiKeysHint');
  const form = document.getElementById('apiKeyForm');
  if (form) form.classList.toggle('hidden', !entitled);
  if (hint){
    hint.textContent = entitled
      ? 'A key lets a script call the API as your account, spending your monthly '
        + 'query allowance. Scope is the account, so revoke one the moment a '
        + 'machine stops needing it.'
      : 'API access is included in the Professional and Enterprise plans. Request '
        + 'an upgrade below to enable it. Keys created on a higher tier stay '
        + 'listed here and can still be revoked.';
  }
}

async function loadApiKeys(){
  const box = document.getElementById('apiKeysBox');
  if (!box) return;
  const res = await authFetch('/api/keys');
  if (!res.ok){
    box.innerHTML = '<div class="keys-empty">Could not load your API keys.</div>';
    return;
  }
  const body = await res.json().catch(() => ({ keys: [] }));
  const keys = body.keys || [];
  if (!keys.length){
    box.innerHTML = '<div class="keys-empty">No keys yet.</div>';
    return;
  }
  box.innerHTML = keys.map(k =>
    '<div class="key-row">'
    + '<div><div class="key-name">' + escapeHtml(k.name) + '</div>'
    + '<div class="key-meta mono">' + escapeHtml(k.prefix) + '… · created '
    + escapeHtml(fmtDate(k.created_at))
    + (k.last_used_at
        ? ' · last used ' + escapeHtml(fmtDate(k.last_used_at))
        : ' · never used')
    + '</div></div>'
    + '<div class="key-actions">'
    + '<button class="key-revoke" onclick="revokeApiKey(' + k.id + ')">Revoke</button>'
    + '</div></div>').join('');
}

function revealApiKey(raw){
  const wrap = document.getElementById('apiKeyReveal');
  const code = document.getElementById('apiKeyValue');
  if (!wrap || !code) return;
  code.textContent = raw || '';
  wrap.classList.remove('hidden');
}

function copyApiKey(){
  const code = document.getElementById('apiKeyValue');
  const text = code ? code.textContent : '';
  if (!text) return;
  if (!navigator.clipboard || !navigator.clipboard.writeText){
    showToast('This browser does not allow copying from the page.', 'err');
    return;
  }
  navigator.clipboard.writeText(text)
    .then(() => showToast('Key copied to the clipboard.'))
    .catch(() => showToast('Could not copy the key.', 'err'));
}

async function createApiKey(){
  if (!currentUser || !currentUser.entitlements || !currentUser.entitlements.api_access){
    showToast('API access is not included in your plan.', 'err');
    return;
  }
  const input = document.getElementById('apiKeyName');
  const name = input ? input.value.trim() : '';
  if (!name){
    showToast('Give the key a name first.', 'err');
    if (input) input.focus();
    return;
  }

  const answer = await askConfirm({
    title: 'Create an API key?',
    message: '“' + name + '” will be able to call the API as your account, spending '
      + 'your monthly query allowance. The key is shown once and cannot be '
      + 'recovered, so store it somewhere safe.',
    confirmLabel: 'Create key',
  });
  if (!answer.confirmed) return;

  const res = await authFetch('/api/keys', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok){
    showToast(describeAuthError(body, 'Could not create the key.'), 'err');
    return;
  }
  if (input) input.value = '';
  revealApiKey(body.key);
  await loadApiKeys();
  showToast('API key created.');
}

async function revokeApiKey(id){
  const answer = await askConfirm({
    title: 'Revoke this API key?',
    message: 'Anything using this key stops working immediately. This cannot be '
      + 'undone — you would have to create a new key and update the client using it.',
    confirmLabel: 'Revoke key',
    danger: true,
  });
  if (!answer.confirmed) return;

  const res = await authFetch('/api/keys/' + id, { method: 'DELETE' });
  if (!res.ok){
    const body = await res.json().catch(() => ({}));
    showToast(describeAuthError(body, 'Could not revoke the key.'), 'err');
    return;
  }
  await loadApiKeys();
  showToast('API key revoked.');
}

// Clears anything secret-bearing out of the DOM when the session ends, so a
// one-time key cannot be left sitting on the page for the next person.
function clearApiKeyUi(){
  const reveal = document.getElementById('apiKeyReveal');
  if (reveal) reveal.classList.add('hidden');
  const code = document.getElementById('apiKeyValue');
  if (code) code.textContent = '';
  const box = document.getElementById('apiKeysBox');
  if (box) box.innerHTML = '';
  const name = document.getElementById('apiKeyName');
  if (name) name.value = '';
}

// ---- Profile: compliance log --------------------------------------------
// Gated by the same entitlement the endpoint enforces. The date range is applied
// server-side, so the file contains exactly the window that was asked for.

function renderAuditAccess(me){
  const entitled = !!(me && me.entitlements && me.entitlements.audit_export);
  const hint = document.getElementById('auditHint');
  const form = document.getElementById('auditForm');
  if (form) form.classList.toggle('hidden', !entitled);
  if (hint && !entitled){
    hint.textContent = 'Audit export is included in the Professional and '
      + 'Enterprise plans. Request an upgrade below to enable it.';
  } else if (hint){
    hint.textContent = 'Your own record: queries run, sign-offs recorded and '
      + 'authentication events, in a spreadsheet you can hand to an auditor.';
  }
}

async function downloadAudit(){
  const startEl = document.getElementById('auditStart');
  const endEl = document.getElementById('auditEnd');
  const start = startEl ? startEl.value : '';
  const end = endEl ? endEl.value : '';
  const params = new URLSearchParams();
  params.set('format', 'csv');
  if (start) params.set('start', start);
  if (end) params.set('end', end);

  try {
    const res = await authFetch('/api/export/audit?' + params.toString());
    if (!res.ok){
      const body = await res.json().catch(() => ({}));
      throw new Error(describeAuthError(body, 'Could not export your audit record.'));
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'variantlens-audit'
      + (start ? '-from-' + start : '')
      + (end ? '-to-' + end : '') + '.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('Compliance log downloaded.');
  } catch (err){
    showToast(err.message || 'Could not export your audit record.', 'err');
  }
}
// ---- Admin page ------------------------------------------------------

async function loadAdminPage(){
  // Five calls, two of which aggregate over the usage table. On a cold Neon
  // compute start this is the slowest screen in the app, so every region gets a
  // placeholder shaped like the content that is coming.
  const seq = ++loadSeq;
  startLoadBar();
  const settle = showSkeletons({
    statRow: '<div class="skeleton skeleton-card"></div>'.repeat(4),
    usageChart: '<div class="skeleton skeleton-block"></div>',
    upgradeQueue: '<div class="skeleton skeleton-row">' + SKELETON_LINES + '</div>'.repeat(2),
    adminPlanCards: '<div class="skeleton skeleton-card"></div>'.repeat(3),
    adminUserBody: '<tr><td colspan="5">'
      + '<div class="skeleton skeleton-line medium"></div></td></tr>'.repeat(4)
  });

  try {
    const [analyticsRes, queueRes, usersRes, plansRes, activityRes] = await Promise.all([
      authFetch('/api/admin/analytics'),
      authFetch('/api/admin/upgrade-requests'),
      authFetch('/api/admin/users?limit=100'),
      authFetch('/api/admin/plans'),
      authFetch('/api/admin/accounts/activity?days=30')
    ]);
    // A clinician reaching this view is expected, not an error to shout about.
    if (analyticsRes.status === 403 || queueRes.status === 403){
      adminMessage('Admin role required. This account cannot reach the dashboard.', true);
      clearAdminSkeletons();
      return;
    }
    const failed = [
      ['analytics', analyticsRes], ['upgrade requests', queueRes],
      ['accounts', usersRes], ['plans', plansRes], ['account activity', activityRes]
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
    const activity = (await activityRes.json()).accounts || {};

    // The user may have navigated away while these were in flight.
    if (seq !== loadSeq) return;
    await wait(settle);

    renderStats(analytics);
    renderChart(analytics.daily_usage);
    renderQueue(queue);
    adminPlans = plans;
    accountActivity = activity;
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

// Bar markup for a day series, shared by the dashboard chart, the account
// drill-down and the signed-in user's own activity strip, so all three read the
// same: gap-filled bars, a quiet day as a real dip rather than a missing column.
function chartBars(series, maxHeight){
  const peak = Math.max(...series.map(d => d.queries), 0);
  if (!peak){
    return '<div class="chart-empty">No queries recorded in this window yet.</div>';
  }
  // The bars live in their own row so the axis can sit underneath them.
  return '<div class="chart-plot">' + series.map(d =>
    '<div class="chart-col' + (d.queries ? '' : ' zero') + '" style="height:'
    + Math.max(2, Math.round((d.queries / peak) * maxHeight)) + 'px" title="'
    + d.date + ': ' + d.queries + ' queries"></div>').join('') + '</div>'
    + '<div class="chart-axis"><span>' + series[0].date + '</span><span>'
    + series[series.length - 1].date + '</span></div>';
}

// A compact bar strip for a table cell: same data, no axis, no hover titles
// competing with the row. A window with nothing in it returns no markup at all
// rather than a placeholder -- the cell already prints the total beside it, so
// "no activity" would be said twice.
function sparkBars(series){
  const peak = Math.max(...series.map(d => d.queries), 0);
  if (!peak) return '';
  return '<span class="acct-spark">' + series.map(d =>
    '<i class="' + (d.queries ? '' : 'zero') + '" style="height:'
    + Math.max(2, Math.round((d.queries / peak) * 24)) + 'px"></i>').join('')
    + '</span>';
}

function renderChart(series){
  const el = document.getElementById('usageChart');
  el.innerHTML = chartBars(series, 118);
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
    showToast('Sign-off recorded for ' + reviewer + '.');
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
    showToast('Report downloaded.');
  } catch (err) {
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Download PDF';
  }
}

function copyReport(){
  if (!latestResult) return;
  const text = latestResult.report_text || '';
  if (!text){
    showToast('There is no report text to copy yet.', 'err');
    return;
  }
  // The clipboard API is unavailable on insecure origins, and used to fail
  // silently here -- the button simply did nothing.
  if (!navigator.clipboard || !navigator.clipboard.writeText){
    showToast('This browser does not allow copying from the page.', 'err');
    return;
  }
  navigator.clipboard.writeText(text)
    .then(() => showToast('Report copied to the clipboard.'))
    .catch(() => showToast('Could not copy the report.', 'err'));
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

  // The stages of the real pipeline, in order. Telling the user which agent is
  // working is the difference between a spinner and a system that is visibly
  // doing six things.
  const msgs = [
    'Sanitising and parsing the query…',
    'Supervisor planning the retrieval strategy…',
    'Retrieving and ranking ClinVar evidence…',
    'Applying ACMG-style criteria…',
    'Verifier checking the call against its evidence…',
    'Writing the report…',
  ];
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

// ---- Agentic control plane --------------------------------------------
// What the Supervisor planned and what the Verifier concluded, rendered under the
// verdict they produced. The pipeline is not a fixed chain -- the plan is decided
// per query, and the Verifier can send a call back once -- so the report says how
// *this* run was steered rather than describing the pipeline in the abstract.
const CHECK_LABELS = {
  evidence_found: 'Evidence was retrieved for this variant',
  exact_match: 'An exact variant match was found',
  predictors_complete: 'Functional predictors were available',
  confidence_justified: 'The stated confidence is earned by the evidence',
};

function agRow(label, value){
  return '<div class="ag-row"><span class="ag-k">' + escapeHtml(label) + '</span>'
    + '<span class="ag-v">' + escapeHtml(value) + '</span></div>';
}

function renderAgentic(data){
  const section = document.getElementById('agenticSection');
  const grid = document.getElementById('agenticGrid');
  const summary = document.getElementById('agenticSummary');
  if (!section || !grid) return;

  const a = data.agentic || null;
  if (!a || !a.plan){
    section.style.display = 'none';
    const tl = document.getElementById('agenticTimeline');
    if (tl) tl.innerHTML = '';
    return;
  }
  section.style.display = 'block';
  renderTimeline(data);

  const plan = a.plan || {};
  const ver = a.verification || {};
  const rounds = a.revision_rounds || 0;
  const broad = plan.retrieval_mode === 'broad';
  const budget = plan.verification_budget;

  if (summary){
    summary.textContent = rounds
      ? 'Verifier challenged the first call; it was re-stated once.'
      : (ver.verdict === 'revise'
          ? 'Verifier asked for a revision the budget did not allow.'
          : 'Verifier accepted the first call.');
  }

  const planCard = '<div class="ag-card">'
    + '<div class="ag-head"><span class="ag-title">Supervisor &middot; plan</span>'
    + '<span class="ag-tag' + (broad ? ' warn' : '') + '">'
    + escapeHtml(plan.retrieval_mode) + ' retrieval</span></div>'
    + '<div class="ag-rows">'
    + agRow('Query type', plan.query_type)
    + agRow('Intent', plan.intent)
    + agRow('Revision budget', (budget === null || budget === undefined) ? '—'
        : budget + (budget === 1 ? ' round' : ' rounds'))
    + '</div>'
    + '<p class="ag-reason">' + escapeHtml(plan.reason || '') + '</p>'
    + '</div>';

  const checks = (ver.checks || []).map(c => {
    // A check that failed and one that was simply not required are different
    // findings, so they are not given the same mark.
    const cls = c.passed ? '' : (c.optional ? ' skip' : ' fail');
    const label = CHECK_LABELS[c.check] || String(c.check || '').replace(/_/g, ' ');
    const note = (!c.passed && c.optional)
      ? ' <span class="ag-k">(not required in ' + escapeHtml(plan.retrieval_mode || 'this')
        + ' mode)</span>'
      : '';
    return '<li class="ag-check' + cls + '">' + escapeHtml(label) + note + '</li>';
  }).join('');

  const issues = ver.issues || [];
  const issueBlock = issues.length
    ? '<div class="ag-issues"><b>The verifier raised:</b> '
      + issues.map(escapeHtml).join(' &middot; ') + '</div>'
    : '';

  // Did the second pass actually change the number? The confidence is the only
  // thing self-correction may move, so the report says explicitly what moved and
  // what did not.
  const clsStep = (data.pipeline_steps || [])
    .filter(s => s.agent === 'classification_agent').pop();
  const revised = !!(clsStep && clsStep.output && clsStep.output.confidence_revised);
  const note = rounds
    ? '<div class="ag-note">'
      + (revised
          ? '<b>Self-correction applied.</b> The first call asserted more confidence than '
            + 'the retrieved evidence earns, so it was re-stated conservatively. Final '
            + 'confidence ' + escapeHtml(fmtNum(data.confidence_numeric, 0)) + '/100 ('
            + escapeHtml(data.confidence || '') + '). The categorical classification was not '
            + 'changed — self-correction can only withdraw confidence, never add it.'
          : '<b>Checked twice.</b> The call was re-examined after the revision round and no '
            + 'further change was needed.')
      + '</div>'
    : '';

  grid.innerHTML = planCard
    + '<div class="ag-card">'
    + '<div class="ag-head"><span class="ag-title">Verifier &middot; verdict</span>'
    + '<span class="ag-tag' + (ver.verdict === 'accept' ? '' : ' warn') + '">'
    + escapeHtml(ver.verdict) + (rounds ? ' after revision' : '') + '</span></div>'
    + '<ul class="ag-checks">' + checks + '</ul>'
    + issueBlock + note
    + '</div>';
}

// ---- Run timeline --------------------------------------------------------
// The pipeline as it actually ran, in order -- not the six boxes the landing
// page draws. It is also where the self-correction loop becomes visible: when
// the verifier sent the call back, the classification row is marked "revised"
// and the verifier row carries the verdict that followed the second pass.
const AGENT_LABELS = {
  intake_agent: 'Intake',
  supervisor_agent: 'Supervisor',
  evidence_retrieval_agent: 'Evidence retrieval',
  classification_agent: 'Classification',
  verifier_agent: 'Verifier',
  report_agent: 'Reporting',
};

function timelineDetail(step, data){
  const a = data.agentic || {};
  const plan = a.plan || {};
  const ver = a.verification || {};
  const out = step.output || {};
  switch (step.agent){
    case 'supervisor_agent': {
      const budget = plan.verification_budget;
      return escapeHtml(plan.retrieval_mode || 'targeted') + ' retrieval &middot; revision budget '
        + (budget === null || budget === undefined ? '—' : escapeHtml(String(budget)))
        + (budget === 1 ? ' round' : ' rounds');
    }
    case 'evidence_retrieval_agent': {
      const m = data.ir_metadata || {};
      if (m.top_k_returned === undefined || m.top_k_returned === null){
        return 'evidence retrieved and ranked';
      }
      return escapeHtml(String(m.top_k_returned)) + ' of '
        + escapeHtml(String(m.candidates_retrieved ?? '?')) + ' candidates returned';
    }
    case 'classification_agent':
      return (out.confidence_revised
          ? 're-stated conservatively: confidence '
          : 'confidence ')
        + escapeHtml(fmtNum(data.confidence_numeric, 0)) + '/100 &middot; '
        + escapeHtml(data.classification || '');
    case 'verifier_agent': {
      const issues = (ver.issues || []).length;
      return 'verdict: ' + escapeHtml(ver.verdict || out.verdict || 'accept')
        + (issues
            ? ' &middot; ' + issues + ' issue' + (issues === 1 ? '' : 's') + ' raised'
            : ' &middot; no issues raised');
    }
    case 'report_agent':
      return 'report written &middot; sign-off block opened';
    default:
      return escapeHtml(step.stage || '');
  }
}

function renderTimeline(data){
  const host = document.getElementById('agenticTimeline');
  if (!host) return;
  const steps = data.pipeline_steps || [];
  if (!steps.length){ host.innerHTML = ''; return; }
  host.innerHTML = steps.map(step => {
    const out = step.output || {};
    // The verifier's revision round leaves its mark on the classification row,
    // so the second pass is stated rather than folded into an unmarked number.
    const revised = step.agent === 'classification_agent' && !!out.confidence_revised;
    const label = AGENT_LABELS[step.agent]
      || String(step.agent || '').replace(/_/g, ' ');
    return '<div class="tl-step' + (revised ? ' tl-revised' : '') + '">'
      + '<span class="tl-dot"></span>'
      + '<div class="tl-body">'
      + '<div class="tl-name">' + escapeHtml(label)
      + (revised ? '<span class="tl-badge">revised</span>' : '')
      + '</div>'
      + '<div class="tl-detail">' + timelineDetail(step, data) + '</div>'
      + '</div></div>';
  }).join('');
}

// ---- ACMG criterion chips ------------------------------------------------
// Plain-English meanings for the codes, so a clinician who does not hold the
// 2015 ACMG/AMP paper in their head can read the row. The suffix matters as much
// as the code: this project applies ACMG-*style* proxies computed from its own
// data, and the chip says which is which.
const ACMG_GLOSSARY = {
  PVS1: 'Very strong evidence of pathogenicity: a null variant (nonsense, frameshift or canonical splice site) in a gene where loss of function is a known disease mechanism.',
  PS1: 'Strong evidence: the same amino-acid change as a variant already established as pathogenic.',
  PS2: 'Strong evidence: confirmed de novo in a patient with the disease.',
  PS3: 'Strong evidence: well-established functional studies show a damaging effect.',
  PS4: 'Strong evidence: the variant is more common in affected individuals than in controls.',
  PM1: 'Moderate evidence: the variant sits in a mutational hot spot or a critical functional domain.',
  PM2: 'Moderate evidence: absent from, or extremely rare in, population databases.',
  PM3: 'Moderate evidence: found in trans with a pathogenic variant in a recessive disease.',
  PM4: 'Moderate evidence: a protein-length change that is not a null variant.',
  PM5: 'Moderate evidence: a different amino-acid change at a position already known to be pathogenic.',
  PM6: 'Moderate evidence: assumed de novo, but parentage was not confirmed.',
  PP1: 'Supporting evidence: co-segregation with the disease in multiple affected family members.',
  PP2: 'Supporting evidence: a missense change in a gene where missense changes are a common cause.',
  PP3: 'Supporting evidence: multiple computational predictors agree the variant is damaging.',
  PP4: 'Supporting evidence: the patient’s phenotype is highly specific for a single-gene disorder.',
  PP5: 'Supporting evidence: a reputable source reports the variant as pathogenic. Supporting only, never a substitute for the underlying evidence.',
  BA1: 'Stand-alone benign evidence: allele frequency above the disease-specific threshold.',
  BS1: 'Strong benign evidence: allele frequency greater than expected for the disorder.',
  BS2: 'Strong benign evidence: seen in a healthy adult who would be expected to be affected.',
  BS3: 'Strong benign evidence: well-established functional studies show no damaging effect.',
  BS4: 'Strong benign evidence: no segregation with the disease in affected family members.',
  BP1: 'Supporting benign evidence: a missense change in a gene where only truncating variants cause disease.',
  BP2: 'Supporting benign evidence: seen in trans with a pathogenic variant, or in cis with one.',
  BP3: 'Supporting benign evidence: an in-frame change in a repetitive region with no known function.',
  BP4: 'Supporting benign evidence: multiple computational predictors agree the variant has no impact.',
  BP5: 'Supporting benign evidence: the variant is in a case that has another molecular explanation for the disease.',
  BP6: 'Supporting benign evidence: a reputable source reports the variant as benign.',
  BP7: 'Supporting benign evidence: a synonymous change with no predicted effect on splicing.',
};

const CRIT_CODE_RE = /^\s*(PVS1|PS[1-4]|PM[1-6]|PP[1-5]|BA1|BS[1-4]|BP[1-7])(_(?:equivalent|strong|moderate|supporting))?\b/i;

function critChip(raw){
  const text = String(raw);
  const m = text.match(CRIT_CODE_RE);
  const code = m ? m[1].toUpperCase() : null;
  const modifier = m && m[2] ? m[2].replace(/^_/, '') : null;
  const rest = m ? text.slice(m[0].length).replace(/^\s*[:\-]?\s*/, '') : text;
  const isProxy = modifier === 'equivalent';
  const meaning = code ? (ACMG_GLOSSARY[code] || 'An ACMG/AMP criterion code.') : null;

  const flags = [];
  if (isProxy) flags.push('proxy');
  if (!code) flags.push('unknown');

  const pop = meaning
    ? '<span class="chip-pop">'
      + '<span class="chip-pop-h">' + escapeHtml(code)
      + (modifier ? ' &middot; ' + escapeHtml(modifier) : '') + '</span>'
      + '<span class="chip-pop-p">' + escapeHtml(meaning) + '</span>'
      + (isProxy
          ? '<span class="chip-pop-p chip-pop-flag">Applied here as an ACMG-style '
            + 'proxy computed from this project’s own data, not as a curator’s '
            + 'assertion.</span>'
          : '')
      + '</span>'
    : '';

  return '<span class="crit-chip' + (flags.length ? ' ' + flags.join(' ') : '')
    + '" tabindex="0">'
    + (code ? '<b>' + escapeHtml(code) + '</b>' : escapeHtml(text.split(' (')[0]))
    + (rest ? ' <span class="chip-detail">' + escapeHtml(rest) + '</span>' : '')
    + pop
    + '</span>';
}

// ---- Confidence breakdown ------------------------------------------------
// A bare bar says "72". This says 72 out of what: the gauge repeats the headline
// number, the bars show the inputs the score was computed from, and the tags
// carry the two judgement calls (submitter consistency, predictor agreement)
// that no single number can express.
const CONSISTENCY_LABELS = {
  submitter_agreement: 'Submitters agree',
  conflicting_submitter_evidence: 'Submitters disagree',
};

const PREDICTOR_LABELS = {
  sift_deleterious_polyphen_damaging: 'SIFT + PolyPhen: damaging',
  sift_tolerated_polyphen_benign: 'SIFT + PolyPhen: benign',
  mixed_predictors: 'Predictors disagree',
  not_available: 'Predictors unavailable',
};

// What each predictor outcome is worth, mirroring the scorer. The API returns
// the verdict as a word and does not repeat its point value, so the bar needs
// the table here to draw the same number the score was built from.
const PREDICTOR_POINTS = {
  sift_deleterious_polyphen_damaging: 20,
  sift_tolerated_polyphen_benign: 15,
  mixed_predictors: 5,
  not_available: 0,
};

function cbBar(label, value, max){
  const pct = max ? Math.max(0, Math.min(100, Math.round((value / max) * 100))) : 0;
  return '<div class="cb-comp">'
    + '<div class="cb-comp-head">'
    + '<span class="cb-comp-name">' + escapeHtml(label) + '</span>'
    + '<span class="cb-comp-val">' + escapeHtml(String(value)) + ' / '
    + escapeHtml(String(max)) + '</span></div>'
    + '<div class="cb-bar"><div class="cb-bar-fill" style="width:' + pct + '%"></div></div>'
    + '</div>';
}

function renderConfidence(data){
  const host = document.getElementById('confBreakdown');
  if (!host) return;
  const comps = data.confidence_components || null;
  if (!comps){
    // A cached result written before this field existed carries no breakdown.
    // Saying nothing is better than drawing an empty widget.
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }

  const pct = Math.max(0, Math.min(100, Number(data.confidence_numeric) || 0));
  const colors = classColors(data.classification);
  // Circumference of the r=26 ring the gauge is drawn on.
  const circumference = 2 * Math.PI * 26;
  const offset = circumference * (1 - pct / 100);

  const tags = [];
  if (comps.clinvar_consistency){
    const key = String(comps.clinvar_consistency);
    const warn = key.indexOf('conflict') !== -1;
    tags.push('<span class="cb-tag' + (warn ? ' warn' : '') + '">'
      + escapeHtml(CONSISTENCY_LABELS[key] || key.replace(/_/g, ' ')) + '</span>');
  }
  const predictorKey = comps.predictor_agreement ? String(comps.predictor_agreement) : '';
  if (predictorKey){
    const weak = predictorKey === 'mixed_predictors' || predictorKey === 'not_available';
    tags.push('<span class="cb-tag' + (weak ? ' warn' : '') + '">'
      + escapeHtml(PREDICTOR_LABELS[predictorKey] || predictorKey.replace(/_/g, ' ')) + '</span>');
  }
  // The scorer docks points for every predictor column that was empty. That
  // subtraction is the difference between a low score and a bad variant, so it
  // is stated rather than left for the reader to infer from a small bar.
  const penalty = Number(comps.unavailable_evidence_penalty) || 0;
  if (penalty > 0){
    tags.push('<span class="cb-tag warn">Missing predictor data &minus;'
      + escapeHtml(String(penalty)) + ' points</span>');
  }

  const ver = (data.agentic && data.agentic.verification) || null;
  const justified = ver
    ? (ver.checks || []).find(c => c.check === 'confidence_justified')
    : null;
  // The bars describe the scorer's raw inputs (pre-cap). When the Verifier
  // lowered the number, the headline and the bars genuinely disagree — the
  // cap is the explanation, so it is stated with both numbers rather than
  // leaving full bars next to a low score unexplained.
  const revised = !!data.confidence_revised;
  const before = (data.confidence_before_revision !== null
    && data.confidence_before_revision !== undefined)
    ? fmtNum(data.confidence_before_revision, 0) : null;
  const capNote = (revised && before !== null)
    ? 'The bars show the evidence as scored (' + before + '/100); the verifier ' +
      'lowered the headline to ' + fmtNum(pct, 0) + '/100 because that confidence ' +
      'was not earned by an exact match or reviewed consensus.'
    : (revised
      ? 'The bars show the evidence as scored; the verifier lowered the headline ' +
        'to ' + fmtNum(pct, 0) + '/100 because that confidence was not earned by ' +
        'an exact match or reviewed consensus.'
      : '');
  const note = justified && !justified.passed
    ? 'The verifier did not accept this confidence as earned by the evidence — '
      + 'see the control plane below for what it raised.'
    : (capNote || (ver
        ? 'The verifier checked that this confidence is earned by the evidence it '
          + 'rests on. Confidence reflects evidence strength, not medical certainty.'
        : 'Confidence reflects evidence strength, not medical certainty.'));

  host.classList.remove('hidden');
  host.innerHTML = '<div class="cb-grid">'
    + '<div class="cb-fig">'
    + '<svg class="cb-svg" viewBox="0 0 64 64" width="76" height="76" aria-hidden="true">'
    + '<circle class="cb-ring" cx="32" cy="32" r="26"></circle>'
    + '<circle class="cb-arc" cx="32" cy="32" r="26" style="stroke:' + colors.fill
    + ';stroke-dasharray:' + circumference.toFixed(1)
    + ';stroke-dashoffset:' + offset.toFixed(1) + '"></circle>'
    + '</svg>'
    + '<div class="cb-fig-value">' + escapeHtml(fmtNum(pct, 0)) + '</div>'
    + '</div>'
    + '<div class="cb-comps">'
    + (comps.evidence_strength === undefined ? ''
        : cbBar('Evidence strength', comps.evidence_strength, 40))
    + (comps.supporting_records === undefined ? ''
        : cbBar('Supporting records', comps.supporting_records, 20))
    + (!predictorKey ? ''
        : cbBar('Predictor agreement', PREDICTOR_POINTS[predictorKey] || 0, 20))
    + (tags.length ? '<div class="cb-tags">' + tags.join('') + '</div>' : '')
    + '</div></div>'
    + '<p class="cb-note">' + note + '</p>';
}

// ---- Query history -------------------------------------------------------
// The account's own runs. Reopening a row re-runs the query rather than
// restoring a cached report: the stored row carries the question, not the
// answer, so what comes back is computed against current evidence instead of
// replayed from a copy that may have been overtaken by a ClinVar update.
let historyItems = [];

// "Today 14:03" beats a bare date for anything recent, which is most of what
// this list holds.
function fmtWhen(iso){
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const startOfDay = value => new Date(value.getFullYear(), value.getMonth(), value.getDate());
  const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
  const time = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  if (days <= 0) return 'Today ' + time;
  if (days === 1) return 'Yesterday ' + time;
  if (days < 7) return days + ' days ago';
  return d.toLocaleDateString();
}

function histBadgeClass(classification){
  const value = String(classification || '').toLowerCase();
  if (value.includes('pathogenic')) return 'path';
  if (value.includes('benign')) return 'benign';
  if (value.includes('uncertain') || value.includes('vus')) return 'vus';
  return '';
}

async function loadHistory(){
  const panel = document.getElementById('historyPanel');
  if (!panel) return;
  if (!hasSession()){
    panel.classList.add('hidden');
    return;
  }
  const res = await authFetch('/api/history?limit=12');
  if (!res.ok){
    // History is a convenience, never the point of the page: if it cannot be
    // read, say nothing rather than putting an error above the report.
    panel.classList.add('hidden');
    return;
  }
  renderHistory(await res.json());
}

function renderHistory(body){
  const panel = document.getElementById('historyPanel');
  const list = document.getElementById('historyList');
  const spark = document.getElementById('historySpark');
  const count = document.getElementById('historyCount');
  if (!panel || !list) return;

  historyItems = body.history || [];
  if (!historyItems.length){
    panel.classList.add('hidden');
    return;
  }

  if (count){
    const total = body.total || historyItems.length;
    count.textContent = total > historyItems.length
      ? 'last ' + historyItems.length + ' of ' + total
      : total + (total === 1 ? ' query' : ' queries');
  }

  if (spark){
    const series = body.daily || [];
    const peak = series.length ? Math.max(...series.map(d => d.queries), 0) : 0;
    spark.innerHTML = peak
      ? series.map(d => '<div class="spark-col' + (d.queries ? '' : ' zero')
          + '" style="height:' + Math.max(2, Math.round((d.queries / peak) * 44))
          + 'px" title="' + d.date + ': ' + d.queries + ' queries"></div>').join('')
      : '';
  }

  list.innerHTML = historyItems.map((item, index) => {
    const confidence = (item.confidence_numeric === null
      || item.confidence_numeric === undefined)
      ? '—' : String(Math.round(item.confidence_numeric));
    const meta = [
      item.gene || 'unknown gene',
      item.variant_id || 'no variant id',
      fmtWhen(item.created_at),
    ].join(' · ');
    return '<div class="hist-row">'
      + '<div class="hist-main">'
      + '<div class="hist-q">' + escapeHtml(item.query_text) + '</div>'
      + '<div class="hist-meta mono">' + escapeHtml(meta) + '</div>'
      + '</div>'
      + '<span class="hist-badge ' + histBadgeClass(item.classification) + '">'
      + escapeHtml(item.classification || 'Unknown') + '</span>'
      + '<span class="hist-conf" title="confidence: '
      + escapeHtml(item.confidence || 'none') + '">' + escapeHtml(confidence) + '</span>'
      + '<button class="hist-open" onclick="rerunHistory(' + index + ')">Re-run</button>'
      + '</div>';
  }).join('');

  panel.classList.remove('hidden');
}

function rerunHistory(index){
  const item = historyItems[index];
  if (!item) return;
  document.getElementById('query').value = item.query_text;
  showView('workspace');
  classifyVariant();
}

function renderResult(data){
  document.getElementById('results').style.display = 'block';
  renderAgentic(data);
  renderConfidence(data);

  document.getElementById('repVariantId').textContent = data.variant_id || '—';
  document.getElementById('repTimestamp').textContent = new Date().toLocaleString();
  // The stamp line doubles as the run's at-a-glance summary: which engine
  // reasoned, how the supervisor chose to retrieve, and whether the verifier
  // sent the call back. The detail is in the control-plane panel below.
  const agentics = data.agentic || {};
  document.getElementById('repMode').textContent =
    'Reasoning: ' + (data.reasoning_mode === 'llm' ? 'LLM-assisted' : 'Rule-based')
    + (agentics.plan ? ' · ' + agentics.plan.retrieval_mode + ' retrieval' : '')
    + (agentics.revision_rounds ? ' · self-corrected once' : '');

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
    // Each chip carries the code, this project's own reason for applying it, and
    // a plain-English reading of the criterion on hover or keyboard focus.
    acmgOnly.forEach(c => { critList.innerHTML += critChip(c); });
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

// ---- The pipeline, on the landing page ---------------------------------
// Six agents the visitor can pick apart instead of six paragraphs to skim. The
// copy here is deliberately the mechanism, not the marketing: what each agent
// takes in, what it hands on, and which of the two is allowed to send work back.
const FLOW_STEPS = [
  {
    name: 'Intake',
    role: 'Parses the request',
    does: 'Takes the free-text question and pulls the structured entities out of it — '
      + 'gene, HGVS expression, rsID, protein change. Anything it cannot read as a '
      + 'variant is reported as unparsed rather than guessed at, so a typo fails '
      + 'here instead of silently retrieving the wrong record.',
    inbound: 'the clinician\'s question',
    outbound: 'gene · variant · identifiers',
    status: 'Runs first, and rejects before any retrieval work is paid for.',
  },
  {
    name: 'Supervisor',
    role: 'Plans the run',
    does: 'Decides how this particular question should be answered: a targeted '
      + 'retrieval when a single variant is named, a broad one when the question is '
      + 'about a gene, and how much verification the result is worth. The plan is '
      + 'written into the audit trail before the work starts, so the reasoning can be '
      + 'read afterwards rather than inferred.',
    inbound: 'parsed entities',
    outbound: 'retrieval mode · verification budget',
    status: 'Why two runs of the same question can take different routes.',
  },
  {
    name: 'Evidence retrieval',
    role: 'Ranks the evidence',
    does: 'Scores the committed ClinVar index against the query with TF-IDF vectors '
      + 'and cosine similarity, then returns the top-k records with their scores. '
      + 'Lexical retrieval is a deliberate choice over a neural one: every ranking '
      + 'decision can be shown to a reviewer as arithmetic.',
    inbound: 'query terms · planned mode',
    outbound: 'top-k scored records',
    status: 'The same index for every run — no live external calls.',
  },
  {
    name: 'Classification',
    role: 'Applies the criteria',
    does: 'Maps the retrieved evidence onto ACMG-style criteria — population '
      + 'frequency, functional predictors, submitter agreement, consequence — and '
      + 'produces a call with a confidence score built from named components, so a '
      + 'disagreement about the answer can be traced to the evidence it rests on.',
    inbound: 'scored evidence',
    outbound: 'classification · confidence breakdown',
    status: 'Runs on rules alone, or LLM-assisted when the plan allows it.',
  },
  {
    name: 'Verifier',
    role: 'Challenges the call',
    does: 'Re-reads the call against the evidence that produced it and looks for the '
      + 'specific failure of claiming more certainty than the evidence supports. It '
      + 'gets one round, and the only direction it may move a call is towards '
      + 'caution — it can never talk the system into a bolder verdict.',
    inbound: 'call + its evidence',
    outbound: 'confirmed, or one revision',
    status: 'The loop back to Classification: bounded, and conservative-only.',
  },
  {
    name: 'Report',
    role: 'Writes it up',
    does: 'Assembles the final report: the verdict, the evidence table, the '
      + 'confidence components, the retrieval metadata, any caveats the pipeline '
      + 'raised, and the full encrypted message trail between agents — so the answer '
      + 'arrives with its working attached.',
    inbound: 'verified call',
    outbound: 'report · audit trail',
    status: 'What is downloadable as a PDF and what the signed-in console shows.',
  },
];

let flowIndex = 0;
let flowTimer = null;

function paintFlow(i){
  const step = FLOW_STEPS[i];
  const panel = document.getElementById('flowPanel');
  if (!step || !panel) return;
  flowIndex = i;
  document.querySelectorAll('#flowTrack .flow-node').forEach((el, n) => {
    const on = n === i;
    el.classList.toggle('flow-active', on);
    el.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  panel.innerHTML =
    '<div class="flow-panel-head">'
    + '<span class="flow-panel-name">' + escapeHtml(step.name) + '</span>'
    + '<span class="flow-panel-role">' + escapeHtml(step.role) + '</span>'
    + '</div>'
    + '<p class="flow-panel-body">' + escapeHtml(step.does) + '</p>'
    + '<div class="flow-io">'
    + '<span class="flow-chip"><b>In</b> ' + escapeHtml(step.inbound) + '</span>'
    + '<span class="flow-chip"><b>Out</b> ' + escapeHtml(step.outbound) + '</span>'
    + '</div>'
    + '<div class="flow-status">' + escapeHtml(step.status) + '</div>';
}

function renderFlow(){
  const track = document.getElementById('flowTrack');
  const panel = document.getElementById('flowPanel');
  if (!track) return;
  // The buttons claim role="tab" and point here, so the panel answers to it.
  if (panel) panel.setAttribute('role', 'tabpanel');
  track.innerHTML = FLOW_STEPS.map((step, i) =>
    (i ? '<span class="flow-arrow" aria-hidden="true">&rarr;</span>' : '')
    + '<button type="button" role="tab" class="flow-node' + (i === 0 ? ' flow-active' : '')
    + '" aria-selected="' + (i === 0 ? 'true' : 'false')
    + '" aria-controls="flowPanel" onclick="selectFlow(' + i + ')">'
    + '<div class="flow-num">' + String(i + 1).padStart(2, '0') + '</div>'
    + '<div class="flow-name">' + escapeHtml(step.name) + '</div>'
    + '<div class="flow-role">' + escapeHtml(step.role) + '</div>'
    + '</button>').join('');
  paintFlow(0);
}

function stopFlowAuto(){
  if (flowTimer){ clearInterval(flowTimer); flowTimer = null; }
}

// Selecting a step is the visitor taking over, so the idle cycle stops for good
// rather than fighting them for the panel a second later.
function selectFlow(i){
  stopFlowAuto();
  paintFlow(i);
}

function initFlow(){
  renderFlow();
  const still = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  // An auto-advancing panel is motion; the diagram has to work without it.
  if (still || !document.getElementById('flowTrack')) return;
  flowTimer = setInterval(() => {
    const landing = document.getElementById('viewLanding');
    // No point redrawing a section nobody is looking at.
    if (landing && landing.classList.contains('hidden')) return;
    paintFlow((flowIndex + 1) % FLOW_STEPS.length);
  }, 3600);
}

// Restore an existing session on load (if the stored token is still valid) and
// otherwise land on the marketing page with sign-in available in the header.
//
// The image map is applied first: it fills the [data-img] placeholders before
// the reveal observer measures anything, so a frame never animates in empty and
// then reflow when its picture arrives.
applyLandingImages();
initReveal();
initFlow();
refreshSession();
