"""CLP-81 highlight selection through the Claude Code CLI (the Claude subscription). The
CLI is faked at the runner seam; its JSON shape is the one `claude -p --output-format json
--json-schema` printed in a probe of CLI 2.1.295."""

# ruff: noqa: F811 - the conn/media fixtures are reused from test_select

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from clipper.config import AppConfig, Settings
from clipper.jobs import Deferred, Job, PermanentError, StageContext
from clipper.pipeline import select
from clipper.pipeline.select import ARTIFACT, MODEL, OUTPUT_SCHEMA, InvalidModelOutput
from clipper.pipeline.ytdlp import CommandTimeout, RunResult
from test_select import (  # noqa: F401 - pytest fixtures
    T0,
    _add_spend,
    _ctx_at,
    _set_meta_duration,
    conn,
    fixture,
    make_ctx,
    make_job,
    media,
    write_transcript,
)

VALID = json.loads(fixture("valid.json")["content"][-1]["text"])
TOKEN = "sk-ant-oat01-test-token-value"


def cli_json(structured: Any = VALID, **over: Any) -> str:
    body = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "tool_use",
        "result": json.dumps(structured),
        "structured_output": structured,
        "api_error_status": None,
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 1116,
            "cache_read_input_tokens": 300,
            "output_tokens": 51,
        },
    }
    if structured is None:
        del body["structured_output"]
    return json.dumps({**body, **over})


def limit_json(result: str, status: int | None = 429) -> str:
    return cli_json(None, is_error=True, subtype="success", result=result, api_error_status=status)


class FakeCli:
    """Replays queued results (a stdout string, a RunResult or an exception) and records
    each call, including the stdin and system prompt files read while the call runs."""

    def __init__(self, *outputs: str | RunResult | BaseException) -> None:
        self.queue = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args, timeout_s, *, env=None, stdin_path=None, cwd=None, **_):
        args = list(args)
        system_file = Path(args[args.index("--system-prompt-file") + 1])
        self.calls.append(
            {
                "args": args,
                "timeout_s": timeout_s,
                "env": dict(env or {}),
                "stdin": Path(stdin_path).read_text(),
                "system": system_file.read_text(),
                "cwd": cwd,
                "cwd_listing": sorted(p.name for p in Path(cwd).iterdir()) if cwd else None,
            }
        )
        if not self.queue:
            raise AssertionError("unexpected extra CLI call")
        out = self.queue.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out if isinstance(out, RunResult) else RunResult(0, out, "")


@pytest.fixture(autouse=True)
def _no_ambient_secrets(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)


def cli_ctx(conn: sqlite3.Connection, media: Path, **settings: Any) -> StageContext:
    return make_ctx(conn, media, selector="claude_cli", **settings)


def run(job: Job, ctx: StageContext, fake: FakeCli) -> dict[str, Any]:
    select.select_highlights(job, ctx, runner=fake)
    return json.loads((ctx.media_dir / ARTIFACT).read_text())


def spend_rows(conn: sqlite3.Connection) -> list[tuple]:
    rows = conn.execute("SELECT provider, usd, input_tokens, output_tokens FROM spend ORDER BY id")
    return [tuple(row) for row in rows]


# --- AC1: highlights through the CLI, no API key ---------------------------------------------


def test_cli_selector_writes_highlights_without_an_api_key(conn, media):
    fake = FakeCli(cli_json())
    out = run(make_job(conn), cli_ctx(conn, media), fake)
    assert out["model"] == MODEL
    assert [c["score"] for c in out["clips"]] == [92, 88, 75]
    assert len(fake.calls) == 1


def test_api_stays_the_default_selector(conn, media):
    assert Settings().selector == "api"
    with pytest.raises(PermanentError, match="ANTHROPIC_API_KEY"):
        select.select_highlights(make_job(conn), make_ctx(conn, media), runner=FakeCli())


# --- AC2: isolated command, no tools, prompt on stdin, scrubbed env --------------------------


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def test_cli_command_is_isolated_and_has_no_tools(conn, media):
    fake = FakeCli(cli_json())
    run(make_job(conn), cli_ctx(conn, media), fake)
    call = fake.calls[0]
    args = call["args"]
    assert args[:2] == ["claude", "-p"]
    assert _flag(args, "--tools") == ""
    assert _flag(args, "--setting-sources") == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in args
    assert _flag(args, "--model") == MODEL
    assert _flag(args, "--output-format") == "json"
    assert json.loads(_flag(args, "--json-schema")) == OUTPUT_SCHEMA
    # System prompt: the same blocks the API path sends, joined.
    system = select.build_system("MrBeast", "en", ())
    assert call["system"] == "\n\n".join(block["text"] for block in system)
    # The untrusted transcript goes on stdin, never into argv.
    assert "<transcript>" in call["stdin"] and "w42" in call["stdin"]
    assert not any("w42" in arg for arg in args)
    # It runs in an empty scratch dir that is gone afterwards (only its own input files).
    assert call["cwd"] is not None and not Path(call["cwd"]).exists()
    assert set(call["cwd_listing"]) <= {"prompt.txt", "system.md"}


def test_cli_env_carries_only_the_subscription_token(conn, media, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    fake = FakeCli(cli_json())
    run(make_job(conn), cli_ctx(conn, media), fake)
    env = fake.calls[0]["env"]
    assert set(env) == {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    # A token login gets a throwaway config dir (the image's root fs is read-only).
    assert not Path(env["CLAUDE_CONFIG_DIR"]).exists()


def test_cli_without_a_token_uses_the_existing_login(conn, media):
    fake = FakeCli(cli_json())
    run(make_job(conn), cli_ctx(conn, media), fake)
    assert fake.calls[0]["env"] == {}


def test_cli_without_a_token_keeps_what_the_local_login_needs(conn, media, monkeypatch, tmp_path):
    # A login made with CLAUDE_CONFIG_DIR set lives there, and the macOS Keychain lookup
    # needs USER; without them the CLI answers "Not logged in" (seen in the live check).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-personal"))
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("LOGNAME", "someone")
    fake = FakeCli(cli_json())
    run(make_job(conn), cli_ctx(conn, media), fake)
    assert fake.calls[0]["env"] == {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-personal"),
        "USER": "someone",
        "LOGNAME": "someone",
    }


# --- AC3: limits, login, process failures ----------------------------------------------------


def test_usage_limit_with_reset_time_defers_until_then(conn, media):
    reset = T0 + timedelta(hours=3)
    fake = FakeCli(limit_json(f"Claude AI usage limit reached|{int(reset.timestamp())}"))
    with pytest.raises(Deferred, match="usage limit") as exc:
        run(make_job(conn), cli_ctx(conn, media), fake)
    assert exc.value.until == reset
    assert len(fake.calls) == 1
    assert spend_rows(conn) == []


def test_usage_limit_without_reset_time_defers_an_hour_then_gives_up(conn, media):
    job = make_job(conn)

    def at(when):
        ctx = _ctx_at(conn, media, when)
        return StageContext(
            ctx.media_dir,
            ctx.conn,
            AppConfig(settings=Settings(selector="claude_cli")),
            clock=lambda: when,
        )

    limit = limit_json("You've hit your limit · resets 3pm", status=None)
    with pytest.raises(Deferred) as exc:
        select.select_highlights(job, at(T0), runner=FakeCli(limit))
    assert exc.value.until == T0 + timedelta(hours=1)
    with pytest.raises(Deferred):
        select.select_highlights(job, at(T0 + timedelta(hours=47)), runner=FakeCli(limit))
    with pytest.raises(PermanentError, match="giving up"):
        select.select_highlights(job, at(T0 + timedelta(hours=49)), runner=FakeCli(limit))


@pytest.mark.parametrize("reset", [T0 - timedelta(hours=1), T0 + timedelta(days=30)])
def test_implausible_reset_time_falls_back_to_an_hour(conn, media, reset):
    fake = FakeCli(limit_json(f"Claude AI usage limit reached|{int(reset.timestamp())}"))
    with pytest.raises(Deferred) as exc:
        run(make_job(conn), cli_ctx(conn, media), fake)
    assert exc.value.until == T0 + timedelta(hours=1)


@pytest.mark.parametrize(
    ("status", "result"),
    [(401, "Invalid API key · Please run /login"), (None, "Not logged in · Please run /login")],
)
def test_rejected_login_is_permanent_with_a_setup_token_hint(conn, media, status, result):
    fake = FakeCli(limit_json(result, status=status))
    with pytest.raises(PermanentError, match="setup-token"):
        run(make_job(conn), cli_ctx(conn, media), fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        CommandTimeout("claude timed out after 600s"),
        RunResult(1, "", "boom: something broke"),
        RunResult(0, "not json at all", ""),
        RunResult(0, "[1, 2]", ""),
        cli_json(None, is_error=True, subtype="error_during_execution", result="overloaded"),
    ],
)
def test_process_failures_are_retryable(conn, media, outcome):
    with pytest.raises(RuntimeError) as exc:
        run(make_job(conn), cli_ctx(conn, media), FakeCli(outcome))
    assert not isinstance(exc.value, PermanentError)


def test_missing_cli_is_permanent_with_an_install_hint(conn, media):
    fake = FakeCli(FileNotFoundError(2, "No such file or directory", "claude"))
    with pytest.raises(PermanentError, match="claude CLI"):
        run(make_job(conn), cli_ctx(conn, media), fake)


def test_cli_error_text_is_redacted(conn, media, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    fake = FakeCli(RunResult(1, "", f"failed with token {TOKEN} and sk-ant-api03-abcdefghijklmnop"))
    with pytest.raises(RuntimeError) as exc:
        run(make_job(conn), cli_ctx(conn, media), fake)
    assert TOKEN not in str(exc.value) and "sk-ant-api03-abcdefghijklmnop" not in str(exc.value)


# --- AC4: invalid output retried once ---------------------------------------------------------


def test_missing_structured_output_is_retried_once(conn, media):
    fake = FakeCli(cli_json(None), cli_json())
    out = run(make_job(conn), cli_ctx(conn, media), fake)
    assert len(fake.calls) == 2 and len(out["clips"]) == 3


def test_invalid_output_twice_raises_invalid_model_output(conn, media):
    bad = {"clips": [{"start_s": 1, "end_s": 0}]}
    fake = FakeCli(cli_json(bad), cli_json(None))
    with pytest.raises(InvalidModelOutput):
        run(make_job(conn), cli_ctx(conn, media), fake)
    assert len(fake.calls) == 2


def test_refusal_counts_as_an_empty_window(conn, media):
    fake = FakeCli(cli_json(None, stop_reason="refusal", result=""))
    out = run(make_job(conn), cli_ctx(conn, media), fake)
    assert out["clips"] == [] and len(fake.calls) == 1


# --- AC5: spend and the window cache ----------------------------------------------------------


def test_spend_row_is_free_and_has_the_tokens(conn, media):
    run(make_job(conn), cli_ctx(conn, media), FakeCli(cli_json()))
    assert spend_rows(conn) == [("claude_cli", 0.0, 2 + 1116 + 300, 51)]


def test_billed_api_spend_does_not_block_the_cli(conn, media):
    _add_spend(conn, 100.0, T0)
    out = run(make_job(conn), cli_ctx(conn, media, daily_llm_usd_cap=1.0), FakeCli(cli_json()))
    assert len(out["clips"]) == 3


def test_retry_reuses_windows_done_through_the_cli(conn, media):
    write_transcript(media, seconds=3700)
    _set_meta_duration(media, 3700)
    job = make_job(conn, is_stream=True)
    first = FakeCli(cli_json(), cli_json(), CommandTimeout("claude timed out after 600s"))
    with pytest.raises(RuntimeError):
        run(job, cli_ctx(conn, media), first)
    second = FakeCli(cli_json())
    out = run(job, cli_ctx(conn, media), second)
    assert len(second.calls) == 1
    assert "Window: [58:00]" in second.calls[0]["stdin"]
    assert len(out["clips"]) == 3
