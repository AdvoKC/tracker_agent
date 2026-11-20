"""Dashboard overrides of the YAML config (PRD §8b).

The YAML files stay the defaults. A superadmin may override a WHITELISTED set of keys in the
dashboard; overrides live in ``app_settings`` and are applied on top of the YAML every time a
command loads its config (``effective_config``). Every value is parsed, range-checked, and the
whole config re-validated before it is stored; every change is audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.config import AppConfig, NewsSourcesConfig, TunablesConfig
from promise_monitor.db.models import AppSetting, User
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    kind: str  # bool | int | float | outlets
    help: str
    lo: float | None = None
    hi: float | None = None


EDITABLE: tuple[Spec, ...] = (
    Spec(
        "publish.enabled",
        "Publishing on",
        "bool",
        "Off = shadow mode: approvals are held, nothing is published.",
    ),
    Spec(
        "classifier.news_threshold",
        "Article threshold",
        "int",
        "Score a change needs to become an article (status changes always do).",
        0,
        100,
    ),
    Spec("news.enabled", "News Scout on", "bool", "Search the news for developments on promises."),
    Spec(
        "news.max_calls_per_run",
        "News Scout calls per run",
        "int",
        "Each call ≈ $0.1-0.3 of Claude usage.",
        0,
        100,
    ),
    Spec(
        "news.batch_size",
        "Promises per News Scout call",
        "int",
        "Promises searched together (same meter).",
        1,
        25,
    ),
    Spec(
        "news.lookback_days",
        "News lookback (days)",
        "int",
        "Never report news older than this.",
        1,
        365,
    ),
    Spec(
        "news.min_relevance",
        "News minimum relevance",
        "float",
        "0-1; results below are rejected.",
        0,
        1,
    ),
    Spec(
        "verifier.relevance_min_confidence",
        "Source relevance minimum",
        "float",
        "0-1; sources below are not citable.",
        0,
        1,
    ),
    Spec(
        "review.stale_after_days",
        "Stale after (days)",
        "int",
        "Undecided drafts are marked stale after this.",
        1,
        60,
    ),
    Spec(
        "monitor.anomaly_change_ratio",
        "Anomaly guard ratio",
        "float",
        "Share of a meter's promises changing at once that pauses storing.",
        0.05,
        1,
    ),
    Spec(
        "outlets",
        "Outlet allowlist",
        "outlets",
        "News outlets every meter may cite (one per line: domain | name).",
    ),
)
SPECS = {s.key: s for s in EDITABLE}


class SettingError(ValueError):
    pass


def parse_value(spec: Spec, raw: Any) -> Any:
    try:
        if spec.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if spec.kind == "int":
            value: float = int(str(raw).strip())
        elif spec.kind == "float":
            value = float(str(raw).strip())
        elif spec.kind == "outlets":
            return list(raw)
        else:  # pragma: no cover
            raise SettingError(f"unknown kind {spec.kind}")
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{spec.label}: not a valid {spec.kind}") from exc
    if (spec.lo is not None and value < spec.lo) or (spec.hi is not None and value > spec.hi):
        raise SettingError(f"{spec.label}: must be between {spec.lo:g} and {spec.hi:g}")
    return value


def apply_overrides(base: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    tunables = base.tunables.model_dump(mode="json")
    sources = base.news_sources
    for key, value in overrides.items():
        if key == "outlets":
            sources = NewsSourcesConfig.model_validate(
                {"outlets": value, "official_suffixes": list(base.news_sources.official_suffixes)}
            )
            continue
        section, field = key.split(".", 1)
        tunables[section][field] = value
    return base.model_copy(
        update={"tunables": TunablesConfig.model_validate(tunables), "news_sources": sources}
    )


def current_overrides(session: Session) -> dict[str, Any]:
    return {r.key: r.value for r in session.scalars(select(AppSetting)) if r.key in SPECS}


def effective_config(session: Session, base: AppConfig) -> AppConfig:
    """YAML + dashboard overrides. A bad stored override is skipped (logged), never fatal."""
    cfg = base
    for key, value in current_overrides(session).items():
        try:
            cfg = apply_overrides(cfg, {key: value})
        except (ValidationError, KeyError, ValueError) as exc:
            log.error("setting_override_invalid", key=key, error=str(exc)[:200])
    return cfg


def current_value(cfg: AppConfig, key: str) -> Any:
    if key == "outlets":
        return [o.model_dump() for o in cfg.news_sources.outlets]
    section, field = key.split(".", 1)
    return getattr(getattr(cfg.tunables, section), field)


def set_setting(
    session: Session, base: AppConfig, key: str, raw: Any, actor: User, now: datetime
) -> None:
    access.require_superadmin(actor)
    spec = SPECS.get(key)
    if spec is None:
        raise SettingError("that setting can't be changed from the dashboard")
    value = parse_value(spec, raw)
    overrides = {**current_overrides(session), key: value}
    try:
        apply_overrides(base, overrides)  # whole config must still validate
    except ValidationError as exc:
        raise SettingError(
            f"{spec.label}: rejected ({exc.errors()[0].get('msg', 'invalid')})"
        ) from exc
    before = current_value(effective_config(session, base), key)
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_by=actor.user_id, updated_at=now))
    else:
        row.value, row.updated_by, row.updated_at = value, actor.user_id, now
    if before != value:
        access.audit(
            session,
            actor,
            "settings.set",
            "setting",
            key,
            now,
            before={"value": before},
            after={"value": value},
        )
    session.flush()


def reset_setting(session: Session, base: AppConfig, key: str, actor: User, now: datetime) -> None:
    access.require_superadmin(actor)
    row = session.get(AppSetting, key)
    if row is None:
        return
    before = row.value
    session.delete(row)
    access.audit(
        session,
        actor,
        "settings.reset",
        "setting",
        key,
        now,
        before={"value": before},
        after={"value": current_value(base, key)},
    )
    session.flush()
