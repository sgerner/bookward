import asyncio
import json
import os
from pathlib import Path

from afterword_engine.llm_subscriptions import (
    ClaudeCodeSubscriptionClient,
    CodexDeviceLoginManager,
    CodexSubscriptionClient,
)


RANKING = {
    "rankings": [
        {"candidate_id": 1, "score": 88, "confidence": 0.9, "reason_codes": ["fit"]},
        {"candidate_id": 2, "score": 31, "confidence": 0.4, "reason_codes": []},
    ]
}


def _executable(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-provider"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o700)
    return str(path)


def test_codex_app_server_jsonl_rank_is_ephemeral_and_tool_free(tmp_path):
    executable = _executable(
        tmp_path,
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif method == 'thread/start':
        assert message['params']['ephemeral'] is True
        assert message['params']['approvalPolicy'] == 'never'
        assert message['params']['sandbox'] == 'read-only'
        print(json.dumps({'id': message['id'], 'result': {'thread': {'id': 'thr_fake'}}}), flush=True)
    elif method == 'turn/start':
        assert 'tools' not in message['params']
        print(json.dumps({'id': message['id'], 'result': {'turn': {'status': 'inProgress'}}}), flush=True)
        print(json.dumps({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': json.dumps(%r)}}}), flush=True)
        print(json.dumps({'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}), flush=True)
""" % RANKING,
    )
    client = CodexSubscriptionClient("gpt-subscription", command=executable, codex_home=str(tmp_path / "codex"), timeout=3)
    result = asyncio.run(client.rank("rank these", {1, 2}))
    assert [item["candidate_id"] for item in result.rankings] == [1, 2]
    assert (tmp_path / "codex").stat().st_mode & 0o777 == 0o700


def test_codex_device_login_manager_keeps_login_token_free(tmp_path):
    executable = _executable(
        tmp_path,
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif method == 'account/login/start':
        print(json.dumps({'id': message['id'], 'result': {'type': 'chatgptDeviceCode', 'loginId': 'login_fake', 'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'ABCD-1234'}}), flush=True)
    elif method == 'account/login/cancel':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
""",
    )
    manager = CodexDeviceLoginManager(command=executable, codex_home=str(tmp_path / "codex"), timeout=3)
    started = manager.start()
    assert started["status"] == "pending"
    assert started["user_code"] == "ABCD-1234"
    cancelled = manager.cancel("login_fake")
    assert cancelled["status"] == "cancelled"
    assert "token" not in json.dumps(started).lower()


def test_claude_code_runner_uses_oauth_env_tools_off_and_no_sessions(tmp_path):
    token_file = tmp_path / "oauth-token"
    executable = _executable(
        tmp_path,
        """
import json, os, sys
from pathlib import Path
Path(%s).write_text(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''))
assert '--no-session-persistence' in sys.argv
assert '--tools' in sys.argv and sys.argv[sys.argv.index('--tools') + 1] == ''
print(json.dumps({'structured_output': %r}))
""" % (repr(str(token_file)), RANKING),
    )
    client = ClaudeCodeSubscriptionClient("claude-subscription", "oauth-secret", command=executable, config_dir=str(tmp_path / "claude"), timeout=3)
    result = asyncio.run(client.rank("rank these", {1, 2}))
    assert result.rankings[0]["candidate_id"] == 1
    assert token_file.read_text() == "oauth-secret"
