from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


ROOT = Path(__file__).resolve().parents[1]
_load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("NOVEL_STUDIO_DATA_DIR", str(ROOT / "data")))
    db_path: str = os.getenv("NOVEL_STUDIO_DB_PATH", str(ROOT / "data" / "novel-studio.db"))
    default_provider: str = os.getenv("NOVEL_STUDIO_DEFAULT_PROVIDER", "deepseek")
    quality_threshold: float = _float("NOVEL_STUDIO_QUALITY_THRESHOLD", 85.0)
    max_revisions: int = _int("NOVEL_STUDIO_MAX_REVISIONS", 2)
    max_agent_steps: int = _int("NOVEL_STUDIO_MAX_AGENT_STEPS", 10)
    max_reference_chars: int = _int("NOVEL_STUDIO_MAX_REFERENCE_CHARS", 120_000)
    max_vision_total_mb: int = _int("NOVEL_STUDIO_MAX_VISION_TOTAL_MB", 24)
    max_upload_mb: int = _int("NOVEL_STUDIO_MAX_UPLOAD_MB", 32)
    # Story-mutation leases are renewed by durable run events. Keep this comfortably
    # above the 5-minute provider HTTP timeout, but short enough to self-heal after a crash.
    write_lease_minutes: int = _int("NOVEL_STUDIO_WRITE_LEASE_MINUTES", 10)

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.6")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_base_url: str = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_base_url: str = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    custom_api_key: str = os.getenv("CUSTOM_API_KEY", "")
    custom_base_url: str = os.getenv("CUSTOM_BASE_URL", "")
    custom_model: str = os.getenv("CUSTOM_MODEL", "")


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
