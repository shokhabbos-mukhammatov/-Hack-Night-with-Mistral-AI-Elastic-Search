"""Elastic Agent Builder setup: HighGround ES|QL tools + an ops agent, exposed over Kibana's MCP server.

    python scripts/agent_builder.py                    # uses HG_KIBANA_URL / HG_KIBANA_API_KEY / HG_INDEX_PREFIX
    python scripts/agent_builder.py --check            # list our tools via MCP and run one

The same tools are then available to:
- Kibana's Agent Builder chat (city staff), optionally powered by Mistral through the Elastic inference endpoint
- the HighGround Mistral agent, which connects to {KIBANA}/api/agent_builder/mcp as an MCP client
"""

import argparse
import json
import os
import sys

import httpx

PREFIX = os.environ.get("HG_INDEX_PREFIX", "hg-")
KIBANA = os.environ.get("HG_KIBANA_URL", "http://localhost:5601").rstrip("/")
KEY = os.environ.get("HG_KIBANA_API_KEY", "")
HOURS = 'TO_TIMEDURATION(CONCAT(TO_STRING(?hours), " hours"))'


def tools() -> list[dict]:
    r, p = f"{PREFIX}hazard-reports", f"{PREFIX}cell-profiles"
    hours = {"hours": {"type": "integer", "description": "Look-back window in hours (1-72)"}}
    nh = {"neighborhood": {"type": "string", "description": "SF analysis neighborhood, e.g. 'Mission', 'Bayview Hunters Point'"}}
    return [
        {
            "id": "highground.flood_hotspots",
            "description": "San Francisco blocks with the most recent flooding / clogged-drain / sewer-backup reports to 311, weighted by "
            "whether the block is in SFPUC's 100-year flood zone and by its storm-day report history (LOOKUP JOIN on cell profiles).",
            "query": f"""FROM {r}
| WHERE requested_at >= NOW() - {HOURS} AND category IN ("flooding", "catch_basin", "sewer_backup")
| STATS reports = COUNT(*), latest = MAX(requested_at) BY h3_9
| LOOKUP JOIN {p} ON h3_9
| EVAL priority = ROUND(reports * (CASE(in_100yr == true, 2.0, 1.0) + COALESCE(storm_reports_5y, 0) / 20.0), 2)
| KEEP h3_9, neighborhood, reports, latest, in_100yr, storm_reports_5y, priority
| SORT priority DESC | LIMIT 15""",
            "params": hours,
        },
        {
            "id": "highground.neighborhood_reports",
            "description": "Counts of recent SF311 hazard reports in one San Francisco neighborhood, by category (flooding, catch_basin, sewer_backup, tree, street).",
            "query": f"""FROM {r}
| WHERE neighborhood == ?neighborhood AND requested_at >= NOW() - {HOURS}
| STATS reports = COUNT(*), latest = MAX(requested_at) BY category
| SORT reports DESC""",
            "params": {**nh, **hours},
        },
        {
            "id": "highground.neighborhood_risk",
            "description": "Current HighGround risk level counts (low/elevated/high/severe) for the blocks of one San Francisco neighborhood.",
            "query": f"""FROM {PREFIX}risk-cells
| RENAME cell AS h3_9
| LOOKUP JOIN {p} ON h3_9
| WHERE neighborhood == ?neighborhood
| STATS blocks = COUNT(*), max_score = MAX(score) BY level
| SORT max_score DESC""",
            "params": nh,
        },
        {
            "id": "highground.storm_history",
            "description": "Flooding reports per year in one San Francisco neighborhood over the last 5 years (SF311 history).",
            "query": f"""FROM {r}
| WHERE category == "flooding" AND neighborhood == ?neighborhood AND requested_at >= NOW() - 5 years
| EVAL year = DATE_EXTRACT("year", requested_at)
| STATS reports = COUNT(*) BY year
| SORT year""",
            "params": nh,
        },
        {
            "id": "highground.active_alerts",
            "description": "National Weather Service alerts in effect for San Francisco right now (flood, coastal flood, wind).",
            "query": f"""FROM {PREFIX}conditions
| WHERE kind == "nws_alert" AND valid_from <= NOW() AND valid_to > NOW()
| KEEP event, severity, headline, valid_from, valid_to, url
| SORT valid_to""",
            "params": {},
        },
    ]


AGENT = {
    "id": "highground-ops",
    "name": "HighGround SF Ops",
    "description": "Answers city-ops questions about San Francisco flood hotspots, 311 hazard reports, block risk and NWS alerts.",
    "configuration": {
        "instructions": "You help San Francisco city crews and community organizations during storms. Use the highground.* tools. "
        "Be concise, cite counts exactly as returned, and name neighborhoods. Never invent numbers.",
        "tools": [{"tool_ids": [t["id"] for t in tools()]}],
    },
}


def client() -> httpx.Client:
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = f"ApiKey {KEY}"
    return httpx.Client(base_url=KIBANA, headers=headers, timeout=60)


def upsert_tools(c: httpx.Client) -> list[str]:
    done = []
    for t in tools():
        body = {"description": t["description"], "tags": ["highground"], "configuration": {"query": t["query"], "params": t["params"]}}
        exists = c.get(f"/api/agent_builder/tools/{t['id']}").status_code == 200
        if exists:  # update: id and type are immutable
            r = c.put(f"/api/agent_builder/tools/{t['id']}", json=body)
        else:
            r = c.post("/api/agent_builder/tools", json={"id": t["id"], "type": "esql", **body})
        if r.status_code >= 400:
            raise SystemExit(f"tool {t['id']}: {r.status_code} {r.text[:400]}")
        done.append(t["id"])
    return done


def upsert_agent(c: httpx.Client) -> str | None:
    body = {k: v for k, v in AGENT.items() if k != "id"}
    if c.get(f"/api/agent_builder/agents/{AGENT['id']}").status_code == 200:
        r = c.put(f"/api/agent_builder/agents/{AGENT['id']}", json=body)
    else:
        r = c.post("/api/agent_builder/agents", json=AGENT)
    if r.status_code >= 400:  # e.g. local clusters without security: agents need an authenticated user
        print(f"warning: agent not created ({r.status_code}): {r.text[:200]}", file=sys.stderr)
        return None
    return AGENT["id"]


def mistral_connector(c: httpx.Client) -> str | None:
    """Kibana .inference connector backed by the ES Mistral chat_completion endpoint: Agent Builder runs on Mistral."""
    inference_id = f"{PREFIX}mistral-chat"
    r = c.post("/api/actions/connector", json={
        "name": "Mistral (via Elastic inference)", "connector_type_id": ".inference",
        "config": {"provider": "mistral", "taskType": "chat_completion", "inferenceId": inference_id, "providerConfig": {}},
        "secrets": {"providerSecrets": {}},
    })  # fmt: skip
    return r.json().get("id") if r.status_code < 400 else None


def mcp_check() -> None:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    from highground import elastic_mcp

    url, headers = f"{KIBANA}/api/agent_builder/mcp?namespace={elastic_mcp.NAMESPACE}", ({"Authorization": f"ApiKey {KEY}"} if KEY else {})
    tools = elastic_mcp.list_tools(url, headers)
    print(json.dumps({"highground_mcp_tools": [t.function.name for t in tools]}))
    out = elastic_mcp.call_tool("highground_flood_hotspots", {"hours": 72}, url, headers)
    print("MCP call highground_flood_hotspots(hours=72) ->", (out[0] if out else "")[:700])
    out = elastic_mcp.call_tool("highground_neighborhood_reports", {"neighborhood": "Mission", "hours": 72}, url, headers)
    print("MCP call highground_neighborhood_reports(Mission, 72h) ->", (out[0] if out else "")[:500])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    with client() as c:
        if not args.check:
            print(json.dumps({"tools": upsert_tools(c), "agent": upsert_agent(c)}))
            if os.environ.get("HG_MISTRAL_API_KEY"):
                print(json.dumps({"mistral_connector": mistral_connector(c)}))
    mcp_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
