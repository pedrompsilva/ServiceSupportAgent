"""ClaudeTriage against a fake Anthropic client: forced tool_choice, the
fallback for models that reject it, and the retry when no tool call comes back."""
from types import SimpleNamespace

import pytest

from agent.triage import TOOL_NAME, ClaudeTriage

from conftest import triage_result

GOOD = triage_result().model_dump()


def tool_resp(data=GOOD):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=TOOL_NAME, input=data)])


def text_resp(text="I think it is a defect."):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(script):
    msgs = FakeMessages(script)
    return ClaudeTriage("k", "m", client=SimpleNamespace(messages=msgs)), msgs


def test_forced_tool_choice_used_when_supported(cfg, ticket):
    t, msgs = make([tool_resp()])
    assert t.triage(ticket, [], cfg).classification == "defect"
    assert msgs.calls[0]["tool_choice"] == {"type": "tool", "name": TOOL_NAME}


def test_falls_back_to_auto_and_remembers(cfg, ticket):
    err = Exception('Error code: 400 - tool_choice: type "tool" and "any" are not supported for this model.')
    t, msgs = make([err, tool_resp(), tool_resp()])
    t.triage(ticket, [], cfg)
    assert msgs.calls[1]["tool_choice"] == {"type": "auto"}
    t.triage(ticket, [], cfg)                       # second ticket: no forced attempt
    assert len(msgs.calls) == 3 and msgs.calls[2]["tool_choice"] == {"type": "auto"}


def test_retries_once_when_model_answers_in_text(cfg, ticket):
    err = Exception("tool_choice: type \"tool\" is not supported for this model")
    t, msgs = make([err, text_resp(), tool_resp()])
    assert t.triage(ticket, [], cfg).component == "Rewards & Incentives"
    assert msgs.calls[2]["messages"][-1]["role"] == "user"


def test_gives_up_after_retry(cfg, ticket):
    t, _ = make([text_resp(), text_resp()])
    t._forced_supported = False
    with pytest.raises(RuntimeError):
        t.triage(ticket, [], cfg)


def test_other_errors_are_not_swallowed(cfg, ticket):
    t, _ = make([Exception("authentication_error")])
    with pytest.raises(Exception, match="authentication_error"):
        t.triage(ticket, [], cfg)
