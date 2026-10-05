"""
Frontend contract tests.

These are static checks over frontend/index.html, app.css and app.js. They exist
because the views are switched by adding/removing a single `hidden` class, which is
exactly the kind of thing that breaks silently: the page still renders, it just
renders *everything at once*.

The first test here is the one that would have caught that.
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
CSS = (FRONTEND / "app.css").read_text(encoding="utf-8")
JS = (FRONTEND / "app.js").read_text(encoding="utf-8")

VIEWS = ["landing", "workspace", "profile", "admin"]

# Element each view's loader fills with a skeleton. The workspace already has a
# dedicated spinner for the query run; the other three were blank.
_LOAD_HOSTS = {
    "landing": "loadBar",
    "workspace": "loading",
    "profile": "pfPlanBox",
    "admin": "statRow",
}

# Loader function backing each data-heavy view.
_LOADERS = {
    "profile": "loadProfile",
    "admin": "loadAdminPage",
}


def _classes_of(tag: str) -> list:
    match = re.search(r'class="([^"]+)"', tag)
    return match.group(1).split() if match else []


@pytest.mark.parametrize("view", VIEWS)
def test_view_starts_hidden_except_landing(view):
    """
    Only the landing view may be visible on first paint.

    Every other view must ship with `hidden`, or all four render stacked on one
    page before any JavaScript runs.
    """
    match = re.search(rf'<section[^>]*id="view{view.capitalize()}"[^>]*>', HTML)
    assert match, f"view{view} is missing from index.html"
    classes = _classes_of(match.group(0))
    if view == "landing":
        assert "hidden" not in classes
    else:
        assert "hidden" in classes, f"view{view} must start hidden"


def test_hidden_class_is_a_real_utility():
    """
    `.hidden` must actually hide something.

    A regression here is invisible in the markup: the class is still on the
    element, it just stops doing anything. The rule needs to win over component
    rules that set `display` (.modal is flex, .app is grid), hence !important.
    """
    rules = re.findall(r"\.hidden\s*\{([^}]*)\}", CSS)
    assert rules, "no .hidden rule in app.css"
    assert any("display:none" in rule for rule in rules), \
        ".hidden exists but does not set display:none"
    assert any("!important" in rule for rule in rules), \
        ".hidden must be !important so component display rules cannot override it"


def test_every_hidden_element_is_covered_by_the_utility():
    """Any element carrying `hidden` is only hidden because of the one rule."""
    assert re.search(r"\.hidden\{display:none\s*!important;\}", CSS)


class TestAuthModal:
    def test_modal_is_hidden_by_default(self):
        match = re.search(r'<div[^>]*id="authModal"[^>]*>', HTML)
        assert match and "hidden" in _classes_of(match.group(0))

    def test_exactly_one_pane_is_visible(self):
        """Sign in shows first; register/forgot/reset wait until asked for."""
        panes = re.findall(r'<form[^>]*class="auth-pane([^"]*)"[^>]*id="(\w+)"', HTML)
        assert len(panes) == 4, [p for p in panes]
        visible = [pid for cls, pid in panes if "hidden" not in cls]
        assert visible == ["paneSignin"], visible

    def test_all_four_auth_paths_exist(self):
        for pane in ("paneSignin", "paneRegister", "paneForgot", "paneReset"):
            assert f'id="{pane}"' in HTML
        for handler in ("doLogin", "doRegister", "doForgotPassword", "doConfirmReset"):
            assert f"function {handler}" in JS


class TestScriptReferences:
    def test_every_getelementbyid_target_exists(self):
        used = set(re.findall(r"getElementById\('([^']+)'\)", JS))
        present = set(re.findall(r'id="([^"]+)"', HTML))
        assert not (used - present), sorted(used - present)

    def test_no_query_selector_targets_are_missing(self):
        for selector in re.findall(r"querySelector\('#([\w-]+)", JS):
            assert f'id="{selector}"' in HTML, selector

    def test_view_switcher_covers_every_view(self):
        assert "function showView(" in JS
        # showView builds ids as 'view' + Capitalised, so the views are listed in
        # the VIEWS array rather than appearing as literal strings.
        listed = re.search(r"const VIEWS = \[([^\]]+)\]", JS)
        assert listed, "no VIEWS array in app.js"
        for view in VIEWS:
            assert f"'{view}'" in listed.group(1), view
        for view in VIEWS:
            assert f'view{view.capitalize()}' in HTML


class TestNoStaleUi:
    """The rail-level account UI was replaced; none of it should come back."""

    @pytest.mark.parametrize("gone", [
        "sessionCard", "loginForm", "registerForm", "adminPanel",
        "sessionUser", "quotaFill", "showAuth(",
    ])
    def test_removed_elements_stay_removed(self, gone):
        assert gone not in HTML and gone not in JS

    def test_css_has_no_rules_for_removed_elements(self):
        # Comments may still mention them by name; only live rules matter.
        without_comments = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        for rule in (".admin-panel", ".session-card", ".auth-switch", ".auth-form"):
            assert rule not in without_comments, rule


class TestWorkspaceLayout:
    """
    The workspace must keep its original two-column shape.

    The rebuild once dropped the <aside class="rail"> element and kept only its
    contents. Nothing errored: `.rail` is where the panel background, right
    border, padding, sticky positioning and the mobile single-column layout all
    live, so losing it silently degraded the whole sidebar to unstyled stacked
    content. These tests assert the structure that styling depends on.
    """

    @property
    def workspace(self):
        return HTML[HTML.index('id="viewWorkspace"'):HTML.index('id="viewProfile"')]

    def test_rail_and_stage_are_both_present(self):
        assert '<aside class="rail">' in self.workspace
        assert '<main class="stage">' in self.workspace

    def test_rail_wraps_the_stage_as_siblings_in_app(self):
        """The grid needs both children; order matters for column placement."""
        assert re.search(
            r'<div class="app">.*<aside class="rail">.*</aside>\s*<main class="stage">',
            self.workspace, re.S), "rail must precede the stage inside .app"

    def test_query_controls_live_inside_the_rail(self):
        for control in ('id="query"', 'id="examples"', 'id="submitBtn"'):
            assert control in self.workspace
        rail = self.workspace[self.workspace.index('<aside class="rail">'):
                          self.workspace.index('</aside>')]
        for control in ('id="query"', 'id="examples"', 'id="submitBtn"'):
            assert control in rail, f"{control} is outside the rail"

    def test_report_area_lives_inside_the_stage(self):
        stage = self.workspace[self.workspace.index('<main class="stage">'):]
        for element in ('id="empty"', 'id="loading"', 'id="results"',
                        'id="signoffStatus"'):
            assert element in stage, f"{element} is outside the stage"

    def test_the_rail_has_css(self):
        """Without the rule the column renders as plain stacked content."""
        rules = re.findall(r"\.rail\s*\{([^}]*)\}", CSS)
        assert rules, "no .rail rule in app.css"
        assert "border-right" in rules[0] or "background" in rules[0]
        assert "position:sticky" in rules[0]

    def test_the_grid_still_has_two_columns(self):
        grid = re.search(r"\.app\s*\{([^}]*)\}", CSS)
        assert grid and "grid-template-columns" in grid.group(1)

    def test_only_one_example_container_exists(self):
        """Two containers would render every example twice."""
        assert HTML.count('id="examples"') == 1
        assert HTML.count('id="landingExamples"') == 1

    def test_the_script_is_loaded_exactly_once(self):
        assert HTML.count("<script") == 1


class TestServedPage:
    """
    The same checks, but against what the server actually serves.

    Reading the file proves the file is right; requesting it proves the route
    serves it. A mismatch there (wrong directory, missing mount) would leave the
    file-level tests passing while the browser got a 404.
    """

    @pytest.fixture
    def served(self, client):
        page = client.get("/")
        assert page.status_code == 200
        return {
            "html": page.text,
            "css": client.get("/static/app.css").text,
            "js": client.get("/static/app.js").text,
        }

    def test_assets_are_served(self, served):
        assert len(served["css"]) > 1000
        assert len(served["js"]) > 1000

    def test_served_css_has_the_hidden_utility(self, served):
        assert re.search(r"\.hidden\{display:none\s*!important;\}", served["css"])

    @pytest.mark.parametrize("view", VIEWS)
    def test_served_views_start_hidden_except_landing(self, served, view):
        tag = re.search(rf'<section[^>]*id="view{view.capitalize()}"[^>]*>',
                        served["html"])
        assert tag, view
        classes = _classes_of(tag.group(0))
        assert ("hidden" in classes) is (view != "landing"), (view, classes)

    def test_served_modal_and_menu_start_hidden(self, served):
        for element in ("authModal", "profileMenu"):
            tag = re.search(rf'<div[^>]*id="{element}"[^>]*>', served["html"])
            assert tag and "hidden" in _classes_of(tag.group(0)), element

    def test_served_js_defines_every_entry_point(self, served):
        for fn in ("showView", "openAuth", "closeAuth", "doLogin", "doRegister",
                   "doForgotPassword", "doConfirmReset", "loadProfile",
                   "loadAdminPage", "renderChart", "renderQueue", "decideUpgrade",
                   "saveProfile", "changeOwnPassword", "requestUpgrade", "logout"):
            assert f"function {fn}" in served["js"], fn


class TestDesignTokens:
    @pytest.mark.parametrize("token", ["--paper", "--panel", "--ink", "--ink-soft",
                                       "--line", "--line-strong", "--accent"])
    def test_token_survives(self, token):
        assert token + ":" in CSS

    def test_fonts_unchanged(self):
        assert "Manrope" in HTML and "JetBrains+Mono" in HTML


def _selectors(css: str) -> set:
    """Every class name declared in the stylesheet, ignoring @media blocks.

    Compound selectors such as `.pm-item.danger` and
    `.signoff-status.pending` contribute all of their class names, not just the
    first one.
    """
    code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    code = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", code, flags=re.S)
    code = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", code, flags=re.S)
    found = set()
    for match in re.finditer(r"([^{}]+)\{", code):
        found.update(re.findall(r"\.([A-Za-z][\w-]*)", match.group(1)))
    return found


class TestConfirmationDialog:
    """
    Consequential actions must go through the in-app dialog. window.prompt /
    window.confirm / window.alert cannot express a required justification, cannot
    be styled, and read as browser chrome rather than as part of the product.
    """

    def test_dialog_is_in_the_markup(self):
        assert 'id="confirmModal"' in HTML
        for part in ("confirmTitle", "confirmMsg", "confirmNote", "confirmOk",
                     "confirmCancel", "confirmNoteCount"):
            assert f'id="{part}"' in HTML, part

    def test_dialog_starts_hidden(self):
        tag = re.search(r'<div[^>]*id="confirmModal"[^>]*>', HTML)
        assert tag and "hidden" in _classes_of(tag.group(0))

    def test_no_browser_dialogs_remain(self):
        """Nothing may still call prompt/confirm/alert -- only a comment may."""
        for line in JS.splitlines():
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            for banned in ("window.prompt", "window.confirm", "alert("):
                assert banned not in stripped, stripped

    @pytest.mark.parametrize("fn", [
        "requestUpgrade",     # user asks for a plan
        "decideUpgrade",      # administrator approves / declines / contacts
        "adminChangePlan",    # administrator moves an account between plans
        "adminToggleActive",  # administrator blocks or restores an account
        "saveProfile",        # changing your own email address
        "changeOwnPassword",
        "saveReview",         # clinical sign-off
    ])
    def test_action_confirms_first(self, fn):
        body = re.search(rf"(?:async )?function {fn}\(", JS)
        assert body, fn
        # The confirmation must come before the request that changes anything.
        source = JS[body.start():body.start() + 3000]
        assert "askConfirm(" in source, f"{fn} does not confirm"
        assert "authFetch(" in source, fn
        assert source.index("askConfirm(") < source.index("authFetch("), \
            f"{fn} acts before confirming"

    def test_admin_decisions_are_described_not_bare_buttons(self):
        for status in ("contacted", "approved", "declined"):
            assert f"{status}:" in JS, status

    @staticmethod
    def _ask_confirm_source():
        start = JS.index("function askConfirm(")
        return JS[start:start + 4000]

    def test_the_confirm_button_is_reset_on_every_open(self):
        """
        Regression: a dialog that collects a required note disables Confirm until
        enough is typed. updateCounter() was the only thing that re-enabled it,
        and it ran only inside the `if (spec)` branch -- so a note-less dialog
        inherited the previous dialog's `disabled` and opened permanently dead.
        That locked the clinical sign-off button after any upgrade dialog.
        """
        body = self._ask_confirm_source()
        call = body.index("\n    updateCounter();")   # bare call, not the declaration
        assert call > body.index("} else {"), (
            "updateCounter() must run after both the note and no-note branches, "
            "otherwise a no-note dialog inherits a disabled Confirm button"
        )

    def test_closing_the_dialog_clears_the_disabled_state(self):
        """Backstop, so no dialog can ever hand the next one a dead button."""
        body = self._ask_confirm_source()
        finish = body.split("function finish(")[1].split("\n    }")[0]
        assert "okBtn.disabled = false" in finish
        # Declining is destructive and must be styled as such.
        assert "danger: true" in JS

    def test_plan_change_can_be_cancelled_without_persisting(self):
        """Cancelling a plan change must put the select back where it was."""
        source = re.search(r"async function adminChangePlan\(.*?\n\}\n", JS, re.S)
        assert source
        assert re.search(
            r"if \(!answer\.confirmed\)\{(?:[^{}]|\n)*?await loadAdminPanel\(\)",
            source.group(0))

    def test_busy_button_helper(self):
        assert "function setBusy(" in JS
        for label in ("Signing in", "Creating account", "Sending", "Updating"):
            assert label in JS, label


class TestUpgradeJustificationIsARealForm:
    """
    The prompt used to read "Tell the administrator why you need <plan>
    (optional)". The note is now required end to end.
    """

    def test_request_prompts_with_a_required_note(self):
        source = re.search(r"async function requestUpgrade\((.*?)\n\}", JS, re.S)
        assert source
        block = source.group(1)
        assert "required: true" in block
        assert "minLength: 10" in block
        assert "(optional)" not in block

    def test_admin_sees_the_justification_prominently(self):
        queue = re.search(r"function renderQueue\(body\)\{(.*?)\n\}", JS, re.S)
        assert queue
        assert "justification" in queue.group(1)
        # A legacy request with no note must say so, not render an empty block.
        assert "No justification was recorded" in queue.group(1)
        assert ".justification{" in CSS

    def test_api_requires_the_note(self):
        schema = (Path(__file__).resolve().parent.parent
                  / "auth" / "schemas.py").read_text(encoding="utf-8")
        block = re.search(r"class UpgradeRequestCreate\(BaseModel\):(.*?)\n\nclass",
                          schema, re.S)
        assert block, "UpgradeRequestCreate not found"
        # Check the declared fields, not the docstring (which explains the old
        # behaviour and necessarily mentions the old Optional type).
        fields = re.sub(r'""".*?"""', "", block.group(1), flags=re.S)
        assert "Optional" not in fields
        assert "min_length=10" in fields


class TestAdminPanelRefreshIsDefined:
    def test_load_admin_panel_exists(self):
        """
        It was called from four places but never defined, so every plan change and
        every activate/deactivate threw a ReferenceError.
        """
        assert re.search(r"async function loadAdminPanel\(\)", JS), \
            "loadAdminPanel is referenced but never defined"

    def test_every_referenced_function_is_defined(self):
        """Catch the whole class of missing-function bug, not just this one."""
        # Comments mention handler names in prose; only real code counts.
        code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        defined = set(re.findall(r"(?:async )?function (\w+)\s*\(", code))
        called = set(re.findall(r"\b(\w+)\s*\(", code))
        # Names that are DOM globals, language builtins or constructors.
        ignore = {
            "if", "for", "while", "switch", "catch", "return", "function",
            "Number", "String", "Boolean", "Array", "Object", "Date", "Math",
            "JSON", "parseInt", "parseFloat", "setTimeout", "clearTimeout",
            "setInterval", "clearInterval", "fetch", "isNaN", "encodeURIComponent",
            "decodeURIComponent", "require", "await", "typeof", "of", "in",
            "window", "document", "console", "Promise", "Map", "Set",
        }
        # Only check names that look like our own handlers.
        missing = sorted(
            n for n in called
            if n not in defined and n not in ignore
            and (n.startswith("admin") or n.startswith("load") or n.startswith("render")
                 or n.startswith("decide") or n.startswith("save") or n.startswith("do"))
        )
        assert not missing, f"called but never defined: {missing}"


class TestLoadingFeedback:
    """
    The admin dashboard waits on four API calls and the profile page on three,
    and Neon scales its compute to zero between requests. Without a loading
    signal these pages sit blank and then snap into place, which reads as a
    broken app rather than as work happening.
    """

    def test_skeleton_primitives_exist(self):
        for token in (".skeleton{", ".skeleton-line", ".skeleton-block",
                      ".skeleton-card", "@keyframes shimmer"):
            assert token in CSS, token

    def test_skeleton_respects_reduced_motion(self):
        blocks = re.findall(r"@media \(prefers-reduced-motion:reduce\)\s*\{[^}]*\}", CSS)
        assert blocks, "no reduced-motion query at all"
        assert any(".skeleton" in b for b in blocks), (
            "shimmer must have a reduced-motion escape hatch"
        )

    def test_progress_bar_is_in_the_markup(self):
        assert 'id="loadBar"' in HTML
        assert ".load-bar{" in CSS

    @pytest.mark.parametrize("view", ["profile", "admin"])
    def test_data_loaded_views_skeleton_their_regions(self, view):
        """
        The landing and workspace views have their own single-purpose loaders
        (a global bar and the query spinner). Profile and admin previously had
        nothing, which is what this covers.
        """
        block = re.search(rf'id="view{view.capitalize()}"(.*?)</section>', HTML, re.S)
        assert block, view
        for host in (_LOAD_HOSTS[view],):
            assert f'id="{host}"' in block.group(1), f"{view} missing #{host}"
        body = re.search(rf"async function {_LOADERS[view]}\(\)\{{(.*?)\n\}}",
                         JS, re.S)
        assert body and "skeleton" in body.group(1), view

    def test_slow_loaders_signal_the_user(self):
        for fn in ("loadAdminPage", "loadProfile"):
            body = re.search(rf"async function {fn}\(\)\{{(.*?)\n\}}", JS, re.S)
            assert body, fn
            assert "startLoadBar()" in body.group(1), fn
            assert "stopLoadBar()" in body.group(1), fn
            assert "skeleton" in body.group(1), fn

    def test_admin_dashboard_no_longer_fails_silently(self):
        """
        It used to `return` on a non-OK response, leaving a blank dashboard with
        no explanation of what went wrong.
        """
        body = re.search(r"async function loadAdminPage\(\)\{(.*?)\n\}", JS, re.S)
        assert body
        source = body.group(1)
        assert "Could not load the dashboard" in source
        # No bare early-return-on-error left behind.
        assert not re.search(r"if \(!.*\.ok \|\|.*\.ok\) return;", source)

    def test_stale_load_guard_exists(self):
        """A slow response must not paint over a view the user has left."""
        assert "loadSeq" in JS
        assert "seq !== loadSeq" in JS

    def test_background_refresh_does_not_flash_the_bar(self):
        """The quota refresh after a query must not show a progress bar."""
        assert "refreshSession({ silent: true })" in JS

    def test_profile_placeholder_restores_real_fields(self):
        """
        The profile form fields are static markup that must survive the load, so
        the skeleton is stashed and restored rather than replacing it outright.
        """
        for token in ("showSkeletonHost", "restoreSkeletonHost",
                      "dataset.stashed", "id=\"pfAccount\"", "id=\"pfPassword\""):
            assert token in JS or token in HTML, token


class TestFooter:
    FOOTER_TEXT = "StatFInity"

    def test_footer_is_styled(self):
        assert ".site-foot{" in CSS

    @pytest.mark.parametrize("view", VIEWS)
    def test_every_view_carries_the_footer(self, view):
        """Profile and admin previously had no footer at all."""
        block = re.search(rf'id="view{view.capitalize()}"(.*?)</section>', HTML, re.S)
        assert block, view
        assert self.FOOTER_TEXT in block.group(1), f"{view} is missing the footer"

    def test_footer_text_is_consistent(self):
        matches = re.findall(
            r"&copy; (\d{4}) StatFInity &middot; All rights reserved", HTML)
        assert len(matches) == len(VIEWS), f"expected {len(VIEWS)} footers, got {len(matches)}"
        assert len(set(matches)) == 1, "footer year is not consistent"

    def test_no_individual_names_in_the_footer(self):
        """Team attribution only -- the docs carry the contributor breakdown."""
        foot = re.findall(r'<p class="site-foot">(.*?)</p>', HTML)
        assert foot
        for block in foot:
            for name in ("Sanulya", "Nihindu", "Nipuni", "Sandali"):
                assert name not in block, name


class TestClientSideRouting:
    """
    The four views are one document switched by a `hidden` class, so without real
    URLs a refresh or a pasted link lands nowhere and the back button is inert.
    """

    def test_every_view_has_a_slug(self):
        assert re.search(r"const VIEW_SLUGS\s*=\s*\{", JS)
        for slug in ("/profile", "/admin-dashboard", "/analyse"):
            assert f"'{slug}'" in JS, slug

    def test_showing_a_view_updates_the_url(self):
        body = re.search(r"function showView\(name, opts\)\{(.*?)\n\}", JS, re.S)
        assert body
        assert "pushViewUrl(name)" in body.group(1)

    def test_back_button_does_not_loop(self):
        """popstate must not push another history entry."""
        assert re.search(
            r"addEventListener\('popstate'.*?fromHistory: true", JS, re.S)

    def test_admin_lands_on_the_dashboard(self):
        """An admin's home is the dashboard, not the query surface."""
        body = re.search(r"function homeViewFor\(me\)\{(.*?)\n\}", JS, re.S)
        assert body
        assert "'admin'" in body.group(1)
        assert "refreshSession" in JS

    def test_the_logo_goes_to_the_landing_page(self):
        """
        The logo used to double as a shortcut into the query surface, so two
        different destinations were both "home". Now that the header has a
        dedicated Analyse button, the logo means home for everyone -- including
        a signed-in clinician, who may want to read the landing page.
        """
        body = re.search(r"function goHome\(\)\{(.*?)\n\}", JS, re.S)
        assert body
        assert "showView('landing')" in body.group(1)
        # It must not route by role any more.
        assert "homeViewFor" not in body.group(1)

    def test_analyse_is_the_visual_primary_of_the_signed_in_header(self):
        """
        It was a ghost button: white on cream behind a faint border, 8px from the
        avatar. Legible, but it did not read as a control. Filled accent makes the
        header's main job obvious.
        """
        tag = re.search(r'<button[^>]*id="analyseNavBtn"[^>]*>', HTML)
        assert tag, "analyseNavBtn missing"
        assert "btn-nav" in _classes_of(tag.group(0))

    def test_the_avatar_is_visually_separated_from_navigation(self):
        """identity vs. destination -- a rule plus padding, not an 8px gap."""
        assert "header-actions-signed" in HTML
        css = CSS
        assert re.search(r"\.header-actions-signed\s+\.profile-wrap\s*\{[^}]*border-left", css), \
            "profile-wrap needs a separating rule"
        assert re.search(r"\.header-actions-signed\s+\.profile-wrap\s*\{[^}]*padding-left", css)

    def test_the_workspace_is_not_also_offered_in_the_profile_menu(self):
        """
        The avatar dropdown carried an 'Analyse a variant' item that duplicated
        the header button. One route to the workspace, not two that can drift.

        Scoped to the menu itself: the landing hero legitimately has its own
        signed-in 'Analyse a variant' CTA, and this test is about the dropdown.
        """
        start = HTML.index('id="profileMenu"')
        end = HTML.index("</header>", start)
        menu = HTML[start:end]
        assert "Analyse a variant" not in menu, \
            "duplicate Analyse entry point still present in the profile menu"
        # The menu must still reach the profile and admin views.
        assert "Profile &amp; plan" in menu
        assert "pmAdminItem" in menu

    def test_the_workspace_has_its_own_header_control(self):
        assert 'id="analyseNavBtn"' in HTML
        assert 'id="adminNavBtn"' in HTML
        assert 'id="avatarBtn"' in HTML

    def test_post_signin_redirect_happens_only_once(self):
        """
        refreshSession() also runs after every query to update the quota bar. If
        the "send them home" redirect were not guarded, a signed-in user reading
        the landing page would be yanked back into the tool by their next query.
        """
        assert "sessionBootstrapped" in JS
        body = re.search(r"async function refreshSession\(opts\)\{(.*?)\n\}", JS, re.S)
        assert body
        source = body.group(1)
        assert "!sessionBootstrapped" in source
        assert "sessionBootstrapped = true;" in source
        # And it must be re-armed on sign-out.
        out = re.search(r"function logout\(\)\{(.*?)\n\}", JS, re.S)
        assert out and "sessionBootstrapped = false;" in out.group(1)

    def test_the_query_surface_is_still_reachable_from_the_header(self):
        assert 'id="analyseNavBtn"' in HTML
        assert 'onclick="showView(\'workspace\')"' in HTML


class TestPlanBoxIsNeverEmpty:
    """
    "Your plan" rendered blank with no explanation. The card now always leads
    with the current tier from /api/auth/me, which does not depend on the
    catalogue request that was failing.
    """

    def test_current_plan_summary_is_rendered(self):
        assert "function currentPlanSummary(me)" in JS
        body = re.search(r"function renderPlanBox\(me\)\{(.*?)\n\}", JS, re.S)
        assert body
        assert "currentPlanSummary(me)" in body.group(1)

    def test_summary_reads_only_from_the_me_payload(self):
        """It must not depend on adminPlans, or it fails with the catalogue."""
        body = re.search(r"function currentPlanSummary\(me\)\{(.*?)\n\}", JS, re.S)
        assert body
        assert "adminPlans" not in body.group(1)

    def test_profile_errors_are_not_swallowed(self):
        """
        The catch block wrote its message into the plan box and then called
        clearProfileSkeletons(), which emptied that same box -- so a real failure
        rendered as a blank card.
        """
        body = re.search(r"async function loadProfile\(\)\{(.*?)\n\}", JS, re.S)
        assert body
        catch = re.search(r"\} catch \(err\)\{(.*?)\} finally \{", body.group(1), re.S)
        assert catch, "no catch block in loadProfile"
        block = catch.group(1)
        assert "console.error" in block
        assert block.index("clearProfileSkeletons()") < block.rindex("innerHTML"), \
            "the error message must be written last, or it gets cleared"

    def test_fields_are_restored_before_they_are_written_to(self):
        """
        The actual cause of the blank "Your plan" card.

        showSkeletonHost() replaces a host's innerHTML wholesale, which detaches
        every input inside it. Restoring in `finally` put the real markup back
        *after* loadProfile had already written to those inputs, so
        getElementById returned null and "Cannot set properties of null
        (setting 'value')" aborted the load before renderPlanBox() ever ran.
        """
        body = re.search(r"async function loadProfile\(\)\{(.*?)\n\}", JS, re.S)
        assert body
        source = body.group(1)
        first_write = source.index("setFieldValue(")
        assert "restoreSkeletonHost('pfAccount')" in source[:first_write], \
            "the account fields must be restored before setFieldValue writes to them"
        assert "restoreSkeletonHost('pfPassword')" in source[:first_write]

    def test_field_writes_tolerate_a_missing_element(self):
        """
        Defence in depth: a detached or renamed field must not be able to abort
        the whole profile load again.
        """
        assert "function setFieldValue(" in JS
        body = re.search(r"function setFieldValue\(id, value\)\{(.*?)\n\}", JS, re.S)
        assert body
        assert "if (!el)" in body.group(1)
        # loadProfile must not write .value directly any more.
        source = re.search(r"async function loadProfile\(\)\{(.*?)\n\}", JS, re.S).group(1)
        assert not re.search(r"getElementById\('pf\w+'\)\.value", source)


class TestElementIdsAreUnique:
    """
    Duplicate ids are invisible in the markup and fatal at runtime: the browser
    silently keeps the first, so getElementById() hands back the wrong element.

    This actually happened -- the landing page's "examples" section and the
    workspace's example-button container were both id="examples", which meant the
    workspace was about to append its buttons into the landing page.
    """

    def test_no_id_appears_twice(self):
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicates, f"duplicate element ids: {duplicates}"

    def test_navigation_targets_all_exist(self):
        """Every goToSection('x') must resolve to a real element."""
        targets = set(re.findall(r"goToSection\('([^']+)'\)", JS))
        targets |= set(re.findall(r'goToSection\(&#39;([^&]+)&#39;\)', HTML))
        targets |= set(re.findall(r"goToSection\('([^']+)'\)", HTML))
        ids = set(re.findall(r'\bid="([^"]+)"', HTML))
        assert targets, "no navigation targets found"
        missing = sorted(targets - ids)
        assert not missing, f"nav points at missing ids: {missing}"


class TestEveryUsedClassIsStyled:
    """
    A class referenced in the markup but absent from the stylesheet renders with
    browser-default styling. That is exactly what happened to `.auth-pane`: the
    old rail-level form rules were deleted during the view split while the modal
    panes kept using them, so the sign-in and register forms quietly went
    unstyled. The static checks all passed -- the page rendered, just wrong.
    """

    def _classes_used(self):
        used = set()
        for tag in re.findall(r"<[a-zA-Z][^>]*>", HTML):
            match = re.search(r'class="([^"]+)"', tag)
            if match:
                used.update(match.group(1).split())
        # Classes the script assigns at runtime.
        for group in re.findall(r"classList\.(?:add|toggle|remove)\(\s*'([^']+)'", JS):
            used.update(group.split())
        for group in re.findall(r"className\s*=\s*'([^']+)'", JS):
            used.update(group.split())
        return used

    def test_no_class_is_used_without_a_rule(self):
        styled = _selectors(CSS)
        unstyled = sorted(c for c in self._classes_used() if c not in styled)
        assert not unstyled, f"used but never styled: {unstyled}"

    def test_auth_panes_have_layout(self):
        """The sign-in / register / reset panes each need form rhythm."""
        assert ".auth-pane{" in CSS
        assert ".auth-pane .field-label{" in CSS
        # Every input in the app shares one definition, so a pane can never end
        # up with raw browser inputs the way it did before.
        assert 'input[type="password"]' in CSS
        assert "input:focus" in CSS


class TestPlanCatalogueIsReachableByUsers:
    """
    The profile page renders plan comparisons and upgrade buttons from a
    catalogue that used to be fetched from the admin-only endpoint, so a regular
    user saw neither.
    """

    def test_profile_loads_the_user_accessible_endpoint(self):
        assert "'/api/plans'" in JS

    def test_load_profile_fetches_it(self):
        body = re.search(r"async function loadProfile\(\)\{(.*?)\n\}", JS, re.S)
        assert body, "loadProfile() not found"
        assert "/api/plans" in body.group(1)

    def test_admin_dashboard_still_uses_its_own_endpoint(self):
        admin_block = re.search(r"async function loadAdminPage\(\)\{(.*?)\n\}", JS, re.S)
        assert admin_block and "/api/admin/plans" in admin_block.group(1)


class TestStylesheetIntegrity:
    def test_no_duplicate_top_level_selectors(self):
        """
        A selector defined twice means the later one silently wins. This is how
        a refined button rule can be quietly discarded by a stale copy of the
        old definition further up the file.
        """
        code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        code = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", code, flags=re.S)
        code = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", code, flags=re.S)
        seen = {}
        for match in re.finditer(r"([^{}]+)\{", code):
            selector = " ".join(match.group(1).split())
            if selector.startswith("@"):
                continue
            line = code[: match.start()].count("\n") + 1
            seen.setdefault(selector, []).append(line)
        dupes = {k: v for k, v in seen.items() if len(v) > 1}
        assert not dupes, f"duplicate selectors: {dupes}"

    def test_hidden_utility_is_global(self):
        """The one rule that stops the four views stacking on top of each other."""
        assert re.search(r"\.hidden\s*\{[^}]*display\s*:\s*none\s*!important", CSS)


class TestFrontendDeepLinks:
    """
    /profile and /admin-dashboard have to resolve to the single-page frontend,
    otherwise a refresh or a pasted link 404s. Only the known view slugs do --
    a typo must still fail loudly rather than silently returning HTML.
    """

    @pytest.mark.parametrize("slug", ["/profile", "/admin-dashboard", "/analyse"])
    def test_view_slugs_serve_the_app(self, client, slug):
        response = client.get(slug)
        assert response.status_code == 200, slug
        assert "VarientLens" in response.text
        assert "text/html" in response.headers["content-type"]

    @pytest.mark.parametrize("slug", ["/nonsense", "/does-not-exist", "/admin",
                                      "/queries"])
    def test_unknown_paths_are_still_404(self, client, slug):
        assert client.get(slug).status_code == 404, slug

    def test_api_paths_are_not_shadowed(self, client):
        """The catch-all must not swallow API routes."""
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/plans").status_code == 401

    def test_docs_still_work(self, client):
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200

    def test_server_and_client_agree_on_the_slugs(self):
        """A slug the server does not serve would 404 on refresh."""
        api = (Path(__file__).resolve().parent.parent / "api.py").read_text(
            encoding="utf-8")
        block = re.search(r"FRONTEND_VIEWS\s*=\s*\{([^}]*)\}", api)
        assert block, "FRONTEND_VIEWS not found in api.py"
        server = set(re.findall(r'"([^"]+)"', block.group(1)))
        client_slugs = set(re.findall(r"'(/[\w-]+)'", JS))
        # Every server slug needs a client slug or its own home path.
        assert server == {"profile", "admin-dashboard", "analyse"}
        assert {"/" + s for s in server} <= client_slugs | {"/"}


class TestEmailIsTheLoginIdentity:
    """
    The username is a display handle, not a credential. These are static checks;
    the behavioural coverage lives in tests/test_password_reset.py.
    """

    def test_the_sign_in_field_asks_for_an_email(self):
        label = re.search(r'<label[^>]*for="loginUsername"[^>]*>([^<]*)<', HTML)
        assert label, "no label for the sign-in field"
        assert "email" in label.group(1).lower()
        assert "username" not in label.group(1).lower()

    def test_the_sign_in_field_is_typed_as_an_email(self):
        """
        type="email" lets the browser reject a bare handle before the request is
        made, instead of spending a round trip to be told 422 by the schema.
        """
        tag = re.search(r'<input[^>]*id="loginUsername"[^>]*>', HTML)
        assert tag and 'type="email"' in tag.group(0)

    def test_the_client_sends_email_not_username(self):
        body = re.search(r"async function doLogin\(.*?\n\}", JS, re.S)
        assert body
        assert "email:" in body.group(0)
        assert "username:" not in body.group(0)

    def test_the_api_contract_matches(self):
        """The field name has to agree with the schema or nothing signs in."""
        from auth.schemas import LoginRequest

        assert "email" in LoginRequest.model_fields
        assert "username" not in LoginRequest.model_fields


class TestLandingPageRespectsTheSession:
    """
    The landing page is both a marketing page and the app's front door. Once
    someone is signed in, "Create a free account" and "Sign in" are not neutral
    -- they are offers to do something the visitor has already done.

    The static checks below are cheap guards; tests/js/apply_session_visibility
    .test.mjs is the real one -- it executes applySessionVisibility() against the
    real markup under node with a stub DOM, and is driven from
    test_the_behavioural_harness_passes below.
    """

    @staticmethod
    def test_the_behavioural_harness_passes():
        """Runs the node harness and surfaces its output on failure."""
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed; static checks still apply")
        harness = FRONTEND.parent / "tests" / "js" / "apply_session_visibility.test.mjs"
        result = subprocess.run(
            [node, str(harness)], capture_output=True, text=True, cwd=FRONTEND.parent
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_every_auth_cta_on_the_landing_page_is_marked(self):
        """
        Registration and sign-in entry points are gated by data-when, so
        renderSession() can hide them in one place instead of by id.
        """
        for label in ("Create a free account", "Sign in", "Start free",
                      "Create an account", "Reset password"):
            assert label in HTML, f"{label} vanished entirely"

        out_tags = re.findall(r"<[^>]*data-when=\"out\"[^>]*>", HTML)
        assert len(out_tags) >= 5, "auth CTAs are not all gated by data-when"

    def test_each_conditional_element_is_marked_out_or_in(self):
        """A data-when value outside {in,out} would silently never show."""
        for value in re.findall(r'data-when="([^"]+)"', HTML):
            assert value in {"in", "out"}, value

    def test_the_signed_in_hero_offers_the_product_not_an_account(self):
        block = re.search(r'data-when="in">(.*?)</div>', HTML, re.S)
        assert block, "no signed-in hero CTA"
        assert "Analyse a variant" in block.group(1)
        assert "register" not in block.group(1)

    def test_the_footer_account_column_switches(self):
        assert 'id="footerAccountOut"' in HTML
        assert 'id="footerAccountIn"' in HTML
        signed_in = HTML.split('id="footerAccountIn"')[1].split("</div>")[0]
        assert "Sign out" in signed_in
        assert "Create an account" not in signed_in

    def test_one_function_drives_all_of_it(self):
        body = re.search(r"function applySessionVisibility\(me\)\{(.*?)\n\}", JS, re.S)
        assert body, "no applySessionVisibility"
        assert "data-when" in body.group(1) or "dataset.when" in body.group(1)
        # renderSession must call it on both the signed-in and signed-out paths.
        render = re.search(r"function renderSession\(me\)\{(.*?)\n\}", JS, re.S)
        assert render and "applySessionVisibility(me)" in render.group(1)

    def test_the_free_plan_card_does_not_offer_a_plan_you_are_on(self):
        """The note is filled in JS, because it depends on the current plan."""
        assert 'id="freePlanNote"' in HTML
        assert re.search(r"freePlanNote[\s\S]{0,400}onFree", JS), \
            "freePlanNote must branch on whether the account is on Free"


class TestBrowserSessionCookie:
    """
    The browser stopped keeping its token in localStorage. These are static
    checks over app.js, which is the only place that policy is expressed.
    """

    def test_the_session_cookie_names_match_the_server(self):
        """A rename on either side silently breaks every request."""
        from auth.cookies import CSRF_COOKIE, CSRF_HEADER

        assert f"'{CSRF_COOKIE}'" in JS
        assert f"'{CSRF_HEADER}'" in JS

    def test_a_new_session_is_never_written_to_local_storage(self):
        """
        The whole point of moving to HttpOnly: if any code path can put the token
        back in localStorage, an XSS bug can read it again.
        """
        assert "localStorage.setItem" not in JS

    def test_signing_out_also_expires_the_csrf_cookie(self):
        """
        An expired session cookie leaves the CSRF cookie behind. Since it is
        deliberately readable, clearSession() can and must drop it, or hasSession()
        keeps reporting a session that no longer exists.
        """
        body = JS.split("function clearSession()")[1].split("\n}")[0]
        assert "localStorage.removeItem" in body
        assert "document.cookie" in body
        assert "Max-Age=0" in body

    def test_writes_carry_the_csrf_header(self):
        assert "headers[CSRF_HEADER]" in JS

    def test_cookies_are_sent_to_the_api(self):
        assert "credentials: 'same-origin'" in JS

    def test_an_expired_session_is_handled(self):
        """401 while holding a session means it went stale, not that we are anonymous."""
        assert "res.status === 401 && hasSession()" in JS


def test_frontend_is_split_into_three_files():
    assert (FRONTEND / "index.html").exists()
    assert (FRONTEND / "app.css").exists()
    assert (FRONTEND / "app.js").exists()
    assert "<style>" not in HTML
    assert not re.search(r"<script>", HTML)