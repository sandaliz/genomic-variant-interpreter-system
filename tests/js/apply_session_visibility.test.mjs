/**
 * Behavioural check for applySessionVisibility, run under node with a stub DOM.
 *
 * The Python suite can only assert on the *source* of app.js. This executes the
 * real function against the real markup, so it catches a data-when typo or an
 * element that was never marked -- the failure modes a static check misses.
 */
import fs from 'fs';

const html = fs.readFileSync('frontend/index.html', 'utf8');
const js = fs.readFileSync('frontend/app.js', 'utf8');

// Pull the function body out of app.js verbatim.
const m = js.match(/function applySessionVisibility\(me\)\{[\s\S]*?\n\}/);
if (!m) { console.error('FAIL: applySessionVisibility not found'); process.exit(1); }

// Every element the page gates, with its data-when value.
const gated = [];
for (const g of html.matchAll(/data-when="([^"]+)"/g)) gated.push(g[1]);

const ids = [...html.matchAll(/id="(footerAccountOut|footerAccountIn|footerAdminLink|freePlanNote|heroSignedInNote)"/g)]
  .map(x => x[1]);

// --- minimal stub DOM -------------------------------------------------------
class El {
  // Declaration order matters: _set must exist before classList closes over it.
  // A prototype-level Set here would be shared by every element and the toggles
  // would silently leak between them.
  _set = new Set();
  classList = {
    toggle: (c, on) => { on ? this._set.add(c) : this._set.delete(c); },
    add: c => this._set.add(c),
    remove: c => this._set.delete(c),
    contains: c => this._set.has(c),
  };
  constructor(name, when) {
    this.name = name;
    this.dataset = { when: when || '' };
    this.textContent = '';
  }
}

function buildDoc(withAdmin) {
  const byId = {};
  for (const id of ids) byId[id] = new El(id, '');
  const nodes = gated.map(w => new El('gated', w));
  return {
    byId,
    nodes,
    document: {
      getElementById: id => byId[id] || null,
      querySelectorAll: sel => (sel === '[data-when]' ? nodes : []),
    },
  };
}

function run(me) {
  const d = buildDoc(me && me.user.role === 'admin');
  const document = d.document, getElementById = document.getElementById,
        querySelectorAll = document.querySelectorAll;
  // eslint-disable-next-line no-new-func
  new Function('document', 'getElementById', 'querySelectorAll',
               m[0] + '\nreturn applySessionVisibility;')(document, getElementById, querySelectorAll)(me);
  return d;
}

let failures = 0;
function check(label, cond) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + label);
  if (!cond) failures++;
}

console.log(`gated elements found: ${gated.length}`);

const anon = run(null);
check('anonymous: every data-when="out" is visible',
  anon.nodes.filter(n => n.dataset.when === 'out').every(n => !n.classList.contains('hidden')));
check('anonymous: every data-when="in" is hidden',
  anon.nodes.filter(n => n.dataset.when === 'in').every(n => n.classList.contains('hidden')));
check('anonymous: footer shows the signed-out column',
  !anon.byId.footerAccountOut.classList.contains('hidden'));
check('anonymous: footer hides the signed-in column',
  anon.byId.footerAccountIn.classList.contains('hidden'));

const clin = run({ user: { role: 'clinician' }, plan: { name: 'Free' } });
check('signed-in: every data-when="in" is visible',
  clin.nodes.filter(n => n.dataset.when === 'in').every(n => !n.classList.contains('hidden')));
check('signed-in: no "Create a free account" / "Sign in" CTA left visible',
  clin.nodes.filter(n => n.dataset.when === 'out').every(n => n.classList.contains('hidden')));
check('signed-in: footer shows the signed-in column',
  !clin.byId.footerAccountIn.classList.contains('hidden'));
check('signed-in: footer hides the signed-out column',
  clin.byId.footerAccountOut.classList.contains('hidden'));
check('clinician: footer admin link is hidden',
  clin.byId.footerAdminLink.classList.contains('hidden'));

const admin = run({ user: { role: 'admin' }, plan: { name: 'Free' } });
check('admin: footer admin link is visible',
  !admin.byId.footerAdminLink.classList.contains('hidden'));

check('no data-when value outside {in,out}',
  gated.every(w => w === 'in' || w === 'out'));
check('both signed-in and signed-out variants exist',
  gated.includes('in') && gated.includes('out'));

console.log(failures === 0 ? '\nALL CHECKS PASSED' : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);