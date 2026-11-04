"""Deterministic actions + template narratives. Always available: used directly when Mistral is down, and as
the factual skeleton Mistral rewrites in plain language / the user's language when it is up.

Every fact and link here was checked on 2026-11-03 (see docs/RESEARCH.md).
"""

from highground.models import Action, Level, Narrative, RiskState, ZoneInfo

URL = {
    "sandbags": "https://www.sfpublicworks.org/services/sandbags",
    "sf311": "https://sf311.org",
    "adopt": "https://adoptadrain.sfwater.org",
    "grant": "https://www.sfpuc.gov/floodwatergrant",
    "flood_map": "https://www.sfpuc.gov/learning/emergency-preparedness/flood-maps",
    "insurance": "https://www.sf.gov/san-francisco-floodplain-administration",
    "alertsf": "https://www.sf.gov/alertsf",
    "turn_around": "https://www.weather.gov/safety/flood-turn-around-dont-drown",
    "sf72": "https://www.sf72.org/hazard/flood",
}

DISCLAIMER = (
    "HighGround is not an official alert service. In an emergency call 911. "
    "Official alerts: AlertSF (text your ZIP code to 888-777) and the National Weather Service."
)


def actions_for(state: RiskState, zone: ZoneInfo, lang: str = "en") -> list[Action]:
    acts = _actions_en(state, zone)
    if _base_lang(lang) == "en":
        return acts
    from highground.i18n import tr

    return [a.model_copy(update={"text": tr(lang, f"action.{a.code}", a.text)}) for a in acts]


def _actions_en(state: RiskState, zone: ZoneInfo) -> list[Action]:
    codes = {r.code for r in state.reasons}
    lvl = state.level
    out: list[Action] = []
    stormy = lvl.rank >= Level.elevated.rank
    if lvl.rank >= Level.high.rank:
        out.append(Action(code="911", text="If water is entering your home or someone is in danger, call 911."))
        out.append(
            Action(
                code="turn_around",
                text="Do not walk or drive through flooded streets or underpasses. Turn around, don't drown.",
                url=URL["turn_around"],
            )
        )
        if zone.in_100yr:
            out.append(Action(code="move_car", text="Move cars out of low spots and underground garages before water rises."))
    if stormy and (zone.in_100yr or "reports_nearby" in codes):
        out.append(
            Action(
                code="sandbags",
                text=(
                    "Free sandbags (up to 10 per address, bring proof of address): Public Works yard, "
                    "Kansas & Marin St gate, Mon-Sat 8 AM-2 PM."
                ),
                url=URL["sandbags"],
            )
        )
    if "wind" in codes or "trees_nearby" in codes or any(c.startswith("nws:Wind") or c.startswith("nws:High Wind") for c in codes):
        out.append(Action(code="trees", text="Strong gusts on wet soil: avoid parking or waiting under large trees."))
    if "high_tide" in codes or any(c.startswith("nws:Coastal") for c in codes):
        out.append(Action(code="coast", text="Stay back from the shoreline and low waterfront streets around high tide."))
    if stormy:
        out.append(Action(code="report", text="See flooding or a fallen tree? Report it to 311 so crews come.", url=URL["sf311"]))
        out.append(
            Action(
                code="drain",
                text="If it is safe, clear leaves off the storm drain on your block. You can adopt a drain.",
                url=URL["adopt"],
            )
        )
    if not stormy:
        if zone.in_100yr:
            out.append(
                Action(
                    code="insurance",
                    text="Homeowner and renter policies usually exclude floods, and NFIP flood insurance has a 30-day wait. Get it before storms.",
                    url=URL["insurance"],
                )
            )
            out.append(
                Action(code="grant", text="SFPUC Floodwater Grant: up to $100,000 for flood-proofing eligible properties.", url=URL["grant"])
            )
        out.append(
            Action(
                code="sandbag_saturday",
                text="Sandbag Saturdays (Nov 7, 14, 21 and Dec 5, 8 AM-2 PM) at Marina Green, Lake Merced, Ocean Beach and more.",
                url=URL["sandbags"],
            )
        )
        out.append(Action(code="adopt", text="Adopt the storm drain on your block and get a heads-up before big storms.", url=URL["adopt"]))
    out.append(Action(code="alertsf", text="Get official city alerts: sign up for AlertSF.", url=URL["alertsf"]))
    return out


_HEADLINE = {
    "en": {
        Level.low: "Low flood risk on this block right now.",
        Level.elevated: "Elevated risk: flooding is possible on this block.",
        Level.high: "High risk: flooding is likely on or near this block.",
        Level.severe: "Severe risk: dangerous flooding is likely here. Act now.",
    },
    "es": {
        Level.low: "Riesgo bajo de inundación en esta cuadra ahora.",
        Level.elevated: "Riesgo elevado: es posible que se inunde esta cuadra.",
        Level.high: "Riesgo alto: es probable que haya inundación en esta cuadra o cerca.",
        Level.severe: "Riesgo grave: es probable una inundación peligrosa aquí. Actúe ahora.",
    },
    "zh": {
        Level.low: "此街区目前水浸风险低。",
        Level.elevated: "风险升高：此街区可能出现水浸。",
        Level.high: "高风险：此街区或附近很可能出现水浸。",
        Level.severe: "极高风险：此处很可能出现危险水浸，请立即行动。",
    },
    "tl": {
        Level.low: "Mababa ang panganib ng baha sa block na ito ngayon.",
        Level.elevated: "Tumaas ang panganib: posibleng bahain ang block na ito.",
        Level.high: "Mataas ang panganib: malamang na bahain ang block na ito o malapit dito.",
        Level.severe: "Matinding panganib: malamang ang mapanganib na baha dito. Kumilos ngayon.",
    },
    "vi": {
        Level.low: "Nguy cơ ngập thấp ở khu phố này lúc này.",
        Level.elevated: "Nguy cơ tăng: khu phố này có thể bị ngập.",
        Level.high: "Nguy cơ cao: khu phố này hoặc gần đây có khả năng bị ngập.",
        Level.severe: "Nguy cơ nghiêm trọng: có khả năng ngập nguy hiểm tại đây. Hãy hành động ngay.",
    },
    "ru": {
        Level.low: "Сейчас риск подтопления в этом квартале низкий.",
        Level.elevated: "Повышенный риск: возможно подтопление этого квартала.",
        Level.high: "Высокий риск: подтопление в этом квартале или рядом вероятно.",
        Level.severe: "Очень высокий риск: здесь вероятно опасное наводнение. Действуйте сейчас.",
    },
}
SUPPORTED_LANGS = sorted(_HEADLINE)


def headline(level: Level, lang: str = "en") -> str:
    return _HEADLINE.get(_base_lang(lang), _HEADLINE["en"])[level]


_LABELS = {
    "en": ("Why", "What to do", "No rain, alerts or nearby reports right now."),
    "es": ("Por qué", "Qué hacer", "Ahora no hay lluvia, alertas ni reportes cerca."),
    "zh": ("原因", "應該怎樣做", "目前沒有下雨、警報或附近報告。"),
    "tl": ("Bakit", "Ano ang gagawin", "Walang ulan, alerto o ulat sa malapit ngayon."),
    "vi": ("Vì sao", "Nên làm gì", "Hiện không có mưa, cảnh báo hay báo cáo gần đây."),
    "ru": ("Почему", "Что делать", "Сейчас нет дождя, предупреждений или сообщений рядом."),
}


def template_narrative(state: RiskState, actions: list[Action], lang: str = "en") -> Narrative:
    """Plain-language briefing built only from the computed reasons/actions (so it can't state anything else).
    Without Mistral, labels and the headline are localized; reason and action details stay in English."""
    why, todo, calm = _LABELS.get(_base_lang(lang), _LABELS["en"])
    lines = [headline(state.level, lang)]
    if state.reasons:
        from highground.i18n import tr

        lines.append(f"{why}: " + " ".join(tr(lang, f"reason.{r.code}", r.text) for r in state.reasons[:5]))
    else:
        lines.append(f"{why}: {calm}")
    top = [a.text for a in actions[:3]]
    if top:
        lines.append(f"{todo}: " + " ".join(top))
    return Narrative(text="\n".join(lines), lang=_base_lang(lang) if _base_lang(lang) in _HEADLINE else "en", generated_by="template")


def alert_text(state: RiskState, place: str, lang: str = "en", link: str | None = None) -> str:
    """<= ~320 chars, for iMessage/Telegram/Web Push."""
    reason = state.reasons[0].text if state.reasons else ""
    parts = [f"HighGround: {place}", headline(state.level, lang), reason]
    if link:
        parts.append(link)
    parts.append("Reply STOP to unsubscribe.")
    txt = " ".join(p for p in parts if p)
    return txt if len(txt) <= 320 else txt[:317] + "..."


def _base_lang(lang: str) -> str:
    return (lang or "en").split("-")[0].lower()
