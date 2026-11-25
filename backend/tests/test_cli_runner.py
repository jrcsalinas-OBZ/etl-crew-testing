import io
import json

import pytest

import app.services.runner as runner_module
from app.services.runner import AutoRunner, AnthropicRunner, CliRunner, RunInput, RunnerError, StubRunner

RUN = RunInput(system="SYSTEM TEXT", documents='<document name="a.sql">SELECT 1</document>',
               turns=[("user", "hello"), ("assistant", "hi"), ("user", "summarise")])


def delta(text):
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                         "delta": {"type": "text_delta", "text": text}}}) + "\n"


def result(text, is_error=False):
    return json.dumps({"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error, "result": text}) + "\n"


class FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdin, self.stdout, self.returncode = io.StringIO(), iter(lines), returncode
        self.stdin_text = ""
        self.stdin.close = lambda: setattr(self, "stdin_text", self.stdin.getvalue())  # keep the text readable after close

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


@pytest.fixture
def fake_popen(monkeypatch):
    calls = {}

    def install(lines, returncode=0):
        def popen(cmd, **kwargs):
            proc = FakeProc(lines, returncode)
            calls.update(cmd=cmd, kwargs=kwargs, proc=proc, system=open(cmd[cmd.index("--system-prompt-file") + 1], encoding="utf-8").read())
            return proc
        monkeypatch.setattr(runner_module.subprocess, "Popen", popen)
        return calls
    return install


def runner(settings):
    return CliRunner(settings, executable="claude")


def test_streams_text_deltas_and_isolates_the_cli(settings, fake_popen, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    calls = fake_popen([json.dumps({"type": "system"}) + "\n", delta("Hello "), delta("world"), result("Hello world")])
    assert "".join(runner(settings).stream(RUN)) == "Hello world"

    cmd = calls["cmd"]
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--tools") + 1] == ""
    for flag in ("--disable-slash-commands", "--no-session-persistence", "--include-partial-messages"):
        assert flag in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    env = calls["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert calls["kwargs"]["cwd"] == settings.data_dir / "cli-workdir"
    assert calls["system"] == "SYSTEM TEXT"  # system prompt travels in a file, not on the command line
    assert "SYSTEM TEXT" not in " ".join(cmd)


def test_prompt_on_stdin_has_documents_and_whole_conversation(settings, fake_popen):
    calls = fake_popen([delta("ok"), result("ok")])
    list(runner(settings).stream(RUN))
    text = calls["proc"].stdin_text
    assert text.index("<document") < text.index("# Conversation")
    assert "[User]\nhello" in text and "[Assistant]\nhi" in text and "[User]\nsummarise" in text


def test_system_prompt_file_is_removed_afterwards(settings, fake_popen):
    fake_popen([delta("ok"), result("ok")])
    list(runner(settings).stream(RUN))
    assert list((settings.data_dir / "cli-workdir").glob("system-*.md")) == []


def test_falls_back_to_result_text_when_nothing_was_streamed(settings, fake_popen):
    fake_popen([result("whole answer")])
    assert "".join(runner(settings).stream(RUN)) == "whole answer"


def test_cli_error_result_becomes_a_runner_error_with_login_hint(settings, fake_popen):
    fake_popen([result("Not logged in · Please run /login", is_error=True)], returncode=1)
    with pytest.raises(RunnerError, match="Not logged in.*sign in with /login"):
        list(runner(settings).stream(RUN))


def test_usage_limit_message_is_passed_through(settings, fake_popen):
    fake_popen([result("You've hit your usage limit. Resets at 5pm.", is_error=True)], returncode=1)
    with pytest.raises(RunnerError, match="usage limit"):
        list(runner(settings).stream(RUN))


def test_missing_cli_is_a_clear_error(settings, monkeypatch):
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: None)
    with pytest.raises(RunnerError, match="not found"):
        list(CliRunner(settings).stream(RUN))


def test_autorunner_prefers_key_then_cli_then_stub(settings, monkeypatch):
    monkeypatch.setattr(runner_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    auto = AutoRunner(settings)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner_module, "cli_logged_in", lambda *a, **k: True)
    assert isinstance(auto.current(), CliRunner) and auto.name == "cli"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    assert isinstance(auto.current(), AnthropicRunner)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner_module, "cli_logged_in", lambda *a, **k: False)
    assert isinstance(auto.current(), StubRunner)


def test_login_check_reads_auth_status_json_and_caches(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"stdout": json.dumps({"loggedIn": True, "authMethod": "claude.ai"})})()

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module, "_login_cache", (0.0, False))
    assert runner_module.cli_logged_in("claude") is True
    assert runner_module.cli_logged_in("claude") is True
    assert calls == [["claude", "auth", "status"]]  # second call served from cache
