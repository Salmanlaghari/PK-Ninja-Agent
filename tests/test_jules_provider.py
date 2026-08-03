"""Comprehensive unit tests for the Jules provider integration (v1.1.0).

Covers:
* JulesProvider initialisation (key resolution, config, no-key → AIError).
* HTTP layer: x-goog-api-key auth header, retry on 429/5xx, non-retryable errors.
* Session lifecycle: create → poll → auto-approve plan → collect agent text.
* Artifact parsing: _parse_unidiff reconstructs {path, content} from a git patch.
* Sync bridge: generate / stream_chat (emulated) / plan / edit / analyze_error.
* Diagnostics/metrics counters.
* JulesAdapter: plugin wrapper, lazy init, no-key → _init_error, delegates.
* ProviderManager registration: jules is a built-in, capability detection,
  no-key → get_instance returns None, no secrets leak in status/to_dict.
* Factory get_provider(AI_PROVIDER=jules) falls back to LocalProvider w/o key.
* Security: the API key never appears in any public status/diagnostics output.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from ai_provider import (
    AIError,
    ChatMessage,
    ChatResult,
    JulesProvider,
    LocalProvider,
    Plan,
    get_provider,
    _parse_unidiff,
    _messages_to_prompt,
)
from config import get_settings


# ── Helpers ────────────────────────────────────────────────────────────────
def _settings_with_jules_key(monkeypatch, key="AQ.test-jules-key-1234567890"):
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "jules")
    monkeypatch.setenv("JULES_API_KEY", key)
    monkeypatch.setenv("JULES_POLL_INTERVAL", "0.01")
    monkeypatch.setenv("JULES_POLL_TIMEOUT", "30")
    monkeypatch.setenv("JULES_MAX_RETRIES", "2")
    get_settings.cache_clear()
    return get_settings()


class FakeResponse:
    """A minimal fake httpx.Response for the sync _request path."""

    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content if content else (
            json.dumps(json_data).encode() if json_data is not None else b""
        )
        self.headers = {}
        self.request = MagicMock()

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=self.request, response=self,
            )

    def json(self):
        return self._json if self._json is not None else {}


class FakeClient:
    """Fake httpx.Client that records calls and returns scripted responses."""

    def __init__(self, responses=None, *, sequence=None, side_effect=None):
        # `responses`: a dict method+url -> FakeResponse (or callable).
        # `sequence`: a list of FakeResponse returned in order (any method).
        # `side_effect`: an Exception to raise (for network-error tests).
        self.responses = responses or {}
        self.sequence = sequence or []
        self.side_effect = side_effect
        self.calls = []  # list of (method, url, json, params)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, json=None, params=None, headers=None):
        self.calls.append((method, url, json, params))
        if self.side_effect is not None:
            raise self.side_effect
        if self.sequence:
            return self.sequence.pop(0)
        key = (method, url)
        resp = self.responses.get(key)
        if callable(resp):
            return resp(method, url, json, params)
        if resp is None:
            # default empty 200
            return FakeResponse(200, json_data={})
        return resp


def _jules_activities(agent_text="", change_set=None):
    """Build a fake activities payload."""
    acts = []
    events = []
    if agent_text:
        events.append({"event": "agentMessaged",
                       "payload": {"agentMessage": agent_text}})
    act = {"name": "activities/123-1", "events": events}
    if change_set:
        act["changeSet"] = change_set
    acts.append(act)
    return {"activities": acts}


# ── Initialisation ──────────────────────────────────────────────────────────
class TestJulesProviderInit:
    def test_no_key_raises_aierror(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        # v1.2.0: also clear the built-in default credential so this test
        # still asserts the "absolutely no key" behaviour.
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        with pytest.raises(AIError, match="JULES_API_KEY"):
            JulesProvider(get_settings())

    def test_uses_official_base_url(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        assert p.base_url == "https://jules.googleapis.com/v1alpha"

    def test_auth_header_is_x_goog_api_key_not_bearer(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch, key="AQ.mysecretkey")
        p = JulesProvider(s)
        h = p._headers()
        assert h["x-goog-api-key"] == "AQ.mysecretkey"
        assert "Authorization" not in h  # MUST NOT use Bearer
        assert "Bearer" not in json.dumps(h)

    def test_key_falls_back_to_ai_api_key(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.setenv("AI_API_KEY", "sk-fallback-key")
        get_settings.cache_clear()
        p = JulesProvider(get_settings())
        assert p.api_key == "sk-fallback-key"

    def test_key_falls_back_to_gemini_api_key(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gem-fallback")
        get_settings.cache_clear()
        p = JulesProvider(get_settings())
        assert p.api_key == "gem-fallback"

    def test_diagnostics_initial_state(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        d = p.diagnostics_summary()
        assert d["sessions_created"] == 0
        assert d["sessions_completed"] == 0
        assert d["sessions_failed"] == 0
        assert d["last_session_id"] is None
        # No key in diagnostics.
        assert "AQ" not in json.dumps(d)


# ── HTTP layer & retry ──────────────────────────────────────────────────────
class TestJulesHttpLayer:
    def test_create_session_posts_user_input(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("POST", "https://jules.googleapis.com/v1alpha/sessions"):
                FakeResponse(200, json_data={"name": "sessions/abc123"}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            sid = p._create_session("do something")
        assert sid == "sessions/abc123"
        method, url, body, params = client.calls[0]
        assert method == "POST"
        assert url.endswith("/sessions")
        assert body["prompt"] == "do something"
        # repoless session has no sourceContext
        assert "sourceContext" not in body
        assert p.diagnostics["sessions_created"] == 1

    def test_create_session_with_repo(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("POST", "https://jules.googleapis.com/v1alpha/sessions"):
                FakeResponse(200, json_data={"name": "sessions/r1"}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            p._create_session("fix bug", repo_url="https://github.com/x/y",
                              branch="main")
        _, _, body, _ = client.calls[0]
        assert body["sourceContext"]["githubRepoContext"]["url"] == "https://github.com/x/y"
        assert body["sourceContext"]["githubRepoContext"]["startingBranch"] == "main"

    def test_retry_on_429_then_succeeds(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        # First call 429, second 200.
        seq = [
            FakeResponse(429, json_data={"error": "rate limited"}),
            FakeResponse(200, json_data={"name": "sessions/r2"}),
        ]
        client = FakeClient(sequence=seq)
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):  # don't really sleep in tests
                sid = p._create_session("retry me")
        assert sid == "sessions/r2"
        assert p.diagnostics["retries"] == 1

    def test_retry_exhausted_raises_aierror(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        seq = [FakeResponse(503), FakeResponse(503), FakeResponse(503)]
        client = FakeClient(sequence=seq)
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                with pytest.raises(AIError, match="Jules API request failed"):
                    p._create_session("nope")

    def test_non_retryable_403_raises_immediately(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("POST", "https://jules.googleapis.com/v1alpha/sessions"):
                FakeResponse(403, json_data={"error": "forbidden"}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep") as sleeps:
                with pytest.raises(AIError):
                    p._create_session("forbidden")
        # 403 is NOT retryable → no sleeps.
        assert sleeps.call_count == 0

    def test_network_error_retries_then_raises(self, monkeypatch):
        import httpx
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(side_effect=httpx.ConnectError("boom"))
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                with pytest.raises(AIError, match="Jules API request failed"):
                    p._create_session("netfail")
        assert p.diagnostics["last_error"] is not None


# ── Session polling & plan approval ─────────────────────────────────────────
class TestJulesPolling:
    def test_poll_completes_immediately(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/abc"):
                FakeResponse(200, json_data={"name": "sessions/abc",
                                             "state": "COMPLETED"}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            sess = p._poll_to_terminal("sessions/abc")
        assert sess["state"] == "COMPLETED"
        assert p.diagnostics["sessions_completed"] == 1

    def test_poll_auto_approves_plan(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        states = iter([
            FakeResponse(200, json_data={"state": "AWAITING_PLAN_APPROVAL"}),
            FakeResponse(200, json_data={"state": "IN_PROGRESS"}),
            FakeResponse(200, json_data={"state": "COMPLETED"}),
        ])
        approve_called = []

        def approve_resp(*a, **kw):
            approve_called.append(True)
            return FakeResponse(200, json_data={})

        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/s1"):
                lambda *a, **kw: next(states),
            ("POST", "https://jules.googleapis.com/v1alpha/sessions/s1:approvePlan"):
                approve_resp,
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                sess = p._poll_to_terminal("sessions/s1")
        assert sess["state"] == "COMPLETED"
        assert len(approve_called) == 1
        assert p.diagnostics["plan_approvals"] == 1

    def test_poll_failed_state(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/f1"):
                FakeResponse(200, json_data={"state": "FAILED"}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            sess = p._poll_to_terminal("sessions/f1")
        assert sess["state"] == "FAILED"
        assert p.diagnostics["sessions_failed"] == 1

    def test_poll_timeout_raises(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        p.poll_timeout = 0  # immediate timeout
        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/t1"):
                FakeResponse(200, json_data={"state": "IN_PROGRESS"}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                with pytest.raises(AIError, match="did not reach a terminal state"):
                    p._poll_to_terminal("sessions/t1")


# ── Artifact / text collection ──────────────────────────────────────────────
class TestJulesArtifacts:
    def test_collect_agent_text(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/a1/activities"):
                FakeResponse(200, json_data=_jules_activities(
                    agent_text="Hello from Jules!")),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            txt = p._collect_agent_text("sessions/a1")
        assert txt == "Hello from Jules!"

    def test_collect_agent_text_empty(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/a2/activities"):
                FakeResponse(200, json_data={"activities": []}),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            txt = p._collect_agent_text("sessions/a2")
        assert txt == ""

    def test_collect_edits_from_changeset(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        unidiff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,2 +1,3 @@\n"
            " import os\n"
            "+import sys\n"
            " print('hi')\n"
        )
        cs = {"gitPatch": {"unidiffPatch": unidiff}}
        client = FakeClient(responses={
            ("GET", "https://jules.googleapis.com/v1alpha/sessions/e1/activities"):
                FakeResponse(200, json_data=_jules_activities(change_set=cs)),
        })
        with patch("ai_provider.httpx.Client", return_value=client):
            edits = p._collect_edits("sessions/e1")
        assert len(edits) == 1
        assert edits[0]["path"] == "main.py"
        assert "import sys" in edits[0]["content"]


# ── Sync bridge methods ──────────────────────────────────────────────────────
class TestJulesSyncBridge:
    def _full_session_client(self, agent_text="Jules says hello"):
        """A client that scripts: create → poll(COMPLETED) → activities."""
        create = FakeResponse(200, json_data={"name": "sessions/gen1"})
        poll = FakeResponse(200, json_data={"name": "sessions/gen1",
                                            "state": "COMPLETED"})
        acts = FakeResponse(200, json_data=_jules_activities(agent_text=agent_text))
        return FakeClient(sequence=[create, poll, acts])

    def test_generate_returns_agent_text(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = self._full_session_client("Plan looks good")
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                text = p.generate([ChatMessage("user", "plan it")])
        assert text == "Plan looks good"
        assert p.diagnostics["sessions_created"] == 1
        assert p.diagnostics["sessions_completed"] == 1

    def test_generate_empty_text_returns_placeholder(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = self._full_session_client(agent_text="")
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                text = p.generate([ChatMessage("user", "hi")])
        assert "no agent message" in text

    def test_stream_chat_emulates_chunks(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        # 20 words → at chunk_size 12, produces 2 chunks.
        words = [f"w{i}" for i in range(20)]
        text = " ".join(words)
        client = self._full_session_client(agent_text=text)
        tokens = []
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                result = p.stream_chat([ChatMessage("user", "hi")],
                                       on_token=lambda t: tokens.append(t))
        assert isinstance(result, ChatResult)
        assert result.model == "jules-async-agent"
        # Tokens should reconstruct the full text (joined by spaces).
        assert " ".join(t.strip() for t in tokens) == text
        assert len(tokens) > 1  # chunked, not one blob

    def test_plan_returns_plan_object(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = self._full_session_client(
            agent_text='{"summary": "Do the thing", "steps": ["a", "b"]}')
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                plan = p.plan("do thing", "ctx")
        assert plan.summary == "Do the thing"
        assert plan.steps == ["a", "b"]

    def test_edit_uses_changeset_edits(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        unidiff = (
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
            "@@ -1,1 +1,2 @@\n old\n+new\n"
        )
        cs = {"gitPatch": {"unidiffPatch": unidiff}}
        create = FakeResponse(200, json_data={"name": "sessions/ed1"})
        poll = FakeResponse(200, json_data={"state": "COMPLETED"})
        acts = FakeResponse(200, json_data=_jules_activities(change_set=cs))
        client = FakeClient(sequence=[create, poll, acts])
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                edits = p.edit("task", Plan("summary", []),
                               [{"path": "f.py"}])
        assert len(edits) == 1
        assert edits[0]["path"] == "f.py"

    def test_analyze_error_returns_text(self, monkeypatch):
        s = _settings_with_jules_key(monkeypatch)
        p = JulesProvider(s)
        client = self._full_session_client("Likely a syntax error")
        with patch("ai_provider.httpx.Client", return_value=client):
            with patch("time.sleep"):
                txt = p.analyze_error("task", "SyntaxError", [])
        assert "syntax" in txt.lower()


# ── _parse_unidiff helper ───────────────────────────────────────────────────
class TestParseUnidiff:
    def test_single_file_addition(self):
        diff = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n x\n+y\n"
        )
        edits = _parse_unidiff(diff)
        assert edits == [{"path": "a.py", "content": "x\ny\n"}]

    def test_multiple_files(self):
        diff = (
            "diff --git a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n"
            "diff --git a/two.py b/two.py\n--- a/two.py\n+++ b/two.py\n"
            "@@ -1,1 +1,2 @@\n c\n+d\n"
        )
        edits = _parse_unidiff(diff)
        paths = [e["path"] for e in edits]
        assert "one.py" in paths and "two.py" in paths

    def test_empty_diff(self):
        assert _parse_unidiff("") == []

    def test_strips_b_prefix(self):
        diff = (
            "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n"
        )
        edits = _parse_unidiff(diff)
        assert edits[0]["path"] == "src/x.py"


# ── _messages_to_prompt helper ──────────────────────────────────────────────
class TestMessagesToPrompt:
    def test_flattens_roles(self):
        msgs = [ChatMessage("system", "be good"), ChatMessage("user", "hi")]
        prompt = _messages_to_prompt(msgs)
        assert "[SYSTEM]" in prompt
        assert "[USER]" in prompt
        assert "be good" in prompt and "hi" in prompt


# ── JulesAdapter (plugin wrapper) ───────────────────────────────────────────
class TestJulesAdapter:
    def test_import_from_providers(self):
        from providers import JulesAdapter
        assert JulesAdapter.name == "jules"
        assert JulesAdapter.requires_api_key is True
        cap = JulesAdapter.capability
        assert cap.streaming is True
        assert cap.code_editing is True
        assert cap.tool_calling is True

    def test_no_key_captures_init_error(self, monkeypatch):
        from providers import JulesAdapter, reset_manager
        get_settings.cache_clear()
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        # v1.2.0: also clear the built-in default credential.
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        reset_manager()
        adapter = JulesAdapter(get_settings())
        assert adapter._inner is None
        assert adapter._init_error is not None
        # Accessing _provider raises AIError.
        with pytest.raises(AIError):
            _ = adapter._provider
        # Diagnostics reflects uninitialised state.
        d = adapter.diagnostics()
        assert d["initialised"] is False

    def test_with_key_initialises(self, monkeypatch):
        from providers import JulesAdapter
        s = _settings_with_jules_key(monkeypatch)
        adapter = JulesAdapter(s)
        assert adapter._inner is not None
        assert adapter._inner.api_key == "AQ.test-jules-key-1234567890"
        d = adapter.diagnostics()
        assert d["initialised"] is True


# ── ProviderManager integration ─────────────────────────────────────────────
class TestJulesManagerIntegration:
    def test_jules_is_registered_builtin(self):
        from providers import ProviderManager, reset_manager
        reset_manager()
        m = ProviderManager()
        names = set(m.all_info().keys())
        assert "jules" in names
        assert {"local", "openai", "gemini", "mock", "jules"} <= names
        reset_manager()

    def test_jules_capability_advertised(self):
        from providers import ProviderManager, reset_manager
        reset_manager()
        m = ProviderManager()
        info = m.all_info()["jules"]
        assert info.capability.streaming is True
        assert info.capability.code_editing is True
        assert info.requires_api_key is True
        assert "jules.googleapis.com" in info.description
        reset_manager()

    def test_get_instance_returns_none_without_key(self, monkeypatch):
        from providers import ProviderManager, reset_manager
        get_settings.cache_clear()
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        # v1.2.0: also clear the built-in default credential.
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        reset_manager()
        m = ProviderManager(get_settings())
        assert m.get_instance("jules") is None
        reset_manager()

    def test_get_instance_builds_with_key(self, monkeypatch):
        from providers import ProviderManager, reset_manager
        s = _settings_with_jules_key(monkeypatch)
        reset_manager()
        m = ProviderManager(s)
        inst = m.get_instance("jules")
        assert inst is not None
        assert inst.name == "jules"
        reset_manager()

    def test_no_secrets_in_manager_status(self, monkeypatch):
        from providers import ProviderManager, reset_manager, provider_manager_status
        s = _settings_with_jules_key(monkeypatch, key="AQ.SUPERSECRETCANNOTLEAK1234567890ABCDEF")
        reset_manager()
        m = ProviderManager(s)
        m.get_instance("jules")  # force construction
        status = provider_manager_status()
        blob = json.dumps(status)
        assert "SUPERSECRETCANNOTLEAK" not in blob
        # Generic long-alphanumeric secret guard.
        import re
        leaks = re.findall(r"[A-Za-z0-9_-]{32,}", blob)
        assert leaks == [], f"potential secret leaked: {leaks}"
        reset_manager()


# ── Factory & security ──────────────────────────────────────────────────────
class TestJulesFactory:
    def test_factory_jules_without_key_falls_back_to_local(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.setenv("AI_PROVIDER", "jules")
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        # v1.2.0: also clear the built-in default credential so the factory
        # genuinely has no key and falls back to LocalProvider.
        monkeypatch.setenv("BUILTIN_AI_API_KEY", "")
        get_settings.cache_clear()
        p = get_provider()
        assert isinstance(p, LocalProvider)

    def test_factory_jules_with_key_returns_jules(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.setenv("AI_PROVIDER", "jules")
        monkeypatch.setenv("JULES_API_KEY", "AQ.factorykey123")
        get_settings.cache_clear()
        p = get_provider()
        assert isinstance(p, JulesProvider)
        assert p.name == "jules"

    def test_provider_status_no_key_leak(self, monkeypatch):
        from ai_provider import provider_status
        get_settings.cache_clear()
        monkeypatch.setenv("AI_PROVIDER", "jules")
        monkeypatch.setenv("JULES_API_KEY", "AQ.STATUSSECRETKEY1234567890123456")
        get_settings.cache_clear()
        st = provider_status()
        assert st["provider"] == "jules"
        assert st["configured"] is True
        assert st["streaming_supported"] is True
        blob = json.dumps(st)
        assert "STATUSSECRETKEY" not in blob
