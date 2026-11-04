"""End-to-end smoke test against a running deployment.

    python scripts/smoke.py https://<your-app>.up.railway.app
"""

import sys
import time

import httpx

ADDRESSES = ["17th and Folsom", "Gilman Ave & 3rd St", "Ocean Ave & Cayuga Ave", "4000 Judah St", "Twin Peaks Blvd & Christmas Tree Point Rd"]


def main(base: str) -> int:
    base = base.rstrip("/")
    failures: list[str] = []
    c = httpx.Client(timeout=40, follow_redirects=True)

    def check(name, fn):
        t0 = time.perf_counter()
        try:
            detail = fn()
            print(f"ok   {name:<34} {1000 * (time.perf_counter() - t0):6.0f} ms  {detail or ''}")
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"FAIL {name:<34} {exc}")

    def j(path, **kw):
        r = c.get(base + path, **kw)
        r.raise_for_status()
        return r.json()

    check("healthz", lambda: j("/healthz")["ok"] or 1 / 0)

    def status():
        s = j("/status")
        stale = [k for k, v in s["feeds"].items() if v.get("stale") and k != "datasf"]
        assert s["elasticsearch"], "elasticsearch down"
        assert not stale, f"stale feeds: {stale}"
        return f"mistral={s['mistral']} channels={s['channels']} levels={s['risk_levels']}"

    check("status (fresh feeds)", status)
    for a in ADDRESSES:
        check(f"briefing {a[:24]}", lambda a=a: (b := j("/api/briefing", params={"address": a}))["risk"]["level"] + f" · {b['location']['label']}")
    check("briefing es", lambda: j("/api/briefing", params={"address": "17th y Folsom", "lang": "es"})["narrative"]["lang"])

    def ask(q):
        r = c.post(base + "/api/ask", json={"question": q})
        r.raise_for_status()
        a = r.json()
        assert a["answer"], "empty answer"
        return f"{a['generated_by']} · {a['answer'][:60]!r}"

    check("ask en", lambda: ask("Where can I get free sandbags?"))
    check("ask es", lambda: ask("¿Dónde consigo sacos de arena gratis?"))
    check("replay index", lambda: ",".join(j("/api/replay")["dates"]))
    check("replay 2022-12-31", lambda: f"{len(j('/api/replay/2022-12-31')['steps'])} steps")
    check("backtest 2022-12-31", lambda: (b := j("/api/backtest/2022-12-31")) and f"recall={b['recall']} precision={b['precision']} median_lead={b['median_lead_min']}min")
    check("text list nearby", lambda: f"{len(j('/api/list/nearby', params={'lat': 37.7637, 'lon': -122.4152, 'hours': 24})['items'])} items")
    check("vector tile", lambda: c.get(base + "/tiles/zones/14/2620/6334.pbf").status_code in (200, 204) or 1 / 0)

    def report():
        r = c.post(base + "/api/report", data={"lat": "37.7637", "lon": "-122.4152", "text": "storm drain clogged, water ponding at the corner"})
        r.raise_for_status()
        d = r.json()
        assert d["ok"] and d["draft"]["service_code"], d
        return f"{d['draft']['service_code']} · dups={len(d['duplicates'])}"

    check("report draft", report)
    check("outside SF is rejected", lambda: c.get(base + "/api/briefing", params={"lat": 37.33, "lon": -121.89}).status_code == 422 or 1 / 0)
    print(f"\n{'ALL OK' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"))
