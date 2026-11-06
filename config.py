"""Environment-backed configuration for the CrewPilot Switch agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from switch.credentials import load_saved_credentials

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    switch_api_url: str
    switch_agent_id: str = field(repr=False)
    switch_api_key: str = field(repr=False)
    switch_agent_name: str
    llm_provider: str
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = field(default=None, repr=False)
    anthropic_model: str = "claude-sonnet-4-5"
    groq_api_key: str | None = field(default=None, repr=False)
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    github_token: str | None = field(default=None, repr=False)
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        agent_name: str | None = None,
        require_llm: bool = True,
    ) -> "Settings":
        if agent_name:
            saved = load_saved_credentials(agent_name)
        else:
            saved = load_saved_credentials()

        provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
        if provider not in {"openai", "anthropic", "groq"}:
            raise ConfigError(
                "LLM_PROVIDER must be 'openai', 'anthropic', or 'groq'."
            )

        resolved_name = (
            (os.getenv("SWITCH_AGENT_NAME") if not agent_name else None)
            or saved.get("agent_name")
            or agent_name
            or "crewpilot"
        ).strip()

        settings = cls(
            switch_api_url=(
                os.getenv("SWITCH_API_URL") or saved.get("api_url") or ""
            ).strip().rstrip("/"),
            switch_agent_id=(
                os.getenv("SWITCH_AGENT_ID") or saved.get("agent_id") or ""
            ).strip(),
            switch_api_key=(
                os.getenv("SWITCH_API_KEY") or saved.get("api_key") or ""
            ).strip(),
            switch_agent_name=resolved_name,
            llm_provider=provider,
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            groq_api_key=os.getenv("GROQ_API_KEY") or None,
            groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            groq_base_url=(
                os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
                or "https://api.groq.com/openai/v1"
            ).rstrip("/"),
            github_token=os.getenv("GITHUB_TOKEN") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
        settings.validate(require_llm=require_llm)
        return settings

    def validate(self, require_llm: bool = True) -> None:
        if not self.switch_api_url.startswith(("http://", "https://")):
            raise ConfigError(
                "SWITCH_API_URL must be the Switch agent bridge/API URL, "
                "for example http://localhost:8000."
            )
        if not self.switch_agent_id or not self.switch_api_key:
            raise ConfigError(
                "Switch agent credentials are missing. Run "
                "`python -m switch.register_agent --name crewpilot` with "
                "SWITCH_REGISTRATION_TOKEN set, or provide SWITCH_AGENT_ID and "
                "SWITCH_API_KEY in .env."
            )
        if require_llm:
            if self.llm_provider == "openai" and not self.openai_api_key:
                raise ConfigError("OPENAI_API_KEY is required when LLM_PROVIDER=openai.")
            if self.llm_provider == "anthropic" and not self.anthropic_api_key:
                raise ConfigError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic.")
            if self.llm_provider == "groq" and not self.groq_api_key:
                raise ConfigError("GROQ_API_KEY is required when LLM_PROVIDER=groq.")
