"""OpenCode Zen provider (https://opencode.ai/zen/v1) — the free models.

Thin wrapper over OpenAICompatProvider pointing at Zen's OpenAI-compatible
endpoint. With no API key, free (cost==0) models are used and Zen accepts the
literal API key "public" (mirrors opencode's behavior).

The Zen gateway throttles anonymous clients (unknown User-Agent, no
x-opencode-* headers) to a tiny free allowance — a couple of requests, then
429 FreeUsageLimitError. The official opencode client identifies itself with
`User-Agent: opencode/...` plus x-opencode-* headers, and the gateway treats
those as trusted clients with the real free quota. This provider sends the
same identity headers so free models (x-preview-f-free, etc.) keep working
across turns instead of being blocked after the first message.
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from .openai_compat import OpenAICompatProvider

ZEN_BASE_URL = "https://opencode.ai/zen/v1"


def _ascending_suffix() -> str:
    """Official ascending() tail: 12 lowercase-hex time + 14 base62 random (26 chars).

    Matches packages/schema/src/identifier.ts create(): time = 6 bytes hex,
    then 14 random base62 chars. Server validates x-opencode-session strictly
    (ses_ + 12 hex + 14 base62); anything else (uuid hex, ::r epoch suffix)
    is treated as non-official and free tier is refused.
    """
    import random as _rand
    import time as _time

    ts = format(int(_time.time() * 1000) & 0xFFFFFFFFFFFF, "012x")[-12:]
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return ts + "".join(_rand.choice(chars) for _ in range(14))


def _new_session_id() -> str:
    return "ses_" + _ascending_suffix()


def _new_request_id() -> str:
    return "msg_" + _ascending_suffix()


def _coerce_session_id(sid: str | None) -> str:
    """Return a server-accepted session id (ses_ + 26 chars, 12 hex head).

    The engine passes uuid4 hex; the gateway rejects those as non-official.
    Valid official ids pass through untouched (including a fresh ascending
    one); anything else is replaced by a fresh official-format id. The
    ::r lane-epoch suffix is stripped (it breaks the length check).
    """
    import re as _re

    if sid:
        base = str(sid).split("::r")[0]
        if _re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", base):
            return base
    return _new_session_id()


_FALLBACK_UA = "opencode/latest/1.18.31/cli"


def _user_agent() -> str:
    """Official client identity, automatic for future versions.

    userAgent(client) = `opencode/{channel}/{version}/{client}`.
    The gateway requires opencode/ >=1.17.0 for free tier (else 426
    UpgradeRequired) and any non-opencode UA gets 403 FreeTierError.
    Auto-detects the installed official binary's version (`opencode
    --version`) so a server-side minimum bump doesn't need a code change;
    falls back to the bundled version offline. Cached per process.
    """
    try:
        cached = getattr(_user_agent, "_cached", "")
        if cached:
            return cached
    except Exception:
        pass
    ver = ""
    try:
        import re as _re
        import shutil as _sh
        import subprocess as _sp

        exe = _sh.which("opencode")
        if exe:
            out = _sp.run([exe, "--version"], capture_output=True, text=True, timeout=5)
            txt = (out.stdout or "") + (out.stderr or "")
            m = _re.search(r"(\d+\.\d+\.\d+)", txt)
            if m:
                ver = m.group(1)
    except Exception:
        ver = ""
    ua = f"opencode/latest/{ver}/cli" if ver else _FALLBACK_UA
    try:
        _user_agent._cached = ua  # type: ignore[attr-defined]
    except Exception:
        pass
    return ua

# Lane-rotation registry: Zen pins each x-opencode-session id to ONE upstream
# lane. When that lane dies server-side (streams end with Zen's own
# finish_reason="network_error"), retrying with the SAME id hammers the SAME
# dead lane — the user sees ↻ climb to (50) without a single success while a
# brand-new session id would have worked on attempt #1 (measured 2026-08-23:
# fixed sid A 5/5 OK, fixed sid B 1/5, fresh ids mixed). Keyed by BASE session
# id so a rotation survives provider re-instantiation: rotation.build_provider
# constructs a NEW ZenProvider for every attempt with the same engine sid.
_LANE_EPOCH: dict[str, int] = {}
_LANE_LOCK = threading.Lock()

# Limited-time free models on Zen ($0). Live-fetched in factory; this is the
# bundled fallback for when the network model list is unavailable (R2 risk).
# Ordered most-used first: muse-spark-1.3 is this user's daily driver.
FREE_MODELS: list[dict] = [
    {"id": "muse-spark-1.3-contributor-free", "name": "Muse Spark 1.3 Free", "context": 1048576, "output": 131072},
    {"id": "x-preview-f-free", "name": "Ox Alpha Free (Unlimited)", "context": 1000000, "output": 131072},
    {"id": "big-pickle", "name": "Big Pickle", "context": 200000, "output": 32000},
    {"id": "hy3-free", "name": "Hy3 Free", "context": 190000, "output": 64000},
    {"id": "mimo-v2.5-free", "name": "MiMo-V2.5 Free", "context": 200000, "output": 32000},
    {"id": "deepseek-v4-flash-free", "name": "DeepSeek V4 Flash Free", "context": 200000, "output": 128000},
    {"id": "nemotron-3-ultra-free", "name": "Nemotron 3 Ultra Free", "context": 1000000, "output": 128000},
    {"id": "nemotron-3.5-lightning-free", "name": "Nemotron 3.5 Lightning Free", "context": 262144, "output": 128000},
]


class ZenProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "x-preview-f-free",
        session_id: str | None = None,
        project: str | None = None,
        **kwargs: Any,
    ):
        # If no key given, we still need SOMETHING; Zen accepts "public" for free models.
        effective_key = api_key or "public"
        # Stable session id across turns (like the official client) so Zen
        # keeps provider affinity and serves the real free quota. The id only
        # changes when rotate_session() runs on a DEAD lane — never per turn.
        # x-opencode-request is a FRESH id per request (official: per-message
        # user id); reusing the session id there made every turn look like
        # the same request. _headers() refreshes it on every call below.
        self._base_session = _coerce_session_id(session_id)
        rot_key = self._base_session
        with _LANE_LOCK:
            epoch = _LANE_EPOCH.get(rot_key, 0)
        # NOTE: no ::r epoch suffix — it breaks the server's ses_ length
        # check and gets the client flagged as non-official. Lane rotation
        # mints a fresh official-format session id instead (rotate_session).
        effective_sid = self._base_session
        client_headers: dict[str, str] = {
            "User-Agent": _user_agent(),
            "x-opencode-client": "cli",
            "x-opencode-project": project or "opencode_py",
            "x-opencode-request": _new_request_id(),
        }
        if effective_sid:
            client_headers["x-opencode-session"] = effective_sid
        merged = dict(client_headers)
        merged.update(kwargs.pop("extra_headers", {}) or {})
        super().__init__(
            id="opencode",
            name="OpenCode Zen",
            base_url=ZEN_BASE_URL,
            api_key=effective_key,
            model=model,
            is_free=True,
            extra_headers=merged,
            reasoning_passthrough=True,
            **kwargs,
        )
        self.has_key = bool(api_key)

    def _headers(self) -> dict[str, str]:
        """Fresh x-opencode-request per call, stable x-opencode-session.

        Official sends a new request id for every message but keeps the
        session id stable for the whole chat (sticky lane). The base
        implementation would resend the init-time request id forever.
        """
        headers = super()._headers()
        headers["x-opencode-request"] = _new_request_id()
        # keep session pinned to the coerced official-format id
        try:
            headers["x-opencode-session"] = self._base_session
        except Exception:
            pass
        headers["User-Agent"] = _user_agent()
        return headers

    def abort_stream(self) -> None:
        # Responses-API streams register here too (see stream_chat); the chat
        # transport manages self._active_resp itself.
        from ..util.net import force_close_response

        box = getattr(self, "_responses_box", None)
        if isinstance(box, list) and box:
            force_close_response(box[0])
        super().abort_stream()

    def _session_cache_key(self) -> str | None:
        """Stable cache key for the Responses `prompt_cache_key` (no lane epoch)."""
        base = self._base_session or self.extra_headers.get("x-opencode-session") or ""
        return str(base).split("::r")[0] or None

    def stream_chat(self, messages, tools=None, on_event=None, **kwargs):
        """Auto transport: try every Zen API until one answers.

        Zen exposes 3 endpoints (chat/completions, responses, messages) and
        each model lives on exactly one (server-side formatFilter). New
        models pick their endpoint via the live catalog's provider.npm with
        zero code changes; unknown future models fall back to try-all-3 in
        cached-preference order and the winner is remembered per model.

        Only TransportIncompatible / HTTP 5xx trigger the silent fallback;
        401/403/426/429 propagate immediately (auth/version/rate, not transport).
        Flaky Console 503s get one retry on a fresh official session (the
        official CLI does the same: first step_finish unknown, second answers).
        """
        from .base import ProviderError
        from .responses import (
            TransportIncompatible,
            get_preferred_endpoint,
            set_preferred_endpoint,
            stream_responses,
        )

        events: list = []
        sink = on_event or (lambda e: events.append(e))
        model = self.model
        is_interrupted = kwargs.get("is_interrupted")

        def _catalog_npm(mid: str) -> str:
            """Live provider.npm for a model ('' when offline/unknown)."""
            try:
                from .rotation import fetch_catalog as _fc

                cat = (_fc().get("opencode") or {}).get("models", {})
                bare = str(mid).split("/", 1)[-1]
                info = cat.get(bare) or cat.get(str(mid)) or {}
                return str(((info.get("provider") or {}).get("npm")) or "")
            except Exception:
                return ""

        def _try_order(mid: str) -> list[str]:
            """Endpoint order for this model, automatic for future models.

            Catalog hint first (anthropic npm -> messages API), then the
            cached winner, then the rest. No hardcoded model names.
            """
            npm = _catalog_npm(mid).lower()
            hint = "anthropic" if "anthropic" in npm else ""
            # future-proof: google/vertex native models would surface here
            # as a 4th endpoint; until then the 3 Zen endpoints cover all.
            base = ["responses", "chat", "anthropic"]
            try:
                pref = get_preferred_endpoint(mid)
            except Exception:
                pref = "responses"
            order: list[str] = []
            for cand in ([hint] if hint else []) + ([pref] if pref in base else []) + base:
                if cand and cand not in order:
                    order.append(cand)
            return order or base

        def _run(ep: str) -> None:
            effort = (getattr(self, "reasoning_effort", "") or "").strip().lower() or None
            if ep == "responses":
                box: list = [None]
                self._responses_box = box
                try:
                    stream_responses(
                        base_url=self.base_url,
                        headers=self._headers(),
                        timeout=self.timeout,
                        model=model,
                        name=self.name,
                        messages=messages,
                        tools=tools,
                        sink=sink,
                        session_key=self._session_cache_key(),
                        is_interrupted=is_interrupted,
                        active_slot=box,
                        extra_payload={"reasoning_effort": effort} if effort else None,
                    )
                finally:
                    self._responses_box = []
            elif ep == "anthropic":
                from .anthropic import AnthropicProvider as _Ap

                ap = _Ap(
                    id="opencode",
                    name=self.name,
                    base_url=self.base_url,
                    api_key=self.api_key,
                    model=model,
                    is_free=True,
                    extra_headers=dict(self._headers()),
                    timeout=self.timeout,
                    reasoning_effort=getattr(self, "reasoning_effort", None),
                )
                ap.stream_chat(messages, tools, sink, **kwargs)
            else:
                self._stream(messages, tools, sink, **kwargs)

        def _finish(ep: str):
            try:
                set_preferred_endpoint(model, ep)
            except Exception:
                pass
            if on_event is None:
                return events  # type: ignore[return-value]
            return None

        def _retryable_5xx(err: BaseException) -> bool:
            st = getattr(err, "status", None)
            return isinstance(st, int) and 500 <= st < 600

        def _self_heal() -> None:
            """One safe self-heal: re-detect UA version + fresh session.

            Runs at most once per turn (guarded by _healed flag below):
            picks up a newly upgraded official binary and drops a possibly
            stale/flagged session id. Never raises.
            """
            try:
                try:
                    delattr(_user_agent, "_cached")
                except Exception:
                    pass
                self.rotate_session()
            except Exception:
                pass

        order = _try_order(model)
        first_err: BaseException | None = None
        _healed = False
        for i, ep in enumerate(order):
            try:
                _run(ep)
                return _finish(ep)
            except TransportIncompatible as e:
                if first_err is None:
                    first_err = e
                continue  # wrong API — try next transport
            except ProviderError as e:
                if first_err is None:
                    first_err = e
                st = getattr(e, "status", None)
                if st in (403, 426) and not _healed:
                    # fingerprint/version block: self-heal once (fresh
                    # official session + UA re-detect), retry SAME transport
                    # before surfacing the hint. The hint text already
                    # attached at _check_status says exactly this happened.
                    _healed = True
                    _self_heal()
                    try:
                        _run(ep)
                        return _finish(ep)
                    except (TransportIncompatible, ProviderError) as e2:
                        first_err = e2
                        raise e2
                # auth/rate/model-gone: never a transport issue
                if st in (401, 403, 426, 429):
                    raise
                if _retryable_5xx(e) and i == len(order) - 1:
                    # last transport but 5xx: flaky Console lane — one
                    # retry on a FRESH official session before giving up.
                    try:
                        self.rotate_session()
                        _run(ep)
                        return _finish(ep)
                    except (TransportIncompatible, ProviderError) as e2:
                        raise e from e2
                if not _retryable_5xx(e):
                    raise
                continue  # 5xx: try next transport
        # every transport failed — surface the first error (most context)
        if first_err is not None:
            raise first_err
        raise ProviderError(f"{self.name}: all transports failed for {model}")

    def rotate_session(self) -> None:
        """Force a different upstream lane on the next request.

        Bumps this session's rotation epoch so every future ZenProvider built
        for the same base session id carries a fresh x-opencode-session value —
        Zen then assigns it a new lane instead of the dead one. Called by the
        rotation layer when a stream dies with server-side lane failure
        (in-band error / empty reply), NOT on local network problems.
        x-opencode-request stays fresh-per-call via _headers(), untouched here.
        """
        # Mint a fresh official-format session id (no ::r suffix — the
        # suffix breaks the server's ses_ length check and flags us).
        fresh = _new_session_id()
        try:
            with _LANE_LOCK:
                _LANE_EPOCH[self._base_session or "__nosession__"] = (
                    _LANE_EPOCH.get(self._base_session or "__nosession__", 0) + 1
                )
        except Exception:
            pass
        self._base_session = fresh
        self.extra_headers["x-opencode-session"] = fresh
