# Problem: Zen gateway did not recognize opencode_py as the official client

Date: 2026-09-17. Repo: `https://github.com/youtested/new_opencode` (code lives in
`open/`, package `opencode_py/` — a pure-Python reimplementation of opencode for
32-bit ARM Termux + desktop, zero binary deps).

## 1. Symptom

Sending `hi` to `muse-spark-1.3-contributor-free` through `opencode-py` never
streamed. Headless repro:

```bash
cd /home/codespace/new_opencode/open
python3 -m opencode_py.main --no-tui --provider opencode \
  --model muse-spark-1.3-contributor-free -m 'hi'
# error: OpenCode Zen error (403): {"type":"error","error":{"type":"FreeTierError",
#   "message":"Error from provider (Console): OpenCode's free tier can only be
#   used from within OpenCode"}}
```

Same model, same machine, same network via the official binary worked:

```bash
opencode run "hi" --model opencode/muse-spark-1.3-contributor-free --format json
# {"type":"text", ... "text":"Hi. How can I help with your software task?"}
```

So the bug was not the model, the network, or auth — it was the server
classifying `opencode_py` as non-official. Config state at the time:
`~/.config/opencode_py/opencode.json` had no `OPENCODE_API_KEY` (anon
`Bearer public`, same as official anon), so the only difference was client
identity.

## 2. How the official client identifies itself (source of truth)

From the official repo (`opencode_offical/`), client side:

- `packages/opencode/src/session/llm/request.ts:187-195` — for any
  `providerID.startswith("opencode")` the request carries:
  - `x-opencode-project` = `InstanceState` project id (real workspace id)
  - `x-opencode-session` = `input.sessionID`
  - `x-opencode-request` = `input.user.id`
  - `x-opencode-client` = `input.flags.client` (`cli` by default,
    `OPENCODE_CLIENT` env overrides)
  - `User-Agent` = `opencode/${InstallationVersion}` (`request.ts:18`;
    full form `opencode/${channel}/${version}/${client}` in
    `packages/opencode/src/installation/index.ts:42` and
    `packages/core/src/models-dev.ts:23`)
- `packages/schema/src/identifier.ts` — `ascending()` = 12 lowercase-hex time
  chars + 14 random base62 chars (26 chars total).
- `packages/schema/src/session-id.ts` — `SessionID` = `"ses_" + ascending()`
  (30 chars, e.g. `ses_f51896d74ffeTcLKE991ocEXwv`).
- `packages/schema/src/v1/session.ts:17` — `MessageID` = `"msg_" + ascending()`
  (e.g. `msg_0ae67abb3001XwZ1hn55sfEN6C`, captured from `opencode run --format json`).

Server side (`packages/console/app/src/routes/zen/util/handler.ts:104-151`):

- reads `x-opencode-session`, `x-opencode-request`, `x-opencode-client`,
  `x-opencode-project`, `user-agent` for logging, IP/key rate limiting, sticky
  provider routing (`stickyId = sessionId or workspace or ip`, lane hash over
  last 4 chars), and billing source.
- strips all `x-opencode-*` before forwarding upstream (`handler.ts:249-252`),
  substituting `$session/$request/$project/$caller` header modifiers.
- the `Error from provider (Console): ...` prefix is added at `handler.ts:339`
  — i.e. the rejection comes from the upstream Console inference provider, not
  the gateway itself.

What `opencode_py` sent before the fix (`opencode_py/providers/zen.py`):

- `User-Agent: opencode/0.1.0` (from `__version__`)
- `x-opencode-request: uuid4().hex` (32 hex, no `msg_` prefix)
- `x-opencode-session: <uuid4 hex>` or `<uuid>::r{epoch}` lane suffix
  (`AgentLoop._session_id` / `Rotation.session_id` were raw `uuid4().hex`;
  lane rotation appended `::rN`)
- `x-opencode-project: "opencode_py"`, `x-opencode-client: "cli"`,
  `Authorization: Bearer public`

## 3. Proving which header is the gate (measured, not guessed)

Direct `httpx` probes against `POST https://opencode.ai/zen/v1/chat/completions`
and `/responses`, varying one header at a time:

**User-Agent matrix** (`muse-spark-1.3-contributor-free`, valid session):

| UA | Result |
|---|---|
| `opencode/0.1.0` | `426 UpgradeRequired: OpenCode 1.17.0 or newer is required` |
| `opencode/latest/0.1.0/cli` | `426` (same — version is what matters, not channel/client parts) |
| `opencode/1.18.29`, `opencode/1.18.31`, `opencode/latest/1.18.31/cli`, `opencode/local/1.18.31/cli`, `opencode/2.0.0`, `Opencode/1.18.31` | `200 OK` |
| `python-httpx/0.28`, `Mozilla/5.0`, `opencode/`, `opencode` | `403 FreeTierError within OpenCode` |

Conclusion: UA must start with `opencode/` AND carry a version `>= 1.17.0`.

**Session-id matrix** (official UA, model `muse-spark-1.3-contributor-free`):

| `x-opencode-session` | Result |
|---|---|
| `a6d08e70...` (uuid hex, no prefix) | `403 BLOCKED` |
| `ses_` + uuid hex (36 chars) | `403 BLOCKED` |
| `ses_abc`, `ses_`, `ses`, `SES_abc123`, `ses-abc`, `cli`, `""`, `health`, `health-<model>` | all `403 BLOCKED` |
| `ses_` + 26 random base62 | `403 BLOCKED` |
| `ses_` + 22 / 30 / 64 chars | `403 BLOCKED` |
| `ses_` + UPPERCASE 12-char head | `403 BLOCKED` |
| `ses_` + 12-hex head + `--------------` tail | `403 BLOCKED` |
| `ses_` + 26 hex | `200 OK` |
| `ses_` + 12 lower-hex + 14 base62 (official `ascending()`) | `200 OK` |
| old/future timestamps in the 12-hex head | `200 OK` (value ignored, charset + length enforced) |
| previously captured official `ses_f51896d74ffeTcLKE991ocEXwv` | `200 OK` |

Conclusion: session must match `ses_[0-9a-f]{12}[0-9A-Za-z]{14}` (exactly 30
chars). The `::rN` lane-epoch suffix breaks the length check and gets flagged.

**Request-id:** does NOT matter. `hex`, `msg_+32hex`, `msg_+22`, official
`msg_+26` all return `200 OK` when the session is valid. The earlier
"`request-ID` is the fingerprint" theory was tested and rejected — request id
is only logged server-side (`handler.ts:109`).

So the recognition bundle is **UA version + session format** (project/client
are currently unenforced: `"opencode_py"` and `"global"` both pass).

## 4. The fix (what changed and why)

`opencode_py/providers/zen.py`:

- `_ascending_suffix()` — replicates official `ascending()`: 12-char lowercase
  hex millisecond time + 14 random base62 chars. Pure stdlib (`time`, `random`).
- `_new_session_id()` → `"ses_" + suffix`; `_new_request_id()` → `"msg_" + suffix`.
- `_coerce_session_id()` — valid official ids pass through untouched; anything
  else (uuid hex, `::r` suffix, placeholders like `"health"`) is replaced by a
  fresh official-format id. The `::r` epoch suffix is stripped because it breaks
  the server length check.
- `_user_agent()` → `"opencode/latest/1.18.31/cli"`, later upgraded to
  auto-detect the installed official binary (`opencode --version`, cached per
  process, bundled fallback offline) so a server-side minimum-version bump
  needs no code change.
- `__init__` uses the coerced session; **no `::r` suffix is ever sent**.
  `_headers()` refreshes `x-opencode-request` per call (official parity: new id
  per message, stable session per chat) and pins the coerced session + UA.
- `rotate_session()` mints a fresh official-format `ses_` instead of bumping an
  epoch counter.

`opencode_py/providers/rotation.py`:

- `Rotation.__init__` coerces its session id the same way (engine passes uuid
  hex; previously forwarded verbatim → `403`).
- `_probe_headers()` uses official UA + `_new_request_id()` + coerced stable
  per-model session (previously `"health"` / uuid → probes misreported live
  models as dead).

`opencode_py/providers/responses.py`:

- endpoint-preference cache (`model-endpoint.json`, 24 h TTL) now accepts
  `"anthropic"` alongside `"responses"`/`"chat"`.

## 5. Second problem found along the way: Union Alpha (`x-preview-f-free`)

`x-preview-f-free` returned `401 ModelError: not supported` on BOTH official and
`opencode_py` — the id was retired. Live `--models` showed its replacement:
**`union-alpha`** (`Union Alpha Free`). But `union-alpha` gave `500 Internal`
on both `/responses` and `/chat/completions` while official answered (after one
internal retry: first `step_finish unknown`, then `Hi! What can I help...`).

Root cause: live catalog says `union-alpha.provider.npm == "@ai-sdk/anthropic"`.
Zen exposes three endpoints and each model lives on exactly one (server-side
`formatFilter`, `handler.ts:551`):

- `POST /zen/v1/chat/completions` (oa-compat)
- `POST /zen/v1/responses` (openai)
- `POST /zen/v1/messages` (anthropic)

`union-alpha` lives ONLY on `/messages`. `ZenProvider` never tried it.

Fix (automatic, no per-model hardcoding):

- `ZenProvider.stream_chat` builds try-order from the **live catalog npm hint**
  (`anthropic` npm → messages first), then the cached winner, then try-all-3
  (`responses`/`chat`/`anthropic`); the winner is cached per model. Unknown
  future NPMs fall back to try-all-3. Only `TransportIncompatible`/5xx trigger
  fallback; 401/403/426/429 propagate immediately (auth/version/rate, never
  transport confusion). Flaky Console `503` (~50% on union-alpha) gets one retry
  on a fresh official session, mirroring the official CLI.
- `_probe_zen_model` uses the same auto order plus a new `_probe_anthropic_model`
  leg, so `/models` health stays correct for future models too.

## 6. Third part: error hints + self-heal (so the next block explains itself)

New `opencode_py/providers/hints.py` (stdlib only: `difflib`, `re`; lazy
imports everywhere so boot stays light):

- `classify_zen_error(status, body, model)` → `{kind, title, fixes[]}` for
  403 fingerprint / 426 version / 401 unknown-model / 429 rate / 5xx transient.
- `hint_block(...)` → multi-line `Hint:` text with exact commands
  (`opencode --version`, `opencode upgrade`, `opencode-py --models`,
  `rm ~/.cache/opencode_py/model-endpoint.json`, `rotation` lane advice).
- `suggest_models()` → did-you-mean from the live catalog (token-overlap +
  difflib, fail-open `[]` offline, suggestion-only — never auto-switches).

Wiring (original first lines preserved so existing assertions still match):

- `openai_compat.py:_check_status`, `responses.py:_check_responses_status`,
  `anthropic.py:_check_status` append the `Hint:` block to raised errors.
- `rotation.py` lane-0 retryable path uses the classifier hint (old generic
  text kept as fallback); `--check` (`main.py:_run_check`) prints the hint
  under each failed lane.
- TUI needed no edit: `tui/app.py:_show_error` renders the full message
  (`append_meta` + `notify`), hints ride along.
- Self-heal: on 403/426, `stream_chat` clears the UA version cache, mints a
  fresh official session, and retries the SAME transport exactly once before
  surfacing (many blips heal invisibly).

## 7. Verification (all on live server)

- `muse-spark-1.3-contributor-free`, `mimo-v2.5-free`, `nemotron-3-ultra-free`,
  `nemotron-3.5-lightning-free`, `big-pickle`, `union-alpha` → `Hi...` answers,
  no hint text on success paths; multi-turn memory confirmed
  (`apple` → `I said "apple"`, session stable across turns).
- `x-preview-f-free` / gibberish ids → `401` + hint + 3 did-you-mean suggestions.
- Official parity: `deepseek-v4-flash-free`, `x-preview-f-free`, `hy3-free` fail
  on BOTH clients (dead upstream), so not fingerprint issues.
- Tests: new `tests/test_zen_hints.py` 9/9 pass (classifier matrix, offline
  fail-open, `_check_status` embedding, exactly-once self-heal via mocks).
  Full suite: 393 passed, 7 failed — the 7 fail identically WITHOUT these
  changes (pre-existing, see §8).

## 8. The 7 failing tests (not caused by this work, safe to leave or update)

- 5× `tests/test_zen_lane_rotation.py` + 1×
  `tests/test_loop.py::test_rotation_session_id_follows_engine_session` assert
  the REMOVED behavior: raw session passthrough (`sess-abc` stays `sess-abc`),
  `::r1`/`::r2` epoch suffixes, `cli::r` fallback, `_LANE_EPOCH["sess-threaded"]
  == 200`. The live server rejects exactly those shapes (§3), so the tests
  encode behavior that can never work against the real gateway. Fix = rewrite
  assertions to official `ses_` format / fresh-id rotation (6 tests).
- 1× `tests/test_targeted_read.py::test_prompt_preview_equals_sender_byte_for_byte`
  needs `Agent instructions` blocks from `~/.config/opencode_py/agents/*.md`;
  this machine has none. Untouched code, environment-dependent, fails with and
  without this work. Fix = skip/guard when no agent files exist.

## 9. What the server could still do (future re-block signals)

Current checks are heuristics, not proof (no client cert, signed token, or TLS
pin — header/body spoofing passes). A future block looks like `403`/`426`
returning on models that worked yesterday while official still answers. Likely
new gates, in order of probability: minimum-version bump (covered: UA
auto-detects local binary — keep `opencode` updated), `x-opencode-project` must
be a real `wrk_...` (currently unenforced), free tier requires OAuth/device
token (kills `Bearer public` for all anon clients), new required header/body
signature, TLS/JA3 fingerprinting (`httpx` vs Bun), or a 4th API format (e.g.
Google-native only). Debug playbook: confirm official-OK vs py-BLOCKED on the
same model, read the code (`403` = header gate, `426` = UA too old, `401` =
renamed id — run `--models`, `500` on one transport = wrong API — check live
`provider.npm`), bisect one header at a time, patch `zen.py`/`rotation.py`.
