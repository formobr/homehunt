"""Settings (config.yaml) and .env loading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


@dataclass(frozen=True)
class SearchSettings:
    bounds: dict[str, float] = field(
        default_factory=lambda: {
            "west": -80.36, "east": -79.69, "south": 40.19, "north": 40.68,
        }
    )
    max_rent: int = 1500
    min_beds: int = 2
    in_unit_laundry: bool = True
    region: str = "Allegheny County, PA (Pittsburgh)"
    fb_city: str = "pittsburgh"  # Facebook Marketplace city slug


@dataclass(frozen=True)
class ReportSettings:
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as exc:
            raise ValueError(f"invalid report.timezone: {self.timezone!r}") from exc

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class PollSettings:
    interval_min: int = 15
    max_details_per_run: int = 30
    detail_delay_s: tuple[float, float] = (2, 5)


@dataclass(frozen=True)
class NotifySettings:
    price_drop_min_usd: int = 200
    price_drop_min_pct: float = 5
    min_reliability: int = 70
    min_value: int = 70
    min_score: float = 55


@dataclass(frozen=True)
class HealthSettings:
    fail_runs_before_alert: int = 2


@dataclass(frozen=True)
class AiSettings:
    triage_model: str = "haiku"
    judge_model: str = "opus"
    max_photos: int = 8
    timeout_s: float = 240


@dataclass(frozen=True)
class ScoreSettings:
    cost_best: int = 1100
    cost_worst: int = 1800
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "cost": 50, "safety": 12, "parking": 12,
            "income": 8, "type": 7, "baths": 6,
        }
    )


@dataclass(frozen=True)
class Settings:
    search: SearchSettings = field(default_factory=SearchSettings)
    score: ScoreSettings = field(default_factory=ScoreSettings)
    ai: AiSettings = field(default_factory=AiSettings)
    poll: PollSettings = field(default_factory=PollSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    health: HealthSettings = field(default_factory=HealthSettings)
    report: ReportSettings = field(default_factory=ReportSettings)


_SECTIONS = {f.name: f.default_factory for f in fields(Settings)}


def _build(cls: type, name: str, data: Any):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError(f"section {name!r} must be a mapping")
    known = {f.name for f in fields(cls)}
    for key in data:
        if key not in known:
            raise ValueError(f"unknown config key: {name}.{key}")
    kwargs = dict(data)
    if "detail_delay_s" in kwargs:
        kwargs["detail_delay_s"] = tuple(kwargs["detail_delay_s"])
    return cls(**kwargs)


_ENV_PREFIX = "HOMEHUNT__"


def _apply_env(raw: dict, env: Mapping[str, str]) -> None:
    """Merge HOMEHUNT__<SECTION>__<KEY>=<yaml value> overrides into raw."""
    for name, text in env.items():
        if not name.upper().startswith(_ENV_PREFIX):
            continue
        parts = name.lower().split("__")
        if len(parts) != 3:
            raise ValueError(f"unknown config key: {name}")
        _, section, key = parts
        value = yaml.safe_load(text)
        current = raw.get(section)
        if current is None:
            current = raw[section] = {}
        if isinstance(current, dict):
            current[key] = value


def load_settings(
    path: str | Path = "config.yaml", env: Mapping[str, str] | None = None
) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError("config root must be a mapping")
    _apply_env(raw, os.environ if env is None else env)
    for key in raw:
        if key not in _SECTIONS:
            raise ValueError(f"unknown config key: {key}")
    sections = {
        f.name: _build(f.default_factory, f.name, raw.get(f.name))
        for f in fields(Settings)
    }
    return Settings(**sections)


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Parse KEY=VALUE lines; set os.environ only for keys not already set."""
    result: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
        os.environ.setdefault(key, value)
    return result
