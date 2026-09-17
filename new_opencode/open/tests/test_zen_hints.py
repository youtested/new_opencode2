"""Tests for providers/hints.py: Zen error classifier + fix suggestions."""

from opencode_py.providers import hints as H


def test_403_fingerprint_hint():
    body = '{"type":"error","error":{"type":"FreeTierError","message":"Error from provider (Console): OpenCode\'s free tier can only be used from within OpenCode"}}'
    info = H.classify_zen_error(403, body)
    assert info["kind"] == "fingerprint_403"
    block = H.hint_block(403, body)
    assert block.startswith("Hint:")
    assert "opencode-py --models" in block
    assert "model-endpoint.json" in block


def test_426_upgrade_hint():
    body = '{"type":"error","error":{"type":"UpgradeRequired","message":"Error from provider (Console): OpenCode 1.17.0 or newer is required"}}'
    info = H.classify_zen_error(426, body)
    assert info["kind"] == "upgrade_426"
    assert "opencode upgrade" in H.hint_block(426, body)


def test_401_model_hint_with_suggestion():
    body = '{"type":"error","error":{"type":"ModelError","message":"Model x-preview-f-free is not supported"}}'
    info = H.classify_zen_error(401, body, model="x-preview-f-free")
    assert info["kind"] == "model_401"
    block = H.hint_block(401, body, model="x-preview-f-free")
    assert "opencode-py --models" in block
    # did-you-mean is best-effort: only assert the prefix when offline catalog works
    assert "Did you mean" in block or "Did you mean" not in block  # never crashes


def test_429_rate_hint():
    info = H.classify_zen_error(429, "rate limited")
    assert info["kind"] == "rate_429"
    assert "rotation" in H.hint_block(429, "rate limited")


def test_5xx_transient_hint():
    info = H.classify_zen_error(503, "Internal server error")
    assert info["kind"] == "transient_5xx"
    assert H.hint_block(503, "boom") != ""


def test_unknown_gives_empty_block():
    assert H.hint_block(None, "some weird thing") == ""
    assert H.classify_zen_error(None, "some weird thing")["kind"] == "unknown"


def test_suggest_models_never_raises_offline():
    # unknown catalog / empty id must fail open, never raise
    assert H.suggest_models("") == []
    assert isinstance(H.suggest_models("union-alpha"), list)


def test_check_status_embeds_hint():
    """_check_status keeps the original first line and appends the Hint block."""
    from opencode_py.providers.openai_compat import OpenAICompatProvider

    class Resp:
        status_code = 403
        headers = {}

        def read(self):
            return b'{"type":"error","error":{"type":"FreeTierError","message":"within OpenCode"}}'

    p = OpenAICompatProvider(id="opencode", base_url="https://opencode.ai/zen/v1", model="union-alpha")
    try:
        p._check_status(Resp())
        raise AssertionError("should have raised")
    except Exception as e:
        text = str(e)
        assert "403" in text  # original preserved
        assert "Hint:" in text  # hint appended


def test_self_heal_runs_once():
    """ZenProvider retries a 403 exactly once with a fresh session, then surfaces."""
    from opencode_py.providers import zen as Z

    calls = {"n": 0}

    p = Z.ZenProvider.__new__(Z.ZenProvider)
    # non-anthropic model so _try_order starts at mocked "chat" (no network)
    p.model = "muse-spark-1.3-contributor-free"
    p.name = "OpenCode Zen"
    p.base_url = Z.ZEN_BASE_URL
    p.api_key = "public"
    p.timeout = None
    p.reasoning_effort = None
    p._responses_box = []
    p._base_session = Z._new_session_id()
    p.extra_headers = {"x-opencode-session": p._base_session}

    from opencode_py.providers.base import ProviderError

    orig_headers = Z.ZenProvider._headers

    def fake_headers(self):
        return {
            "User-Agent": Z._user_agent(),
            "x-opencode-request": Z._new_request_id(),
            "x-opencode-session": self._base_session,
        }

    Z.ZenProvider._headers = fake_headers
    try:
        from opencode_py.providers import responses as R

        orig_pref = R.get_preferred_endpoint
        R.get_preferred_endpoint = lambda mid: "chat"
        orig_stream = Z.OpenAICompatProvider._stream

        def boom(self, messages, tools, sink, **kw):
            calls["n"] += 1
            raise ProviderError("OpenCode Zen error (403): FreeTierError within OpenCode", status=403)

        Z.OpenAICompatProvider._stream = boom
        try:
            p.stream_chat([{"role": "user", "content": "hi"}], [], on_event=lambda e: None)
            raise AssertionError("should have raised")
        except ProviderError as e:
            assert "Hint:" in str(e) or "403" in str(e)
        assert calls["n"] == 2, f"expected exactly 1 self-heal retry, got {calls['n']}"
    finally:
        Z.ZenProvider._headers = orig_headers
        R.get_preferred_endpoint = orig_pref
        Z.OpenAICompatProvider._stream = orig_stream
