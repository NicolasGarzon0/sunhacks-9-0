"""HaikuExtractor request shape and response parsing, with the Anthropic client faked."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2

from extraction import PROPOSE_BET_TOOL, HaikuExtractor
from extraction.prompt import build_system_prompt

TODAY = date(2026, 11, 14)
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def tool_response(input_: dict, stop_reason: str = "tool_use"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="tool_use", name="propose_bet", id="t1", input=input_),
        ],
    )


FULL = {"classification": "bet_proposal", "team": "ASU", "opponent": "Texas Tech", "date": "2026-11-14"}


class FakeMessages:
    def __init__(self, result=None, exc: BaseException | None = None, delay: float = 0):
        self.result, self.exc, self.delay, self.kwargs = result, exc, delay, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def make(logs, messages: FakeMessages, timeout: float = 10.0) -> HaikuExtractor:
    return HaikuExtractor("k", "claude-haiku-5-5", logs.logger, timeout=timeout, client=SimpleNamespace(messages=messages))


async def test_request_shape(logs):
    m = FakeMessages(tool_response(FULL))
    await make(logs, m).extract("create a bet for ASU vs Texas Tech", TODAY, "America/Phoenix")
    k = m.kwargs
    assert k["model"] == "claude-haiku-5-5"
    assert k["tools"] == [PROPOSE_BET_TOOL] and PROPOSE_BET_TOOL["strict"] is True
    assert k["tool_choice"] == {"type": "tool", "name": "propose_bet"}
    assert k["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k"} & set(k)
    assert k["messages"] == [{"role": "user", "content": "create a bet for ASU vs Texas Tech"}]
    schema = PROPOSE_BET_TOOL["input_schema"]
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


async def test_parses_tool_input_skipping_thinking_block(logs):
    r = await make(logs, FakeMessages(tool_response(FULL))).extract("x", TODAY, "America/Phoenix")
    assert r.classification == "bet_proposal" and r.team == "ASU" and r.opponent == "Texas Tech" and r.date == TODAY
    assert r.fields() == {"team": "ASU", "opponent": "Texas Tech", "date": "2026-11-14"}


def test_schema_is_game_only():
    props = PROPOSE_BET_TOOL["input_schema"]["properties"]
    assert set(props) == {"classification", "team", "opponent", "date"}


def test_prompt_has_today_timezone_calendar_and_pending():
    p = build_system_prompt(TODAY, "America/Phoenix", "team")
    assert "Today is Saturday 2026-11-14 in America/Phoenix" in p
    assert "Sunday 2026-11-15 (tomorrow)" in p and "Saturday 2026-11-28" in p
    assert "field `team`" in p and "Ignore amounts" in p
    assert "No bet request from this sender is waiting" in build_system_prompt(TODAY, "America/Phoenix", None)


async def test_api_errors_return_none(logs):
    resp = httpx2.Response(500, request=REQ)
    for exc in (
        anthropic.APIConnectionError(request=REQ),
        anthropic.APITimeoutError(request=REQ),
        anthropic.InternalServerError("boom", response=resp, body=None),
        anthropic.RateLimitError("slow", response=httpx2.Response(429, request=REQ), body=None),
        ValueError("unexpected"),
    ):
        assert await make(logs, FakeMessages(exc=exc)).extract("x", TODAY, "America/Phoenix") is None


async def test_timeout_returns_none(logs):
    ex = make(logs, FakeMessages(tool_response(FULL), delay=0.5), timeout=-4.8)  # wait_for timeout = 0.2s
    assert await ex.extract("x", TODAY, "America/Phoenix") is None
    assert logs.events("extract.unreachable")


async def test_refusal_no_tool_and_invalid_output_return_none(logs):
    assert await make(logs, FakeMessages(tool_response(FULL, "refusal"))).extract("x", TODAY, "t") is None
    no_tool = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="hi")])
    assert await make(logs, FakeMessages(no_tool)).extract("x", TODAY, "t") is None
    bad = tool_response({**FULL, "classification": "maybe"})
    assert await make(logs, FakeMessages(bad)).extract("x", TODAY, "t") is None
    bad_date = tool_response({**FULL, "date": "next saturday"})
    assert await make(logs, FakeMessages(bad_date)).extract("x", TODAY, "t") is None


async def test_input_cap_skips_api(logs):
    m = FakeMessages(tool_response(FULL))
    assert await make(logs, m).extract("x" * 501, TODAY, "t") is None
    assert m.kwargs is None


async def test_logs_have_no_values(logs):
    await make(logs, FakeMessages(tool_response(FULL))).extract("secret text", TODAY, "t")
    blob = "\n".join(logs.lines)
    assert "Texas Tech" not in blob and "ASU" not in blob and "secret text" not in blob
    assert logs.events("extract.ok")[0]["has_opponent"] is True
