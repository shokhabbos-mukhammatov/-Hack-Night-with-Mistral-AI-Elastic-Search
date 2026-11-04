"""Data contracts shared by every module. Data only: logic lives in plain functions elsewhere."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Level(StrEnum):
    low = "low"
    elevated = "elevated"
    high = "high"
    severe = "severe"

    @property
    def rank(self) -> int:
        return LEVEL_ORDER.index(self)


LEVEL_ORDER = [Level.low, Level.elevated, Level.high, Level.severe]


class Category(StrEnum):
    flooding = "flooding"
    sewer = "sewer_backup"
    catch_basin = "catch_basin"
    water_leak = "water_leak"
    tree = "tree"
    street = "street"
    other = "other"


HAZARD_CATEGORIES = {Category.flooding, Category.sewer, Category.catch_basin, Category.tree, Category.street}


class HazardReport(BaseModel):
    """A 311 case from SF Open311 (live) or DataSF (history), normalized."""

    id: str  # "311:<case_id>": Open311 (live) and DataSF (nightly) views of one case merge into one doc
    case_id: str
    source: str  # open311 | datasf
    category: Category
    subcategory: str | None = None
    service_name: str | None = None
    service_code: str | None = None
    status: str | None = None
    status_notes: str | None = None
    requested_at: datetime
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    lat: float
    lon: float
    address: str | None = None
    neighborhood: str | None = None
    description: str | None = None
    media_url: str | None = None


class Reason(BaseModel):
    code: str
    text: str
    value: float | None = None
    unit: str | None = None
    source: str
    source_url: str | None = None
    as_of: datetime | None = None


class Components(BaseModel):
    susceptibility: float = 0.0
    hazard: float = 0.0
    confirmation: float = 0.0
    trees: float = 0.0


class RiskState(BaseModel):
    cell: str
    level: Level
    score: float
    components: Components
    reasons: list[Reason] = Field(default_factory=list)
    as_of: datetime
    inputs_hash: str = ""


class ZoneInfo(BaseModel):
    in_100yr: bool = False
    depth_ft: float | None = None  # expected 100-year-storm depth band (ft above ground), if published
    slr24: bool = False  # inside 100-yr storm + 24" sea level rise inundation
    shoreline: bool = False


class CellProfile(BaseModel):
    """Slow-changing facts about a cell, rebuilt nightly (or as of a date for replay)."""

    cell: str
    zone: ZoneInfo = Field(default_factory=ZoneInfo)
    reports_5y: int = 0  # flooding reports within the cell + ring(1), 5 years before as_of
    storm_reports_5y: int = 0  # ...of which on citywide storm days (>= 100 flood reports that day)
    storm_days_5y: int = 0  # number of citywide storm days in the same window
    top_dates: list[str] = Field(default_factory=list)


class Weather(BaseModel):
    point: str
    rain_1h_mm: float | None = None
    rain_3h_mm: float | None = None
    rain_next3h_mm: float | None = None
    gust_kmh: float | None = None
    gust_next3h_kmh: float | None = None
    as_of: datetime | None = None
    source: str = "open-meteo"


class NwsAlert(BaseModel):
    id: str
    event: str
    severity: str | None = None
    headline: str | None = None
    starts: datetime | None = None
    ends: datetime | None = None
    url: str | None = None
    description: str | None = None


class Tide(BaseModel):
    now_ft: float | None = None
    next_high_ft: float | None = None
    next_high_at: datetime | None = None
    as_of: datetime | None = None


class EventKind(StrEnum):
    flood_report = "flood_report"
    sewer_report = "sewer_report"
    tree_report = "tree_report"
    street_report = "street_report"
    road_incident = "road_incident"
    street_signal = "street_signal"


class NearbyEvent(BaseModel):
    kind: EventKind
    at: datetime
    distance_m: float
    confidence: float = 1.0
    verified_photo: bool = False
    source: str
    source_url: str | None = None
    label: str | None = None
    lat: float | None = None
    lon: float | None = None


class Inputs(BaseModel):
    """Everything the risk engine may look at for one cell at one instant. Nothing else."""

    cell: str
    as_of: datetime
    profile: CellProfile
    weather: Weather | None = None
    alerts: list[NwsAlert] = Field(default_factory=list)
    tide: Tide | None = None
    events: list[NearbyEvent] = Field(default_factory=list)


class Action(BaseModel):
    code: str
    text: str
    url: str | None = None


class Narrative(BaseModel):
    text: str
    lang: str = "en"
    generated_by: str = "template"  # template | mistral
    verified: bool = True
    dropped_claims: int = 0


class Briefing(BaseModel):
    cell: str
    location: dict
    risk: RiskState
    actions: list[Action]
    zone: ZoneInfo
    history: CellProfile
    conditions: dict
    nearby: list[NearbyEvent]
    narrative: Narrative
    freshness: dict
    disclaimer: str


class Subscription(BaseModel):
    id: str
    channel: str  # imessage | telegram | webpush | sms
    handle_hash: str
    lang: str = "en"
    label: str | None = None
    lat: float
    lon: float
    radius_m: int = 300
    cells: list[str]
    min_level: Level = Level.high
    status: str = "pending"  # pending | active | stopped | invalid
    created_at: datetime
    confirmed_at: datetime | None = None
