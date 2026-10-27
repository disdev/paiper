"""Immutable dashboard data. Instants are aware UTC datetimes; dates stay dates."""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class WeatherCurrent:
    observed_at: datetime
    valid_until: datetime
    temperature_f: float
    weather_code: int
    wind_mph: float


@dataclass(frozen=True)
class WeatherHour:
    at: datetime
    temperature_f: float
    rain_probability: float
    wind_mph: float


@dataclass(frozen=True)
class WeatherDay:
    date: date
    high_f: float
    low_f: float
    sunrise: datetime | None
    sunset: datetime | None


@dataclass(frozen=True)
class Weather:
    latitude: float
    longitude: float
    timezone: str
    current: WeatherCurrent
    hourly: tuple[WeatherHour, ...]
    daily: tuple[WeatherDay, ...]
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class Alert:
    id: str
    issued_at: datetime
    expires_at: datetime
    effective_at: datetime
    severity: str
    event: str
    headline: str


@dataclass(frozen=True)
class Alerts:
    updated_at: datetime | None
    items: tuple[Alert, ...]

    def active_at(self, now: datetime) -> tuple[Alert, ...]:
        return tuple(
            item for item in self.items if item.effective_at <= now < item.expires_at
        )


@dataclass(frozen=True)
class GameTeam:
    id: int
    name: str
    score: int | None
    probable_pitcher: str | None


@dataclass(frozen=True)
class Game:
    id: int
    date: date
    start_at: datetime | None
    start_time_tbd: bool
    detailed_status: str
    abstract_status: str
    doubleheader: str | None
    game_number: int | None
    home: GameTeam
    away: GameTeam
    inning: int | None
    inning_half: str | None


@dataclass(frozen=True)
class Schedule:
    start_date: date
    end_date: date
    games: tuple[Game, ...]


@dataclass(frozen=True)
class Quote:
    symbol: str
    currency: str
    price: float
    observed_at: datetime
    previous_close: float | None
    previous_close_date: date | None

    @property
    def change(self) -> float | None:
        return None if self.previous_close is None else self.price - self.previous_close

    @property
    def change_percent(self) -> float | None:
        return (
            None if not self.previous_close else 100 * self.change / self.previous_close
        )


@dataclass(frozen=True)
class ExchangeRate:
    date: date
    base: str
    quote: str
    rate: float


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    link: str
    published_at: datetime | None


@dataclass(frozen=True)
class Feed:
    title: str | None
    items: tuple[Article, ...]


@dataclass(frozen=True)
class Aircraft:
    id: str
    callsign: str | None
    registration: str | None
    aircraft_type: str | None
    altitude_ft: float | None
    on_ground: bool | None
    distance_nm: float | None
    bearing_degrees: float | None
    latitude: float | None
    longitude: float | None
    observed_at: datetime | None


@dataclass(frozen=True)
class Flights:
    observed_at: datetime
    aircraft: tuple[Aircraft, ...]

    def current_at(self, now: datetime) -> tuple[Aircraft, ...]:
        return tuple(
            plane
            for plane in self.aircraft
            if plane.observed_at is not None
            and plane.latitude is not None
            and plane.longitude is not None
            and 0 <= (now - plane.observed_at).total_seconds() <= 120
        )


Payload = Weather | Alerts | Schedule | Quote | ExchangeRate | Feed | Flights


@dataclass(frozen=True)
class SourceRecord:
    source_key: str
    payload: Payload | None = None
    fetched_at: datetime | None = None
    observed_at: datetime | date | None = None
    valid_until: datetime | None = None
    last_attempt_at: datetime | None = None
    next_fetch_at: datetime | None = None
    failures: int = 0
    error: str | None = None
    context: str | None = None
    attempt_context: str | None = None

    def is_due(self, now: datetime) -> bool:
        return self.next_fetch_at is None or now >= self.next_fetch_at

    def is_stale(self, now: datetime) -> bool:
        return (
            self.payload is None or self.valid_until is None or now >= self.valid_until
        )

    def status(self, now: datetime) -> str:
        if self.payload is None:
            return "unavailable"
        if self.error is not None:
            return "degraded"
        return "stale" if self.is_stale(now) else "ok"
