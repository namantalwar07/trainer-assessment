"""Runtime configuration. Every scoring rule and threshold lives here or in config/rubric_v1.json."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONLY_ALLOWED_MODEL = "gemini-3.8-flash"
LANGUAGES = [
    "Hindi", "English", "Hindi-English (mixed)", "Marathi", "Gujarati", "Bengali", "Tamil",
    "Telugu", "Kannada", "Malayalam", "Punjabi", "Odia", "Assamese", "Other",
]


class ConfigError(RuntimeError):
    pass


def _bool(v: str | None, default: bool) -> bool:
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: Path, env: dict[str, str]) -> None:
    """Tiny .env reader. Never overrides variables already set in the environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    # --- mode and secrets ---
    mock_gemini: bool = True
    gemini_api_key: str | None = None
    model: str = ONLY_ALLOWED_MODEL
    session_secret: str = ""
    cookie_secure: bool = False  # set COOKIE_SECURE=true behind HTTPS
    public_base_url: str = "http://127.0.0.1:8000"  # used for report links in the Excel workbook
    # --- storage ---
    database_url: str = ""
    data_dir: Path = ROOT / "data"
    storage_backend: str = "local"  # local | gcs
    gcs_bucket: str | None = None
    max_upload_bytes: int = 1024**3
    retention_days: int = 30
    delete_video_after_assessment: bool = False  # frees disk: reviewers lose the "Watch video" link
    min_free_disk_gb: float = 1.5
    # --- provider path ---
    corpus_api_path: str = "interactions"  # "batch" stays disabled until the spike passes
    poll_interval_seconds: float = 15.0
    interaction_timeout_seconds: float = 3600.0
    extraction_fallback: bool = True  # fall back to generate_content if Interactions fails
    video_resolution: str = ""  # "", low, medium, high (empty = API default)
    # --- worker ---
    embedded_worker: bool = True
    worker_concurrency: int = 2
    max_validation_retries: int = 3
    backoff_base_seconds: float = 2.0
    # --- scoring policy (all configurable) ---
    score_floor_mode: str = "accept"  # accept | zero_anchor
    band_rounding_rule: str = "threshold"  # threshold | round_half_up
    min_coverage_points: float = 70.0
    low_conf_weight_threshold: int = 10
    min_sessions_for_aggregate: int = 3
    # --- measured signals (FFmpeg) ---
    silence_noise_db: float = -45.0
    silence_min_seconds: float = 20.0
    run_freeze_detect: bool = True
    freeze_min_seconds: float = 3.0
    pts_jump_seconds: float = 5.0
    invoice_shortfall_minutes: float = 5.0  # flag when the recording covers this much less than invoiced
    clock_tolerance_seconds: int = 120  # allowed drift between the on-screen clock and recording time
    # --- effective teaching time ---
    min_effective_minutes: float = 60.0
    gap_tolerance_seconds: int = 120  # pauses up to this long are forgiven (counted as effective)
    gap_flag_seconds: int = 150  # pauses of at least this long are flagged (2.5 min)
    count_participant_activity_as_effective: bool = True  # trainer present while participants do an exercise
    silent_flag_seconds: int = 60  # trainer saying nothing for this long is listed for review
    technical_review_minutes: float = 10.0  # review if this much of the recording is a technical problem
    conduct_review_events: int = 3  # review if this many conduct events are observed
    # --- video policy ---
    duration_ok_min: int = 60
    duration_ok_max: int = 90
    duration_warn_low: int = 45
    duration_warn_high: int = 120
    mock_default_duration_min: int = 75
    # --- cost tracking (re-verify against the published price list) ---
    price_input_per_m_usd: float = 0.75
    price_output_per_m_usd: float = 3.75
    usd_inr: float = 90.0

    @property
    def zero_anchor(self) -> bool:
        return self.score_floor_mode == "zero_anchor"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'app.db'}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            merged: dict[str, str] = dict(os.environ)
            load_dotenv(ROOT / ".env", merged)
            env = merged
        g = env.get
        mock = _bool(g("MOCK_GEMINI"), True)
        data_dir = Path(g("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
        s = cls(
            mock_gemini=mock,
            gemini_api_key=g("GEMINI_API_KEY") or None,
            model=g("GEMINI_MODEL", ONLY_ALLOWED_MODEL),
            session_secret=g("SESSION_SECRET", "") or ("" if not mock else secrets.token_urlsafe(32)),
            cookie_secure=_bool(g("COOKIE_SECURE"), False),
            public_base_url=g("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            database_url=g("DATABASE_URL", ""),
            data_dir=data_dir,
            storage_backend=g("STORAGE_BACKEND", "local"),
            gcs_bucket=g("GCS_BUCKET") or None,
            max_upload_bytes=int(g("MAX_UPLOAD_BYTES", str(1024**3))),
            retention_days=int(g("RETENTION_DAYS", "30")),
            delete_video_after_assessment=_bool(g("DELETE_VIDEO_AFTER_ASSESSMENT"), False),
            min_free_disk_gb=float(g("MIN_FREE_DISK_GB", "1.5")),
            corpus_api_path=g("CORPUS_API_PATH", "interactions"),
            poll_interval_seconds=float(g("POLL_INTERVAL_SECONDS", "15")),
            interaction_timeout_seconds=float(g("INTERACTION_TIMEOUT_SECONDS", "3600")),
            extraction_fallback=_bool(g("EXTRACTION_FALLBACK"), True),
            video_resolution=g("VIDEO_RESOLUTION", ""),
            embedded_worker=_bool(g("EMBEDDED_WORKER"), True),
            worker_concurrency=int(g("WORKER_CONCURRENCY", "2")),
            max_validation_retries=int(g("MAX_VALIDATION_RETRIES", "3")),
            backoff_base_seconds=float(g("BACKOFF_BASE_SECONDS", "2")),
            score_floor_mode=g("SCORE_FLOOR_MODE", "accept"),
            band_rounding_rule=g("BAND_ROUNDING_RULE", "threshold"),
            min_coverage_points=float(g("MIN_COVERAGE_POINTS", "70")),
            low_conf_weight_threshold=int(g("LOW_CONF_WEIGHT_THRESHOLD", "10")),
            min_sessions_for_aggregate=int(g("MIN_SESSIONS_FOR_AGGREGATE", "3")),
            silence_noise_db=float(g("SILENCE_NOISE_DB", "-45")),
            silence_min_seconds=float(g("SILENCE_MIN_SECONDS", "20")),
            run_freeze_detect=_bool(g("RUN_FREEZE_DETECT"), True),
            freeze_min_seconds=float(g("FREEZE_MIN_SECONDS", "3")),
            pts_jump_seconds=float(g("PTS_JUMP_SECONDS", "5")),
            invoice_shortfall_minutes=float(g("INVOICE_SHORTFALL_MINUTES", "5")),
            clock_tolerance_seconds=int(g("CLOCK_TOLERANCE_SECONDS", "120")),
            min_effective_minutes=float(g("MIN_EFFECTIVE_MINUTES", "60")),
            gap_tolerance_seconds=int(g("GAP_TOLERANCE_SECONDS", "120")),
            gap_flag_seconds=int(g("GAP_FLAG_SECONDS", "150")),
            count_participant_activity_as_effective=_bool(g("COUNT_PARTICIPANT_ACTIVITY_AS_EFFECTIVE"), True),
            silent_flag_seconds=int(g("TRAINER_SILENT_FLAG_SECONDS", "60")),
            technical_review_minutes=float(g("TECHNICAL_REVIEW_MINUTES", "10")),
            conduct_review_events=int(g("CONDUCT_REVIEW_EVENTS", "3")),
            mock_default_duration_min=int(g("MOCK_DEFAULT_DURATION_MIN", "75")),
            price_input_per_m_usd=float(g("PRICE_INPUT_PER_M_USD", "0.75")),
            price_output_per_m_usd=float(g("PRICE_OUTPUT_PER_M_USD", "3.75")),
            usd_inr=float(g("USD_INR", "90")),
        )
        s.validate_startup()
        return s

    def validate_startup(self) -> None:
        """Fail loudly. Live mode without a key must never start."""
        if self.model != ONLY_ALLOWED_MODEL:
            raise ConfigError(f"Only {ONLY_ALLOWED_MODEL} is enabled for now (got {self.model!r}).")
        if self.score_floor_mode not in {"accept", "zero_anchor"}:
            raise ConfigError("SCORE_FLOOR_MODE must be 'accept' or 'zero_anchor'.")
        if self.band_rounding_rule not in {"threshold", "round_half_up"}:
            raise ConfigError("BAND_ROUNDING_RULE must be 'threshold' or 'round_half_up'.")
        if self.storage_backend not in {"local", "gcs"}:
            raise ConfigError("STORAGE_BACKEND must be 'local' or 'gcs'.")
        if self.storage_backend == "gcs" and not self.gcs_bucket:
            raise ConfigError("STORAGE_BACKEND=gcs requires GCS_BUCKET.")
        if self.gap_tolerance_seconds > self.gap_flag_seconds:
            raise ConfigError("GAP_TOLERANCE_SECONDS must not exceed GAP_FLAG_SECONDS.")
        if self.video_resolution not in {"", "low", "medium", "high"}:
            raise ConfigError("VIDEO_RESOLUTION must be empty, low, medium or high.")
        if self.corpus_api_path == "batch":
            raise ConfigError(
                "CORPUS_API_PATH=batch is disabled until spikes/test_batch_video.py passes. "
                "Use 'interactions'."
            )
        if self.corpus_api_path != "interactions":
            raise ConfigError("CORPUS_API_PATH must be 'interactions'.")
        if not self.mock_gemini:
            if not self.gemini_api_key:
                raise ConfigError("Live mode (MOCK_GEMINI=false) requires GEMINI_API_KEY. Refusing to start.")
            if not self.session_secret:
                raise ConfigError("Live mode requires SESSION_SECRET (a long random string).")
