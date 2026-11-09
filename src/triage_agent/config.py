"""Runtime configuration, read from environment variables (prefix ``TRIAGE_``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

READ_ONLY_TOOLS: tuple[str, ...] = (
    "prometheus_query",
    "prometheus_range_query",
    "k8s_get_pods",
    "k8s_describe_pod",
    "k8s_get_events",
    "k8s_rollout_history",
    "loki_search",
    "runbook_lookup",
)


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRIAGE_", env_file=".env", extra="ignore")

    # LLM
    llm_provider: Literal["fake", "anthropic"] = "fake"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 120.0
    llm_fallbacks: Literal["default", "off"] = "default"

    # Agent budgets
    max_steps: int = Field(default=8, ge=1, le=50)
    max_tool_calls: int = Field(default=16, ge=1, le=200)
    token_budget: int = Field(default=120_000, ge=1_000)
    tool_timeout_seconds: float = Field(default=10.0, gt=0)
    tool_result_max_chars: int = Field(default=6000, ge=500)
    allowed_tools: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(READ_ONLY_TOOLS)
    )

    # Correlation
    correlation_window_seconds: int = 300
    correlation_group_by: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["alertname", "namespace", "workload"]
    )
    debounce_seconds: float = 5.0
    max_concurrent_triages: int = 2

    # Data sources
    prometheus_url: str = "http://localhost:9090"
    prometheus_external_url: str | None = None
    loki_url: str = "http://localhost:3100"
    kubernetes_mode: Literal["auto", "off"] = "off"
    kubernetes_namespaces: Annotated[list[str], NoDecode] = Field(default_factory=list)
    runbook_dir: Path = Path("runbooks")

    # Service
    webhook_token: str | None = None
    approval_token: str | None = None
    audit_log_path: Path = Path("var/audit.jsonl")

    # Optional cost estimate inputs (USD per million tokens). No defaults on purpose.
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None
    price_cache_read_per_mtok: float | None = None
    price_cache_write_per_mtok: float | None = None

    @field_validator(
        "allowed_tools", "correlation_group_by", "kubernetes_namespaces", mode="before"
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("allowed_tools")
    @classmethod
    def _only_known_tools(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(READ_ONLY_TOOLS))
        if unknown:
            raise ValueError(f"unknown tools in allow-list: {unknown}")
        return value

    @field_validator(
        "webhook_token",
        "approval_token",
        "prometheus_external_url",
        "price_input_per_mtok",
        "price_output_per_mtok",
        "price_cache_read_per_mtok",
        "price_cache_write_per_mtok",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value
