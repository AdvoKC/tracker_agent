"""Configuration: secrets/machine values from env (.env), tunables from config/*.yaml.

Everything is validated by pydantic so a typo fails at startup, not mid-run.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PositiveFloat, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    """Environment-driven settings. Variables are prefixed ``PM_``."""

    model_config = SettingsConfigDict(env_prefix="PM_", env_file=".env", extra="ignore")

    env: Literal["dev", "prod", "test"] = "dev"
    database_url: str | None = None
    admin_email: str | None = None
    log_dir: Path = Path("logs")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    config_dir: Path = DEFAULT_CONFIG_DIR


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiConfig(_Strict):
    base_url: HttpUrl
    public_site_url: HttpUrl
    public_promise_url: str
    user_agent: str
    min_interval_seconds: PositiveFloat
    timeout_seconds: PositiveFloat
    max_retries: int = Field(ge=0)
    backoff_base_seconds: PositiveFloat
    max_retry_after_seconds: PositiveFloat


class MonitorConfig(_Strict):
    full_sweep_weekday: int = Field(ge=0, le=6)
    anomaly_change_ratio: float = Field(gt=0, le=1)
    anomaly_min_changes: PositiveInt


class BaseScores(_Strict):
    status_change: int = Field(ge=0, le=100)
    new_evidence: int = Field(ge=0, le=100)
    new_promise: int = Field(ge=0, le=100)
    news_update: int = Field(ge=0, le=100)


class ClassifierConfig(_Strict):
    news_threshold: int = Field(ge=0, le=100)
    cosmetic_max_edit_ratio: float = Field(ge=0, le=1)
    llm_review_max_edit_ratio: float = Field(ge=0, le=1)
    llm_max_adjustment: int = Field(ge=0, le=50)
    base_scores: BaseScores
    bonus_final_status: int = Field(ge=0)
    bonus_priority_meter: int = Field(ge=0)
    priority_meter_max_rank: PositiveInt
    bonus_high_priority_promise: int = Field(ge=0)
    bonus_news_high_confidence: int = Field(ge=0)


class NewsConfig(_Strict):
    enabled: bool
    batch_size: PositiveInt
    max_calls_per_run: int = Field(ge=0)
    lookback_days: PositiveInt
    min_relevance: float = Field(ge=0, le=1)
    max_results_per_promise: PositiveInt


class LLMModels(_Strict):
    classifier: str
    news_scout: str
    default: str


class LLMConfig(_Strict):
    backend: Literal["claude_cli"]
    claude_bin: str
    timeout_seconds: PositiveFloat
    max_retries: int = Field(ge=0)
    models: LLMModels


class Outlet(_Strict):
    domain: str
    name: str


class NewsSourcesConfig(_Strict):
    outlets: tuple[Outlet, ...]
    official_suffixes: tuple[str, ...] = ()


class VerifierConfig(_Strict):
    timeout_seconds: PositiveFloat
    max_redirects: PositiveInt
    max_retries: int = Field(ge=0)
    per_host_interval_seconds: PositiveFloat
    max_parallel_hosts: PositiveInt
    relevance_min_confidence: float = Field(ge=0, le=1)
    cache_days: PositiveInt


class ArticleConfig(_Strict):
    target_words: PositiveInt
    tolerance_words: int = Field(ge=0)
    min_links: PositiveInt
    max_rewrites: int = Field(ge=0)
    dedupe_similarity: float = Field(gt=0, le=1)
    dedupe_window_days: PositiveInt

    @property
    def min_words(self) -> int:
        return self.target_words - self.tolerance_words

    @property
    def max_words(self) -> int:
        return self.target_words + self.tolerance_words


class ReviewConfig(_Strict):
    stale_after_days: PositiveInt


class TunablesConfig(_Strict):
    api: ApiConfig
    monitor: MonitorConfig
    classifier: ClassifierConfig
    verifier: VerifierConfig
    news: NewsConfig
    llm: LLMConfig
    article: ArticleConfig
    review: ReviewConfig
    timezone_display: str


class PriorityConfig(_Strict):
    default_rank: PositiveInt
    ranks: dict[str, PositiveInt]

    def rank_for(self, key: str) -> int:
        return self.ranks.get(key, self.default_rank)


class AppConfig(_Strict):
    tunables: TunablesConfig
    states: tuple[str, ...]
    priority: PriorityConfig
    banned_terms: tuple[str, ...]
    news_sources: NewsSourcesConfig


class ConfigError(RuntimeError):
    """Raised when a config file is missing or invalid."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at top level")
    return data


def load_app_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> AppConfig:
    """Load and validate all YAML config from ``config_dir``."""
    states = _read_yaml(config_dir / "states.yaml").get("states")
    if not isinstance(states, list) or not states:
        raise ConfigError("states.yaml: 'states' must be a non-empty list")
    if len(set(states)) != len(states):
        raise ConfigError("states.yaml: duplicate state names")

    terms = _read_yaml(config_dir / "banned_terms.yaml").get("terms", [])

    return AppConfig(
        tunables=TunablesConfig.model_validate(_read_yaml(config_dir / "settings.yaml")),
        states=tuple(states),
        priority=PriorityConfig.model_validate(_read_yaml(config_dir / "priority.yaml")),
        banned_terms=tuple(str(t).lower() for t in terms),
        news_sources=NewsSourcesConfig.model_validate(_read_yaml(config_dir / "news_sources.yaml")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
