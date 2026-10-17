from dataclasses import dataclass, field

import pytest

from finops_agent.detect import detect
from finops_agent.explain import Writer, check, incident_facts, recommendation_facts
from finops_agent.explain.llm import LLM, NoModel, RateLimited, Reply
from finops_agent.recommend import recommend


@dataclass
class FakeLLM:
    replies: list[str]
    calls: list[list[dict]] = field(default_factory=list)
    live_calls: int = 0
    cached_calls: int = 0

    def chat(self, messages):
        self.calls.append(messages)
        if not self.replies:
            raise NoModel("empty")
        return Reply(self.replies.pop(0), False, 0.1, 10, 10)


@pytest.fixture(scope="module")
def items(dataset, prepared):
    return detect(prepared, "seasonal"), recommend(prepared, dataset.metrics)


def test_every_template_explanation_is_grounded(items):
    incidents, recs = items
    writer = Writer(use_model=False)
    for inc in incidents:
        e = writer.incident(inc)
        assert e.final_ungrounded == [], e.text
    for rec in recs:
        e = writer.recommendation(rec)
        assert e.final_ungrounded == [], e.text


def test_grounded_model_text_is_kept(items):
    inc = items[0][0]
    f = incident_facts(inc)
    text = f"Spend on {f['what_changed']} rose to {f['daily_cost_when_detected']} a day."
    e = Writer(FakeLLM([text])).incident(inc)  # type: ignore[arg-type]
    assert e.source == "model" and e.text == text


def test_ungrounded_text_gets_one_retry_naming_the_bad_number(items):
    inc = items[0][0]
    f = incident_facts(inc)
    llm = FakeLLM([f"It cost $999,999 more, on {f['what_changed']}.", f"It cost {f['extra_cost_so_far']} more."])
    e = Writer(llm).incident(inc)  # type: ignore[arg-type]
    assert e.source == "model-retry" and e.first_try_ungrounded == ["$999,999"]
    assert "$999,999" in llm.calls[1][-1]["content"]


def test_still_ungrounded_falls_back_to_the_template(items):
    rec = items[1][0]
    llm = FakeLLM(["Saves $1 a month.", "Saves $2 a month."])
    e = Writer(llm).recommendation(rec)  # type: ignore[arg-type]
    assert e.source == "template" and e.final_ungrounded == []
    assert check(e.text, recommendation_facts(rec)) == []


def test_cached_replies_replay_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)
    llm = LLM(cache_dir=tmp_path, offline=True)
    msgs = [{"role": "user", "content": "hello"}]
    with pytest.raises(NoModel):
        llm.chat(msgs)
    (tmp_path / f"{llm.key(msgs)}.json").write_text(
        '{"text": "hi", "latency_s": 0.5, "prompt_tokens": 3, "completion_tokens": 1}', encoding="utf-8"
    )
    assert llm.chat(msgs).text == "hi" and llm.cached_calls == 1


def test_rate_limit_is_an_error_not_a_silent_retry():
    assert issubclass(RateLimited, RuntimeError)
