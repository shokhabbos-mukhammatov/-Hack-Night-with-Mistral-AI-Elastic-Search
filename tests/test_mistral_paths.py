"""Mistral code paths with a fake SDK client (no network): agent + conversations loop, receipts check,
guardrail handling, LLM enrichment safeguards, Batch-translation fallback."""

from types import SimpleNamespace as NS

import pytest

from highground import enrich_llm, fallback, i18n, mistral_agent
from highground.models import Components, Level, Reason, RiskState, ZoneInfo


class FakeConversations:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def start(self, **kw):
        self.calls.append(("start", kw))
        return self.script.pop(0)

    def append(self, **kw):
        self.calls.append(("append", kw))
        return self.script.pop(0)


class FakeAgents:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return NS(id="ag_1")

    def update(self, **kw):
        return NS(id=kw["agent_id"])


class FakeMistral:
    def __init__(self, script):
        from highground.config import settings

        self.enabled, self.cfg = True, settings()
        self._client = NS(beta=NS(conversations=FakeConversations(script), agents=FakeAgents()))


class FakeES:
    """Just enough of ES for the agent's meta/thread docs."""

    def __init__(self):
        self.docs = {}
        self.client = NS(get=self._get, index=self._index)

    def ix(self, n):
        return f"t-{n}"

    def _get(self, index, id):
        from elasticsearch import NotFoundError

        if (index, id) not in self.docs:
            raise NotFoundError("nf", NS(status=404), {})
        return {"_source": self.docs[(index, id)]}

    def _index(self, index, id, document):
        self.docs[(index, id)] = document


def call(name, args, cid="c1"):
    return NS(conversation_id=cid, outputs=[NS(type="function.call", name=name, arguments=args, tool_call_id="t1")])


def message(chunks, cid="c1"):
    return NS(conversation_id=cid, outputs=[NS(type="message.output", content=chunks)])


@pytest.fixture
def es():
    return FakeES()


def test_agent_runs_our_tool_then_answers_with_receipts(monkeypatch, es):
    fake = FakeMistral([
        call("search_guides", '{"query": "sandbags"}'),
        message([NS(type="text", text="Get up to 10 free sandbags at the Public Works yard. They hold 999 pounds each.")]),
    ])  # fmt: skip
    monkeypatch.setattr(mistral_agent, "mistral", lambda: fake)
    monkeypatch.setattr(mistral_agent, "elastic_mcp_tools", lambda: [])
    monkeypatch.setattr(mistral_agent.chat_agent, "run_tool", lambda es_, n, a, lang: {"results": [{"url": "https://www.sfpublicworks.org/services/sandbags", "title": "Free sandbags", "text": "up to 10 free sandbags"}]})
    out = mistral_agent.converse(es, "where can I get sandbags?")
    assert out["generated_by"] == "mistral-agent" and out["conversation_id"] == "c1"
    assert "10 free sandbags" in out["answer"] and "999" not in out["answer"], "invented number dropped by the receipts check"
    assert out["citations"][0]["url"].endswith("/sandbags")
    kinds = [k for k, _ in fake._client.beta.conversations.calls]
    assert kinds == ["start", "append"]
    created = fake._client.beta.agents.created[0]
    assert {"type": "web_search"} in created["tools"] and created["guardrails"][0]["moderation_llm_v2"]["action"] == "block"


def test_agent_follow_up_appends_to_existing_conversation(monkeypatch, es):
    fake = FakeMistral([message([NS(type="text", text="Tonight looks dry.")], cid="c9")])
    monkeypatch.setattr(mistral_agent, "mistral", lambda: fake)
    monkeypatch.setattr(mistral_agent, "elastic_mcp_tools", lambda: [])
    out = mistral_agent.converse(es, "and tonight?", conversation_id="c9")
    assert fake._client.beta.conversations.calls[0][0] == "append" and out["conversation_id"] == "c9"


def test_web_sourced_answers_keep_citations_and_are_flagged(monkeypatch, es):
    fake = FakeMistral([message([NS(type="text", text="The Great Highway is closed between Lincoln and Sloat until 6 PM."),
                                  NS(type="tool_reference", url="https://sfmta.com/x", title="SFMTA")])])  # fmt: skip
    monkeypatch.setattr(mistral_agent, "mistral", lambda: fake)
    monkeypatch.setattr(mistral_agent, "elastic_mcp_tools", lambda: [])
    out = mistral_agent.converse(es, "is the great highway closed?")
    assert out["web_sourced"] and "6 PM" in out["answer"] and out["citations"][0]["source"] == "web_search"


def test_guardrail_block_is_friendly(monkeypatch, es):
    class Boom(FakeConversations):
        def start(self, **kw):
            raise RuntimeError("403 blocked by guardrail")

    fake = FakeMistral([])
    fake._client.beta.conversations = Boom([])
    monkeypatch.setattr(mistral_agent, "mistral", lambda: fake)
    monkeypatch.setattr(mistral_agent, "elastic_mcp_tools", lambda: [])
    assert mistral_agent.converse(es, "something abusive")["generated_by"] == "guardrail"


def test_mcp_tools_are_namespaced_for_mistral(monkeypatch):
    from highground import elastic_mcp

    monkeypatch.setattr(elastic_mcp, "endpoint", lambda: ("http://k/api/agent_builder/mcp?namespace=highground", {}))
    tool = NS(function=NS(name="highground_flood_hotspots", description="hotspots", parameters={"type": "object", "properties": {"hours": {"type": "integer"}}}))
    monkeypatch.setattr(elastic_mcp, "list_tools", lambda *a, **k: [tool])
    specs = mistral_agent.elastic_mcp_tools()
    assert specs[0]["name"] == "elastic_highground_flood_hotspots" and specs[0]["parameters"]["properties"]["hours"]
    monkeypatch.setattr(elastic_mcp, "call_tool", lambda name, args, *a: [f"{name}:{args['hours']}"])
    assert mistral_agent._run_mcp_tool("elastic_highground_flood_hotspots", {"hours": 6}) == {"result": ["highground_flood_hotspots:6"]}


def test_photo_allowlist_blocks_ssrf():
    assert enrich_llm.allowed_photo("https://spot-sf-res.cloudinary.com/image/upload/x.jpg")
    assert not enrich_llm.allowed_photo("http://spot-sf-res.cloudinary.com/x.jpg")
    assert not enrich_llm.allowed_photo("https://169.254.169.254/latest/meta-data")
    assert not enrich_llm.allowed_photo("https://evil.example/cloudinary.com.jpg")


def test_batch_translations_used_by_fallback(monkeypatch):
    strings = i18n.source_strings()
    assert "action.sandbags" in strings and "reason.zone_100yr" in strings
    i18n.table.cache_clear()
    monkeypatch.setattr(i18n, "table", lambda lang: {"action.alertsf": "Reciba alertas oficiales: AlertSF."} if lang == "es" else {})
    st = RiskState(cell="c", level=Level.low, score=0, components=Components(), reasons=[Reason(code="zone_100yr", text="x", source="s")], as_of=__import__("datetime").datetime(2026, 1, 1))
    acts = {a.code: a.text for a in fallback.actions_for(st, ZoneInfo(in_100yr=True), "es")}
    assert acts["alertsf"] == "Reciba alertas oficiales: AlertSF."
    assert acts["insurance"].startswith("Homeowner"), "untranslated strings fall back to English"
