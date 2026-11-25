"""StepRunner: the one place that talks to an LLM. Swap implementations without touching chat logic."""
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol

from dotenv import load_dotenv

from app.config import REPO_ROOT, Settings, api_key_configured


@dataclass(frozen=True)
class RunInput:
    system: str
    documents: str  # pre-rendered, untrusted document text; may be empty
    turns: list[tuple[str, str]]  # (role, text), oldest first; roles are "user" | "assistant"
    wants_deliverable: bool = False
    skill_name: str = ""  # set when running one skill standalone
    artifact_types: tuple[str, ...] = ()  # discovery runs: one "=== ARTIFACT: TYPE ===" section per type


class RunnerError(Exception):
    """Raised for failures the user should see (missing key, API error, CLI failure)."""


class StepRunner(Protocol):
    name: str

    def stream(self, run: RunInput) -> Iterator[str]: ...


def merge_turns(run: RunInput) -> list[tuple[str, str]]:
    """Merge consecutive same-role turns and make sure the conversation starts with a user turn."""
    merged: list[list] = []
    for role, text in run.turns:
        if merged and merged[-1][0] == role:
            merged[-1][1] += "\n\n" + text
        else:
            merged.append([role, text])
    while merged and merged[0][0] != "user":
        merged.pop(0)
    if not merged:
        raise RunnerError("Nothing to send: the conversation has no user message.")
    return [(role, text) for role, text in merged]


def to_api_messages(run: RunInput) -> list[dict]:
    """API format: merged turns, documents first in a cacheable block."""
    merged = merge_turns(run)
    messages: list[dict] = [{"role": role, "content": text} for role, text in merged]
    if run.documents:
        messages[0]["content"] = [
            {"type": "text", "text": run.documents, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": merged[0][1]},
        ]
    return messages


class StubRunner:
    """Deterministic, offline. Used by tests and when no API key is configured. Not derived from the documents."""

    name = "stub"

    def stream(self, run: RunInput) -> Iterator[str]:
        names = [line.split('"')[1] for line in run.documents.splitlines() if line.startswith("<document ")]
        header = "> **Stub runner** — no API key is configured, so this text is scripted and is *not* based on your documents.\n\n"
        if run.artifact_types:
            body = "".join(f"=== ARTIFACT: {t} ===\n# {t} (stub from {run.skill_name})\n\nDocuments considered: "
                           f"{', '.join(names) or '(none)'}.\n\n" for t in run.artifact_types)
            yield body  # no header: discovery output must start with the first marker
            return
        if run.skill_name:
            body = f"# Skill run (stub): {run.skill_name}\n\n## Documents considered\n" + (
                "\n".join(f"- {n}" for n in names) or "- (none)") + "\n\n## Next step\nAdd your key to `.env` to run this skill for real.\n"
        elif run.wants_deliverable:
            body = "# Draft deliverable (stub)\n\n## Documents considered\n" + (
                "\n".join(f"- {n}" for n in names) or "- (none)") + "\n\n## Next step\nSet `ANTHROPIC_API_KEY` in `.env` and restart to generate a real draft.\n"
        else:
            body = f"Stub reply. I can see {len(names)} document(s) in this session's context."
        for chunk in (header + body).splitlines(keepends=True):
            yield chunk


class AnthropicRunner:
    name = "api"
    BETA_FALLBACK = "server-side-fallback-2026-07-01"

    def __init__(self, model: str):
        self.model = model

    def stream(self, run: RunInput) -> Iterator[str]:
        import anthropic

        try:
            client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / profile from the environment
        except Exception as exc:
            raise RunnerError(f"Claude client could not start: {exc}") from exc

        try:
            with client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=run.system,
                messages=to_api_messages(run),
                betas=[self.BETA_FALLBACK],
                extra_body={"fallbacks": "default"},  # re-run on a fallback model if safety classifiers decline
            ) as stream:
                yield from stream.text_stream
                final = stream.get_final_message()
        except TypeError as exc:  # SDK raises this when no credentials resolve
            raise RunnerError("No Anthropic credentials found. Set ANTHROPIC_API_KEY in .env and restart.") from exc
        except anthropic.AuthenticationError as exc:
            raise RunnerError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.") from exc
        except anthropic.RateLimitError as exc:
            raise RunnerError("Rate limited by the Anthropic API. Wait a moment and try again.") from exc
        except anthropic.APIConnectionError as exc:
            raise RunnerError("Could not reach the Anthropic API. Check your network connection.") from exc
        except anthropic.APIStatusError as exc:
            raise RunnerError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        if final.stop_reason == "refusal":
            raise RunnerError("The model declined this request (safety classifier). Try rewording or removing sensitive content.")
        if final.stop_reason == "max_tokens":
            yield "\n\n_[Output hit the length limit and was cut off. Ask me to continue.]_"


_login_cache: tuple[float, bool] = (0.0, False)


def cli_logged_in(executable: Optional[str] = None) -> bool:
    """True when the Claude Code CLI is installed and logged in (claude.ai subscription). Cached for 60s."""
    global _login_cache
    checked_at, value = _login_cache
    if time.monotonic() - checked_at < 60:
        return value
    exe = executable or shutil.which("claude")
    ok = False
    if exe:
        try:
            out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=20, encoding="utf-8")
            ok = bool(json.loads(out.stdout).get("loggedIn"))
        except (OSError, subprocess.SubprocessError, ValueError):
            ok = False
    _login_cache = (time.monotonic(), ok)
    return ok


class CliRunner:
    """Runs prompts through the `claude` CLI (headless), billed to the user's own Claude subscription — no API key.

    Isolated from the user's Claude Code setup: no tools, no hooks, no CLAUDE.md, no slash commands/skills, empty cwd.
    """

    name = "cli"  # the system prompt goes in a file: skills can exceed the Windows command-line length limit

    def __init__(self, settings: Settings, executable: Optional[str] = None):
        self.workdir = settings.data_dir / "cli-workdir"
        self.executable = executable or shutil.which("claude")
        self.model = os.environ.get("CLI_MODEL") or None  # optional alias such as "opus" or "sonnet"

    @staticmethod
    def build_prompt(run: RunInput) -> str:
        turns = merge_turns(run)
        parts = [run.documents] if run.documents else []
        parts.append("# Conversation\n\n" + "\n\n".join(
            f"[{'User' if role == 'user' else 'Assistant'}]\n{text}" for role, text in turns))
        parts.append("Respond now to the last [User] message above. Output only your reply, with no preamble about this format.")
        return "\n\n".join(parts)

    def stream(self, run: RunInput) -> Iterator[str]:
        if not self.executable:
            raise RunnerError("The Claude Code CLI (`claude`) was not found. Install it, then run `claude` and sign in with /login.")
        self.workdir.mkdir(parents=True, exist_ok=True)
        prompt_file = self.workdir / f"system-{uuid.uuid4().hex}.md"
        prompt_file.write_text(run.system, encoding="utf-8")
        cmd = [self.executable, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--tools", "", "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "project",
               "--system-prompt-file", str(prompt_file)]
        if self.model:
            cmd += ["--model", self.model]
        # Strip API credentials so the CLI uses the logged-in subscription, and keep the user's CLAUDE.md out of the run.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    cwd=self.workdir, env=env, text=True, encoding="utf-8")
        except OSError as exc:
            prompt_file.unlink(missing_ok=True)
            raise RunnerError(f"Could not start the Claude Code CLI: {exc}") from exc

        streamed, final_text, error, stray = False, "", None, []
        try:
            proc.stdin.write(self.build_prompt(run))
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    stray.append(line.strip())
                    continue
                kind = event.get("type")
                if kind == "stream_event":
                    inner = event.get("event", {})
                    delta = inner.get("delta", {})
                    if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        streamed = True
                        yield delta["text"]
                elif kind == "result":
                    final_text = event.get("result") or ""
                    if event.get("is_error"):
                        error = final_text or "the CLI reported an error"
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()  # the client disconnected or the generator was closed early
            prompt_file.unlink(missing_ok=True)

        if error or (proc.returncode not in (0, None) and not streamed):
            detail = error or " ".join(stray[-3:]) or f"exit code {proc.returncode}"
            hint = " Run `claude` in a terminal and sign in with /login." if "login" in detail.lower() else ""
            raise RunnerError(f"Claude Code CLI failed: {detail}{hint}")
        if not streamed and final_text:
            yield final_text


class AutoRunner:
    """Picks the best available runner on every call (re-reading .env): API key → Claude account via CLI → stub.
    Saving a key or logging in takes effect without restarting the API."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def current(self) -> StepRunner:
        load_dotenv(REPO_ROOT / ".env", override=True)
        if api_key_configured():
            return AnthropicRunner(os.environ.get("MODEL") or self.settings.model)
        if cli_logged_in():
            return CliRunner(self.settings)
        return StubRunner()

    @property
    def name(self) -> str:
        return self.current().name

    def stream(self, run: RunInput) -> Iterator[str]:
        return self.current().stream(run)


def get_runner(settings: Settings) -> StepRunner:
    if settings.runner == "auto":
        return AutoRunner(settings)
    if settings.runner == "api":
        return AnthropicRunner(settings.model)
    if settings.runner == "cli":
        return CliRunner(settings)
    return StubRunner()
