import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from gummy.errors import ConfigurationError


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="sqlite:///./data/gummy.db", repr=False)
    app_env: str = "local"
    rapidapi_key: str = field(default="", repr=False)
    free_plan_confirmed: bool = False
    total_limit: int = 30
    daily_limit: int = 5
    api_token: str = field(default="", repr=False)
    active_plan_path: str = ""
    scheduler_config_path: str = ""

    def __post_init__(self):
        if self.app_env not in {"local", "cloud"}:
            raise ConfigurationError("APP_ENV must be local or cloud")
        if not 1 <= self.daily_limit <= self.total_limit <= 30:
            raise ConfigurationError("MVP limits require 1 <= daily <= total <= 30")
        if self.api_token and len(self.api_token) < 32:
            raise ConfigurationError("GUMMY_API_TOKEN must contain at least 32 characters")
        if self.app_env == "cloud" and not self.database_url.startswith("postgresql+psycopg://"):
            raise ConfigurationError("Cloud mode requires persistent PostgreSQL")

    @classmethod
    def from_env(cls):
        # Local developer convenience only; cloud secrets must come from the runtime environment.
        if os.getenv("APP_ENV", "local") == "local":
            load_dotenv(".env", override=False)
        try:
            return cls(
                database_url=os.getenv("DATABASE_URL", "sqlite:///./data/gummy.db"),
                app_env=os.getenv("APP_ENV", "local"),
                rapidapi_key=os.getenv("RAPIDAPI_KEY", "").strip(),
                free_plan_confirmed=os.getenv("RAPIDAPI_FREE_PLAN_CONFIRMED", "false") == "true",
                total_limit=int(os.getenv("LIVE_TOTAL_LIMIT", "30")),
                daily_limit=int(os.getenv("LIVE_DAILY_LIMIT", "5")),
                api_token=os.getenv("GUMMY_API_TOKEN", ""),
                active_plan_path=os.getenv("GUMMY_ACTIVE_PLAN", "").strip(),
                scheduler_config_path=os.getenv("GUMMY_SCHEDULER_CONFIG", "").strip(),
            )
        except ValueError:
            raise ConfigurationError("Quota limits must be integers") from None

    def validate_live(self):
        if not self.rapidapi_key:
            raise ConfigurationError("Set RAPIDAPI_KEY in the local .env before live collection")
        if not self.free_plan_confirmed:
            raise ConfigurationError("Confirm your free hard-cap subscription in .env first")

    def validate_bind(self, host: str):
        if (
            self.app_env == "cloud" or host not in {"127.0.0.1", "::1", "localhost"}
        ) and not self.api_token:
            raise ConfigurationError("Non-loopback/cloud API requires GUMMY_API_TOKEN")
