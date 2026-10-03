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


def test_frontend_is_split_into_three_files():
    assert (FRONTEND / "index.html").exists()
    assert (FRONTEND / "app.css").exists()
    assert (FRONTEND / "app.js").exists()
    assert "<style>" not in HTML
    assert not re.search(r"<script>", HTML)