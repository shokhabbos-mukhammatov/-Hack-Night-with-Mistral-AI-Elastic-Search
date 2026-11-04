"""Mistral Agents API + Conversations: one persistent "HighGround SF" agent, threads with memory.

- Agent: Mistral Large, our read-only Elasticsearch tools (function tools), Mistral's built-in web_search
  (for live facts we don't hold, e.g. a road closure), and Mistral moderation guardrails.
- Conversations API keeps per-thread memory (web chat, Telegram, iMessage): follow-ups like "what about tonight?"
  work. Our function calls are executed client-side in a small loop; web_search runs server-side.
- Optional: Elastic Agent Builder tools exposed over MCP are added as more function tools (elastic_*), executed
  through an MCP client, when HG_KIBANA_URL + HG_KIBANA_API_KEY are set.
- Every answer passes the receipts check; sentences backed only by web_search citations are kept but marked.

Falls back to agent.ask (chat-completions tool loop) and then to the no-LLM path.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from elasticsearch import NotFoundError

from highground import agent as chat_agent
from highground import fallback, verify
from highground.config import settings
from highground.es import ES
from highground.mistral_client import MistralUnavailable, mistral

log = logging.getLogger("highground.mistral_agent")
AGENT_NAME = "HighGround SF"
AGENT_VERSION = "3"  # bump when instructions/tools change -> agent is updated once
THREAD_TTL = timedelta(hours=24)
INSTRUCTIONS = (
    chat_agent.SYSTEM
    + " Use the tools for anything about San Francisco flood risk, reports, alerts, tides and city help."
    " Use web_search only for live facts the tools can't know (a road closure, an event today); cite those sources."
    " Reply in the user's language, at most 120 words, and end with the single most useful link."
    " Never claim to send messages or file 311 reports: tell the user how to do it in the app."
)


def tool_specs(extra: list[dict] | None = None) -> list[dict]:
    tools = [{"type": "web_search"}]
    for t in chat_agent.TOOLS:
        tools.append({"type": "function", "function": t["function"]})
    for t in extra or []:
        tools.append({"type": "function", "function": t})
    return tools


GUARDRAILS = [{"moderation_llm_v2": {"action": "block"}, "block_on_error": False}]


def _meta_get(es: ES, key: str) -> dict | None:
    try:
        return es.client.get(index=es.ix("jobs"), id=f"meta:{key}")["_source"].get("payload")
    except NotFoundError:
        return None


def _meta_put(es: ES, key: str, payload: dict) -> None:
    es.client.index(index=es.ix("jobs"), id=f"meta:{key}", document={"kind": "meta", "status": "ok", "created_at": datetime.now(UTC).isoformat(), "payload": payload})


def ensure_agent(es: ES) -> str:
    """Create (or update once per AGENT_VERSION / MCP tool set) the persistent Mistral agent; returns its id."""
    m = mistral()
    if not m.enabled:
        raise MistralUnavailable("no key")
    mcp_tools = elastic_mcp_tools()
    fingerprint = hashlib.sha1(json.dumps([AGENT_VERSION, [t["name"] for t in mcp_tools]]).encode()).hexdigest()[:12]
    meta = _meta_get(es, "mistral-agent")
    if meta and meta.get("fingerprint") == fingerprint:
        return meta["agent_id"]
    client = m._client
    kwargs = dict(
        model=m.cfg.model_large,
        name=AGENT_NAME,
        description="Flood & storm-hazard copilot for San Francisco residents (HighGround SF)",
        instructions=INSTRUCTIONS,
        tools=tool_specs(mcp_tools),
        completion_args={"temperature": 0.2},
        guardrails=GUARDRAILS,
        metadata={"app": "highground-sf", "hg_version": AGENT_VERSION},
    )
    if meta and meta.get("agent_id"):
        agent = client.beta.agents.update(agent_id=meta["agent_id"], **kwargs)
    else:
        agent = client.beta.agents.create(**kwargs)
    _meta_put(es, "mistral-agent", {"agent_id": agent.id, "fingerprint": fingerprint})
    return agent.id


# ---------------------------------------------------------------- Elastic Agent Builder tools over MCP

_MCP_NAMES: dict[str, str] = {}  # Mistral function name -> Agent Builder tool id


def elastic_mcp_tools() -> list[dict]:
    """Agent Builder ES|QL tools (scripts/agent_builder.py) as Mistral function specs, via the SDK's MCP client."""
    from highground import elastic_mcp

    if not elastic_mcp.endpoint():
        return []
    try:
        tools = elastic_mcp.list_tools()
    except Exception as exc:  # noqa: BLE001
        log.warning("Agent Builder MCP unavailable: %s", exc)
        return []
    out = []
    for t in tools:
        fn = "elastic_" + "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in t.function.name)[:55]
        _MCP_NAMES[fn] = t.function.name
        out.append({"name": fn, "description": (t.function.description or "")[:900], "parameters": t.function.parameters or {"type": "object", "properties": {}}})
    return out


def _run_mcp_tool(fn_name: str, args: dict) -> dict:
    from highground import elastic_mcp

    if fn_name not in _MCP_NAMES:
        elastic_mcp_tools()
    name = _MCP_NAMES.get(fn_name)
    if not name:
        return {"error": f"unknown Elastic tool {fn_name}"}
    try:
        return {"result": elastic_mcp.call_tool(name, args)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Agent Builder tool failed: {exc}"[:300]}


# ---------------------------------------------------------------- threads (conversation memory)


def thread_key(channel: str, handle: str) -> str:
    return "thread:" + hashlib.sha256(f"{settings().fernet_key}|{channel}|{handle}".encode()).hexdigest()[:32]


def get_thread(es: ES, key: str) -> str | None:
    try:
        src = es.client.get(index=es.ix("jobs"), id=key)["_source"]
    except NotFoundError:
        return None
    if datetime.fromisoformat(src["created_at"]) < datetime.now(UTC) - THREAD_TTL:
        return None
    return (src.get("payload") or {}).get("conversation_id")


def put_thread(es: ES, key: str, conversation_id: str) -> None:
    es.client.index(index=es.ix("jobs"), id=key, document={"kind": "thread", "status": "ok", "created_at": datetime.now(UTC).isoformat(), "payload": {"conversation_id": conversation_id}})


# ---------------------------------------------------------------- the conversation loop


def _text_and_refs(entry) -> tuple[str, list[dict]]:
    content = getattr(entry, "content", "")
    if isinstance(content, str):
        return content, []
    text, refs = [], []
    for ch in content or []:
        t = getattr(ch, "type", None)
        if t == "text":
            text.append(ch.text)
        elif t == "tool_reference":
            refs.append({"url": ch.url, "title": ch.title, "source": "web_search"})
    return "".join(text), refs


def converse(es: ES, question: str, lang: str | None = None, lat: float | None = None, lon: float | None = None, conversation_id: str | None = None) -> dict:
    lang = (lang if lang and lang != "auto" else None) or chat_agent.detect_lang(question)
    m = mistral()
    if not m.enabled:
        return chat_agent.ask(es, question, lang, lat, lon)
    ctx = f"\n(The user is near lat={lat:.5f}, lon={lon:.5f}.)" if lat is not None and lon is not None else ""
    evidence: list = []
    citations: list[dict] = []
    try:
        agent_id = ensure_agent(es)
        conv = m._client.beta.conversations
        if conversation_id:
            res = conv.append(conversation_id=conversation_id, inputs=question + ctx, timeout_ms=30_000)
        else:
            res = conv.start(agent_id=agent_id, inputs=question + ctx, store=True, timeout_ms=30_000)
        for _ in range(5):
            calls = [o for o in res.outputs if getattr(o, "type", None) == "function.call"]
            if not calls:
                break
            results = []
            for c in calls:
                args = json.loads(c.arguments) if isinstance(c.arguments, str) else (c.arguments or {})
                out = _run_mcp_tool(c.name, args) if c.name.startswith("elastic_") else chat_agent.run_tool(es, c.name, args, lang)
                evidence.append(out)
                for r in out.get("results", []) if isinstance(out, dict) else []:
                    citations.append({"url": r.get("url"), "title": r.get("title"), "source": "guides"})
                results.append({"type": "function.result", "tool_call_id": c.tool_call_id, "result": "<data>" + json.dumps(out, default=str, ensure_ascii=False)[:6000] + "</data>"})
            res = conv.append(conversation_id=res.conversation_id, inputs=results, timeout_ms=30_000)
        messages = [o for o in res.outputs if getattr(o, "type", None) == "message.output"]
        if not messages:
            raise MistralUnavailable("no message output")
        answer, refs = _text_and_refs(messages[-1])
    except MistralUnavailable as exc:
        log.info("agent fallback: %s", exc)
        return chat_agent.ask(es, question, lang, lat, lon)
    except Exception as exc:  # noqa: BLE001 - beta API or guardrail block: fall back, never fail the user
        if "guardrail" in str(exc).lower() or "403" in str(exc):
            return {"answer": "Let's keep it about flooding, storms and city services.", "lang": lang, "citations": [], "generated_by": "guardrail"}
        log.warning("agent error, falling back: %s", exc)
        return chat_agent.ask(es, question, lang, lat, lon)
    kept, dropped, gone = verify.check(answer, evidence, question, fallback.DISCLAIMER)
    web_backed = bool(refs)
    final = answer if (dropped == 0 or web_backed) else kept
    return {
        "answer": final,
        "lang": lang,
        "citations": chat_agent._uniq(citations + refs),
        "conversation_id": res.conversation_id,
        "dropped_claims": 0 if web_backed else dropped,
        "web_sourced": web_backed,
        "generated_by": "mistral-agent",
    }
