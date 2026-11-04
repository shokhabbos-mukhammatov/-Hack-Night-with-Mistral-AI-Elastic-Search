"""The InputsProvider seam: the same risk engine runs on live data or on an archived storm day.

LiveInputs    reads the live indices (profiles from hg-cell-profiles, conditions without a replay tag)
ArchiveInputs reads replay-tagged conditions + real 311 history, with profiles recomputed as of the replay
              date, so the replay can only see what was knowable at each instant.
"""

from datetime import UTC, datetime
from typing import Protocol

from elasticsearch import NotFoundError

from highground import tools
from highground.es import ES
from highground.geo import cell_center, cell_of, nearest_weather_point
from highground.models import CellProfile, Inputs, ZoneInfo
from highground.profiles import compute_profiles, profile_from_doc


class InputsProvider(Protocol):
    def inputs_for(self, cell: str, as_of: datetime, point: tuple[float, float] | None = None) -> Inputs: ...


class LiveInputs:
    def __init__(self, es: ES):
        self.es = es

    def profile(self, cell: str) -> CellProfile:
        try:
            src = self.es.client.get(index=self.es.ix("cell-profiles"), id=cell)["_source"]
            return profile_from_doc(src)
        except NotFoundError:
            lat, lon = cell_center(cell)
            z = tools.zone_at(self.es, lat, lon)
            return CellProfile(cell=cell, zone=ZoneInfo(in_100yr=z["in_100yr"], slr24=z["slr24"], shoreline=z["slr24"]))

    def inputs_for(self, cell: str, as_of: datetime | None = None, point: tuple[float, float] | None = None) -> Inputs:
        as_of = as_of or datetime.now(UTC)
        lat, lon = point or cell_center(cell)
        return Inputs(
            cell=cell,
            as_of=as_of,
            profile=self.profile(cell),
            weather=tools.weather_at(self.es, nearest_weather_point(lat, lon), as_of),
            alerts=tools.active_alerts(self.es, as_of),
            tide=tools.tide_at(self.es, as_of),
            events=tools.nearby_events(self.es, lat, lon, as_of),
        )


class ArchiveInputs:
    def __init__(self, es: ES, replay_date: str):
        self.es = es
        self.replay = replay_date
        start = datetime.fromisoformat(replay_date).replace(tzinfo=UTC)
        self._profiles, self.meta = compute_profiles(es, start)  # strictly before the replay day

    def profile(self, cell: str) -> CellProfile:
        return self._profiles.get(cell) or CellProfile(cell=cell)

    def cells(self) -> list[str]:
        return list(self._profiles)

    def inputs_for(self, cell: str, as_of: datetime, point: tuple[float, float] | None = None) -> Inputs:
        lat, lon = point or cell_center(cell)
        return Inputs(
            cell=cell,
            as_of=as_of,
            profile=self.profile(cell),
            weather=tools.weather_at(self.es, nearest_weather_point(lat, lon), as_of, replay=self.replay),
            alerts=tools.active_alerts(self.es, as_of, replay=self.replay),
            tide=tools.tide_at(self.es, as_of, replay=self.replay),
            events=tools.nearby_events(self.es, lat, lon, as_of, replay=self.replay),
        )


def cell_for(lat: float, lon: float) -> str:
    return cell_of(lat, lon)
