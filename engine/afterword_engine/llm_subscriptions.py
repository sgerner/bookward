"""Subscription-backed LLM runners.

The subscription providers intentionally use their supported command-line
surfaces instead of copying credentials into HTTP clients.  Both runners have
strict subprocess boundaries: argv is passed as a list, the child receives a
small environment, stdout is bounded and parsed as a protocol, and every
request has a deadline.  Codex threads and Claude sessions are ephemeral;
only the provider's authentication home is persistent.
"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .config import settings
from .llm import (
    RANKING_SCHEMA,
    BaseLLMClient,
    LLMError,
    LLMResult,
    SYSTEM_PROMPT,
    validate_rankings,
)


AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_OPENAI_CODEX = "openai_codex"
AUTH_TYPE_CLAUDE_CODE = "claude_code"

OPENAI_CODEX_AUTH_TYPES = frozenset(
    {
        AUTH_TYPE_OPENAI_CODEX,
        "openai_codex_subscription",
        "openai_chatgpt",
        "chatgpt_subscription",
        "chatgpt_device_code",
        "openai_device_code",
        "openai_subscription",
        "chatgpt",
        "codex",
    }
)
CLAUDE_CODE_AUTH_TYPES = frozenset(
    {
        AUTH_TYPE_CLAUDE_CODE,
        "claude_code_subscription",
        "anthropic_claude_code",
        "anthropic_claude",
        "claude_subscription",
        "claude_oauth",
        "anthropic_oauth",
        "anthropic_subscription",
        "claude",
    }
)

MAX_PROTOCOL_LINE_BYTES = 2_000_000
MAX_CLAUDE_OUTPUT_BYTES = 4_000_000
SUBSCRIPTION_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")


class SubscriptionRunnerError(LLMError):
    """A provider subprocess or protocol failure safe to persist."""


class _ProtocolTimeout(SubscriptionRunnerError):
    pass


def normalize_auth_type(auth_type: str) -> str:
    """Return the canonical storage value for a connection auth type."""

    value = (auth_type or AUTH_TYPE_API_KEY).strip().lower()
    if value in OPENAI_CODEX_AUTH_TYPES:
        return AUTH_TYPE_OPENAI_CODEX
    if value in CLAUDE_CODE_AUTH_TYPES:
        return AUTH_TYPE_CLAUDE_CODE
    if value == AUTH_TYPE_API_KEY:
        return AUTH_TYPE_API_KEY
    raise ValueError("Unsupported LLM authentication type")


def is_openai_codex_auth(auth_type: str) -> bool:
    return (auth_type or "").strip().lower() in OPENAI_CODEX_AUTH_TYPES


def is_claude_code_auth(auth_type: str) -> bool:
    return (auth_type or "").strip().lower() in CLAUDE_CODE_AUTH_TYPES


def _private_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Subscription auth directory must be absolute")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is affected by the process umask and does not tighten an
    # existing directory.  Authentication material must stay owner-only.
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise ValueError("Subscription auth directory permissions could not be restricted") from exc
    return path


def _command(raw: str, default: str) -> str:
    value = (raw or default).strip()
    if not value or "\x00" in value:
        raise ValueError("Subscription executable is invalid")
    if os.path.sep not in value:
        resolved = shutil.which(value)
        if resolved:
            return resolved
    return value


def _safe_env(*, home: Path, token: str | None = None, config_dir: Path | None = None) -> dict[str, str]:
    """Build a deliberately small child environment.

    In particular, do not inherit API keys, proxy credentials, shell hooks, or
    arbitrary user environment values.  OAuth is passed only to the Claude
    child that needs it and is never put in argv or an error message.
    """

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _safe_account(value: Any) -> dict[str, Any] | None:
    """Allowlist account metadata; never expose provider credential fields."""

    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in ("type", "email", "planType", "plan_type"):
        item = value.get(key)
        if isinstance(item, (str, type(None))):
            result[key] = item
    return result or None


def _safe_models(value: Any) -> list[dict[str, Any]]:
    """Return the small, public model projection advertised by app-server."""

    if not isinstance(value, list):
        return []
    models: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        model_id = raw.get("id") or raw.get("model")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        item: dict[str, Any] = {"id": model_id.strip()}
        for key in ("displayName", "name", "defaultReasoningEffort"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                item[key] = value.strip()
        efforts = raw.get("supportedReasoningEfforts")
        if isinstance(efforts, list):
            safe_efforts: list[dict[str, str]] = []
            for effort in efforts:
                if not isinstance(effort, Mapping):
                    continue
                name = effort.get("reasoningEffort")
                if not isinstance(name, str) or name not in SUBSCRIPTION_REASONING_LEVELS:
                    continue
                entry = {"reasoningEffort": name}
                description = effort.get("description")
                if isinstance(description, str) and description.strip():
                    entry["description"] = description.strip()
                safe_efforts.append(entry)
            if safe_efforts:
                item["supportedReasoningEfforts"] = safe_efforts
        if raw.get("isDefault") is True:
            item["isDefault"] = True
        models.append(item)
    return models


class _JSONLProcess:
    """Small synchronous JSONL process transport used from worker threads."""

    def __init__(self, argv: list[str], env: Mapping[str, str], timeout: float, *, cwd: Path | None = None):
        self.timeout = max(1.0, float(timeout))
        try:
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                cwd=str(cwd or Path.cwd()),
                bufsize=0,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            raise SubscriptionRunnerError("Subscription executable could not be started") from exc
        if self.process.stdin is None or self.process.stdout is None:
            self.close()
            raise SubscriptionRunnerError("Subscription executable did not expose stdio")
        self._write_lock = threading.Lock()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ)

    def send(self, payload: Mapping[str, Any]) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
            raise SubscriptionRunnerError("Subscription protocol request was too large")
        with self._write_lock:
            if self.process.poll() is not None or self.process.stdin is None:
                raise SubscriptionRunnerError("Subscription executable exited unexpectedly")
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise SubscriptionRunnerError("Subscription executable closed its input") from exc

    def read(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout if timeout is None else max(0.01, timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProtocolTimeout("Subscription provider timed out")
            events = self._selector.select(remaining)
            if not events:
                if self.process.poll() is not None:
                    raise SubscriptionRunnerError("Subscription executable exited unexpectedly")
                raise _ProtocolTimeout("Subscription provider timed out")
            try:
                line = self.process.stdout.readline() if self.process.stdout else b""
            except OSError as exc:
                raise SubscriptionRunnerError("Subscription protocol read failed") from exc
            if not line:
                raise SubscriptionRunnerError("Subscription executable closed its output")
            if len(line) > MAX_PROTOCOL_LINE_BYTES:
                raise SubscriptionRunnerError("Subscription protocol response was too large")
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SubscriptionRunnerError("Subscription executable returned invalid protocol data") from exc
            if not isinstance(value, dict):
                raise SubscriptionRunnerError("Subscription protocol message was not an object")
            return value

    def response(self, request_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout if timeout is None else max(0.01, timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProtocolTimeout("Subscription provider timed out")
            message = self.read(remaining)
            if message.get("id") == request_id:
                error = message.get("error")
                if error is not None:
                    raise SubscriptionRunnerError("Subscription provider rejected the request")
                return message

    def close(self) -> None:
        try:
            self._selector.close()
        except Exception:
            pass
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def __enter__(self) -> "_JSONLProcess":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _check_response(message: Mapping[str, Any], request_id: int) -> dict[str, Any]:
    if message.get("id") != request_id:
        raise SubscriptionRunnerError("Subscription protocol response id did not match")
    error = message.get("error")
    if error is not None:
        raise SubscriptionRunnerError("Subscription provider rejected the request")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


def _codex_initialize(rpc: _JSONLProcess) -> None:
    rpc.send(
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "bookward_shadow",
                    "title": "Bookward shadow ranking",
                    "version": "1",
                }
            },
        }
    )
    response = rpc.response(1)
    _check_response(response, 1)
    rpc.send({"method": "initialized", "params": {}})


def _text_from_item(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    direct = item.get("text")
    if isinstance(direct, str):
        return direct
    content = item.get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return ""


def _reject_server_request(rpc: _JSONLProcess, message: Mapping[str, Any]) -> None:
    request_id = message.get("id")
    if isinstance(request_id, (int, str)) and not isinstance(request_id, bool):
        rpc.send(
            {
                "id": request_id,
                "error": {"code": -32000, "message": "Tools are disabled for shadow ranking"},
            }
        )


def _normalize_reasoning_effort(value: str | None) -> str:
    effort = (value or "medium").strip().lower()
    return effort if effort in SUBSCRIPTION_REASONING_LEVELS else "medium"


def _codex_failure_detail(value: Mapping[str, Any]) -> str:
    """Extract a bounded provider diagnostic without persisting credentials."""

    raw: Any = value.get("error")
    if isinstance(raw, Mapping):
        raw = raw.get("message") or raw.get("code")
    if raw is None:
        raw = value.get("message") or value.get("status")
    detail = str(raw or "unknown provider error").replace("\n", " ").strip()
    return detail[:800]


class CodexSubscriptionClient(BaseLLMClient):
    """Run one ephemeral JSON-only turn through ``codex app-server``."""

    def __init__(self, model: str, *, reasoning_effort: str | None = None, timeout: float | None = None, command: str | None = None, codex_home: str | None = None):
        home = _private_dir(codex_home or settings.llm_codex_home)
        self.codex_home = home
        self.command = _command(command or settings.llm_codex_command, "codex")
        self.reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
        super().__init__("", model, "", timeout=timeout or settings.llm_subscription_timeout_seconds)

    def _argv(self) -> list[str]:
        return [self.command, "app-server", "--listen", "stdio://"]

    def _rank_sync(self, prompt: str, candidate_ids: set[int]) -> LLMResult:
        env = _safe_env(home=self.codex_home)
        with _JSONLProcess(self._argv(), env, self.timeout, cwd=self.codex_home) as rpc:
            _codex_initialize(rpc)
            rpc.send(
                {
                    "method": "thread/start",
                    "id": 2,
                    "params": {
                        "model": self.model,
                        "ephemeral": True,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "serviceName": "bookward_shadow",
                    },
                }
            )
            thread = _check_response(rpc.response(2), 2).get("thread")
            thread_id = thread.get("id") if isinstance(thread, Mapping) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise SubscriptionRunnerError("Codex app-server did not return a thread")
            rpc.send(
                {
                    "method": "turn/start",
                    "id": 3,
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "outputSchema": RANKING_SCHEMA,
                        "approvalPolicy": "never",
                        "effort": self.reasoning_effort,
                    },
                }
            )
            deltas: list[str] = []
            completed_items: list[str] = []
            turn_response: dict[str, Any] | None = None
            while True:
                message = rpc.read()
                method = message.get("method")
                if method == "error":
                    params = message.get("params")
                    detail = _codex_failure_detail(params.get("error") if isinstance(params, Mapping) and isinstance(params.get("error"), Mapping) else (params if isinstance(params, Mapping) else {}))
                    raise SubscriptionRunnerError(f"Codex app-server turn failed: {detail}")
                if method in {"item/tool/call", "item/commandExecution/request", "item/mcpToolCall"}:
                    _reject_server_request(rpc, message)
                    raise SubscriptionRunnerError("Codex app-server attempted to use a tool")
                if method == "item/agentMessage/delta":
                    params = message.get("params")
                    if isinstance(params, Mapping) and isinstance(params.get("delta"), str):
                        deltas.append(params["delta"])
                elif method == "item/completed":
                    params = message.get("params")
                    item = params.get("item") if isinstance(params, Mapping) else None
                    text = _text_from_item(item)
                    if text:
                        completed_items.append(text)
                elif message.get("id") == 3:
                    turn_response = message
                    turn = message.get("result", {}).get("turn") if isinstance(message.get("result"), Mapping) else None
                    if isinstance(turn, Mapping) and turn.get("status") in {"completed", "failed", "interrupted"}:
                        if turn.get("status") != "completed":
                            raise SubscriptionRunnerError(f"Codex app-server turn failed: {_codex_failure_detail(turn)}")
                        break
                elif method == "turn/completed":
                    params = message.get("params")
                    turn = params.get("turn") if isinstance(params, Mapping) else None
                    if isinstance(turn, Mapping) and turn.get("status") not in {None, "completed"}:
                        detail = _codex_failure_detail(turn)
                        raise SubscriptionRunnerError(f"Codex app-server turn failed: {detail}")
                    break
            if turn_response and turn_response.get("error") is not None:
                raise SubscriptionRunnerError("Codex app-server rejected the turn")
            output = "".join(deltas) if deltas else "".join(completed_items)
            if not output:
                raise SubscriptionRunnerError("Codex app-server returned no ranking output")
            payload = json.loads(output)
            return LLMResult(validate_rankings(payload, candidate_ids))

    async def rank(self, prompt: str, candidate_ids: set[int]) -> LLMResult:
        return await asyncio.to_thread(self._rank_sync, prompt, candidate_ids)


class ClaudeCodeSubscriptionClient(BaseLLMClient):
    """Run Claude Code in headless JSON mode with OAuth supplied in env."""

    def __init__(self, model: str, oauth_token: str, *, reasoning_effort: str | None = None, timeout: float | None = None, command: str | None = None, config_dir: str | None = None):
        if not oauth_token or "\x00" in oauth_token:
            raise ValueError("Claude Code OAuth token is required")
        self.oauth_token = oauth_token
        self.command = _command(command or settings.llm_claude_command, "claude")
        self.reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
        # Keep the CLI's private config separate from a developer's interactive
        # Claude profile; only this runner's OAuth env token is used.
        self.config_dir = _private_dir(config_dir or str(Path(settings.db).with_name("claude-config")))
        super().__init__("", model, "", timeout=timeout or settings.llm_subscription_timeout_seconds)

    def _argv(self, prompt: str) -> list[str]:
        return [
            self.command,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--bare",
            "--tools",
            "",
            "--model",
            self.model,
            "--effort",
            self.reasoning_effort,
            "--json-schema",
            json.dumps(RANKING_SCHEMA, ensure_ascii=False, separators=(",", ":")),
            "--",
            prompt,
        ]

    def _rank_sync(self, prompt: str, candidate_ids: set[int]) -> LLMResult:
        env = _safe_env(home=self.config_dir, token=self.oauth_token, config_dir=self.config_dir)
        argv = self._argv(prompt)
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(self.config_dir),
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            raise SubscriptionRunnerError("Claude Code executable could not be started") from exc
        try:
            try:
                stdout, _ = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                _terminate_process(process)
                raise _ProtocolTimeout("Claude Code timed out") from exc
            if len(stdout) > MAX_CLAUDE_OUTPUT_BYTES:
                raise SubscriptionRunnerError("Claude Code response was too large")
            if process.returncode != 0:
                raise SubscriptionRunnerError("Claude Code returned an error")
            try:
                data = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SubscriptionRunnerError("Claude Code returned invalid JSON") from exc
            if not isinstance(data, Mapping):
                raise SubscriptionRunnerError("Claude Code returned a non-object response")
            payload: Any = data.get("structured_output")
            if payload is None:
                payload = data.get("result")
            if payload is None and "rankings" in data:
                payload = data
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise SubscriptionRunnerError("Claude Code returned invalid ranking JSON") from exc
            usage = data.get("usage")
            input_tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
            output_tokens = usage.get("output_tokens") if isinstance(usage, Mapping) else None
            return LLMResult(
                validate_rankings(payload, candidate_ids),
                int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
                int(output_tokens) if isinstance(output_tokens, (int, float)) else None,
            )
        finally:
            if process.poll() is None:
                _terminate_process(process)

    async def rank(self, prompt: str, candidate_ids: set[int]) -> LLMResult:
        return await asyncio.to_thread(self._rank_sync, prompt, candidate_ids)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


@dataclass
class _LoginState:
    login_id: str
    process: _JSONLProcess
    verification_url: str = ""
    user_code: str = ""
    status: str = "pending"
    error: str | None = None
    account: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)


class CodexDeviceLoginManager:
    """Own pending device-code processes and expose token-free status data."""

    def __init__(self, *, command: str | None = None, codex_home: str | None = None, timeout: float | None = None):
        self.codex_home = _private_dir(codex_home or settings.llm_codex_home)
        self.command = _command(command or settings.llm_codex_command, "codex")
        self.timeout = max(1.0, float(timeout or settings.llm_subscription_timeout_seconds))
        self._lock = threading.RLock()
        self._pending: dict[str, _LoginState] = {}

    def _spawn(self) -> _JSONLProcess:
        return _JSONLProcess(
            [self.command, "app-server", "--listen", "stdio://"],
            _safe_env(home=self.codex_home),
            self.timeout,
            cwd=self.codex_home,
        )

    def start(self) -> dict[str, Any]:
        with self._lock:
            for state in self._pending.values():
                if state.status == "pending":
                    return self._public(state)
            rpc = self._spawn()
            try:
                _codex_initialize(rpc)
                rpc.send({"method": "account/login/start", "id": 2, "params": {"type": "chatgptDeviceCode"}})
                result = _check_response(rpc.response(2), 2)
                login_id = result.get("loginId")
                if not isinstance(login_id, str) or not login_id:
                    raise SubscriptionRunnerError("Codex app-server did not return a login id")
                state = _LoginState(
                    login_id=login_id,
                    process=rpc,
                    verification_url=str(result.get("verificationUrl") or ""),
                    user_code=str(result.get("userCode") or ""),
                )
                self._pending[login_id] = state
                threading.Thread(target=self._watch, args=(state,), daemon=True).start()
                return self._public(state)
            except Exception:
                rpc.close()
                raise

    def _watch(self, state: _LoginState) -> None:
        try:
            while state.status == "pending":
                try:
                    message = state.process.read(0.5)
                except _ProtocolTimeout:
                    if state.process.process.poll() is not None:
                        if state.status == "pending":
                            state.status = "failed"
                            state.error = "Codex app-server exited during login"
                        break
                    continue
                method = message.get("method")
                params = message.get("params")
                if method == "account/login/completed" and isinstance(params, Mapping):
                    if params.get("loginId") in {None, state.login_id}:
                        if params.get("success") is True:
                            state.status = "authenticated"
                        else:
                            state.status = "failed"
                            state.error = "Codex device login failed"
                elif method == "account/updated" and isinstance(params, Mapping):
                    state.account = _safe_account(params)
            if state.status != "pending":
                state.process.close()
        except SubscriptionRunnerError:
            if state.status == "pending":
                state.status = "failed"
                state.error = "Codex app-server login protocol failed"
            state.process.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            pending = [state for state in self._pending.values() if state.status == "pending"]
            if pending:
                return self._public(pending[-1])
        account: dict[str, Any] | None = None
        requires_auth: bool | None = None
        try:
            with self._spawn() as rpc:
                _codex_initialize(rpc)
                rpc.send({"method": "account/read", "id": 2, "params": {"refreshToken": False}})
                result = _check_response(rpc.response(2), 2)
                account = _safe_account(result.get("account"))
                requires_auth = result.get("requiresOpenaiAuth") if isinstance(result.get("requiresOpenaiAuth"), bool) else None
        except SubscriptionRunnerError:
            return {"status": "unavailable", "authenticated": False}
        return {
            "status": "authenticated" if account else "signed_out",
            "authenticated": bool(account),
            "account": account,
            "requires_openai_auth": requires_auth,
        }

    def models(self) -> list[dict[str, Any]]:
        """Read the models and reasoning levels available to this account."""

        with self._spawn() as rpc:
            _codex_initialize(rpc)
            rpc.send(
                {
                    "method": "model/list",
                    "id": 2,
                    "params": {"includeHidden": False, "limit": 100},
                }
            )
            result = _check_response(rpc.response(2), 2)
            return _safe_models(result.get("data"))

    def cancel(self, login_id: str) -> dict[str, Any]:
        if not login_id or len(login_id) > 200:
            raise ValueError("A valid login id is required")
        with self._lock:
            state = self._pending.get(login_id)
            if state is None or state.status != "pending":
                raise KeyError(login_id)
            try:
                state.process.send({"method": "account/login/cancel", "id": 3, "params": {"loginId": login_id}})
                state.process.response(3, timeout=min(10.0, self.timeout))
            except SubscriptionRunnerError:
                # A process that already completed is still safe to clean up;
                # the API reports the resulting terminal state below.
                pass
            state.status = "cancelled"
            state.process.close()
            return self._public(state)

    def logout(self) -> dict[str, Any]:
        with self._lock:
            for state in list(self._pending.values()):
                state.process.close()
                state.status = "cancelled"
            self._pending.clear()
        try:
            with self._spawn() as rpc:
                _codex_initialize(rpc)
                rpc.send({"method": "account/logout", "id": 2})
                _check_response(rpc.response(2), 2)
        except SubscriptionRunnerError:
            return {"status": "unavailable"}
        return {"status": "signed_out", "authenticated": False}

    @staticmethod
    def _public(state: _LoginState) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": state.status,
            "authenticated": state.status == "authenticated",
            "login_id": state.login_id,
            "verification_url": state.verification_url,
            "user_code": state.user_code,
        }
        if state.account:
            result["account"] = state.account
        if state.error:
            result["error"] = state.error
        return result


_LOGIN_MANAGERS: dict[str, CodexDeviceLoginManager] = {}
_LOGIN_MANAGERS_LOCK = threading.Lock()


def codex_login_manager() -> CodexDeviceLoginManager:
    key = str(Path(settings.llm_codex_home).expanduser())
    with _LOGIN_MANAGERS_LOCK:
        manager = _LOGIN_MANAGERS.get(key)
        if manager is None:
            manager = CodexDeviceLoginManager()
            _LOGIN_MANAGERS[key] = manager
        return manager
