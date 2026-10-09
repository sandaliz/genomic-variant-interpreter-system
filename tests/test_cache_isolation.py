"""
Cache isolation tests for main.VariantInterpretationSystem.

The API layer is multi-tenant, so a cached pipeline result must never be
served to a different account. These tests pin that guarantee directly on the
orchestrator rather than only through the HTTP layer.
"""

import pytest

from main import VariantInterpretationSystem


@pytest.fixture
def system():
    # use_llm=False keeps construction cheap and offline; the caching logic under
    # test is identical in both modes.
    return VariantInterpretationSystem(use_llm=False, lazy_load=True)


class TestCacheKey:
    def test_same_query_different_user_gives_different_keys(self, system):
        first = system._generate_cache_key("BRCA1 c.68_69del", "demo-clinician-key", "1")
        second = system._generate_cache_key("BRCA1 c.68_69del", "demo-clinician-key", "2")
        assert first != second

    def test_same_user_same_query_is_stable(self, system):
        a = system._generate_cache_key("BRCA1 c.68_69del", "demo-clinician-key", "1")
        b = system._generate_cache_key("BRCA1 c.68_69del", "demo-clinician-key", "1")
        assert a == b

    def test_different_query_same_user_gives_different_keys(self, system):
        a = system._generate_cache_key("BRCA1 c.68_69del", "k", "1")
        b = system._generate_cache_key("BRCA2 c.5946del", "k", "1")
        assert a != b

    def test_user_id_wins_over_the_api_key_for_identity(self, system):
        # With an authenticated user the key must be bound to that user, so the
        # legacy API key cannot widen the cache scope.
        with_user = system._generate_cache_key("q", "demo-clinician-key", "42")
        without_user = system._generate_cache_key("q", "demo-clinician-key")
        assert with_user != without_user


class TestCachedRunIsolation:
    def test_a_cached_result_is_not_reused_for_another_user(self, system, monkeypatch):
        seen = []

        def _fake(raw_query, api_key, user_id=""):
            seen.append(user_id)
            return {"variant_id": f"result-for-{user_id}"}

        monkeypatch.setattr(system, "_run_uncached", _fake)
        system._run_cached.cache_clear()

        first = system._run_cached("k1", "q", "demo-clinician-key", "1")
        second = system._run_cached("k2", "q", "demo-clinician-key", "2")
        assert first["variant_id"] == "result-for-1"
        assert second["variant_id"] == "result-for-2"
        assert seen == ["1", "2"]
        system._run_cached.cache_clear()

    def test_the_same_user_repeated_query_hits_the_cache(self, system, monkeypatch):
        calls = []

        def _fake(raw_query, api_key, user_id=""):
            calls.append(user_id)
            return {"variant_id": "cached"}

        monkeypatch.setattr(system, "_run_uncached", _fake)
        system._run_cached.cache_clear()

        key = system._generate_cache_key("q", "demo-clinician-key", "7")
        assert system._run_cached(key, "q", "demo-clinician-key", "7") == {"variant_id": "cached"}
        assert system._run_cached(key, "q", "demo-clinician-key", "7") == {"variant_id": "cached"}
        assert calls == ["7"]
        system._run_cached.cache_clear()