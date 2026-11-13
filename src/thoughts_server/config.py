# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .build_sandbox import default_from_environment

DEFAULT_PUBLIC_BASE_URL = "https://thoughts.example.org"
# Bearer tokens are the only credential on a publicly reachable server.
MINIMUM_TOKEN_LENGTH = 32


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    database_url: str
    object_root: Path
    state_root: Path
    worktree_root: Path
    docs_root: Path | None
    comment_sync_bin: Path | None
    toolchain_key: str
    api_host: str
    api_port: int
    worker_poll_seconds: float
    build_timeout_seconds: int
    max_object_bytes: int
    allow_development_builds: bool
    auto_create_schema: bool
    bootstrap_tokens: dict[str, str]
    source_bundle_root: Path | None = None
    anchor_max_attempts: int = 5
    build_parallel_jobs: int = 5
    review_reanchor_max_attempts: int = 3
    renderer_backend: str = "development"
    logical_comment_promotion_enabled: bool = False
    strict_review_correspondence_enabled: bool = False
    # protocol/agent-proposals.md 10.7.1: off at the first deploy (compatible mode).
    proposal_decisions_strict: bool = False
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    auth_failure_limit: int = 10
    auth_failure_window_seconds: int = 300
    auth_lockout_seconds: int = 900
    auth_tracked_clients: int = 10_000
    # Which Library these settings belong to. One server can host several
    # Libraries (thoughts_server.libraries); "default" is the one configured
    # directly by the environment.
    library_id: str = "default"
    # Compile documents in a sandbox (build_sandbox). From the environment this is on wherever
    # the sandbox exists (Linux); only the default Library can turn it off, and every other
    # Library always has it on (libraries.derive_settings).
    sandbox_builds: bool = False
    # HTML editions (html_edition): built in the background after each PDF publication.
    html_editions_enabled: bool = True
    html_edition_timeout_seconds: int = 120
    # /docs, /redoc and /openapi.json: public pages that describe the whole API.
    api_docs_enabled: bool = True
    # Bytes one Library may keep in its object store; an upload that would exceed it is
    # refused with 507. 0 (the default) means no limit.
    library_storage_quota_bytes: int = 0
    # Proposal previews compiled at once by one Library's API (each is a renderer process).
    preview_parallel_jobs: int = 2
    # Builds one Library may have waiting to run. A client's build request beyond it is
    # refused with 429 until the queue drains. Builds of one target that the newest of
    # them will cover count as one; builds the server queues itself are never refused.
    max_queued_builds: int = 32
    # Heavy jobs (builds; proposal previews have their own, equal count) running at once
    # on this host across all Libraries. 0: as many as fit the processors when each uses
    # build_parallel_jobs of them (10 cores and 5 jobs: 2).
    host_build_slots: int = 0
    # The directory all Libraries' workers share for that limit (host_slots); by default
    # "build-slots" in the default Library's state folder.
    host_build_slot_dir: Path | None = None
    # Database connections one Library's API keeps open while idle, and how many more it
    # may open under load (closed again when returned). The most it uses is their sum.
    database_pool_size: int = 1
    database_max_overflow: int = 14
    # The same for each worker process of a Library.
    worker_database_pool_size: int = 1
    worker_database_max_overflow: int = 4
    # How long an idle build worker waits for a queue notification before it looks at the
    # database on its own (PostgreSQL; a notification still wakes it at once). Without
    # notifications (sqlite) it keeps polling every worker_poll_seconds.
    worker_idle_poll_seconds: float = 5.0
    # Start the export, anchor and review workers of a Library only while they have work
    # (on_demand_workers), from its build worker, instead of keeping three more processes
    # per Library. Their own services then only wait.
    workers_on_demand: bool = False
    # An on-demand worker ends after this long without work.
    worker_idle_exit_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> Settings:
        """The environment's settings, for the Library named by ``THOUGHTS_LIBRARY``.

        Without ``THOUGHTS_LIBRARY`` (or with the default Library's ID) these are the
        settings of the default Library, exactly as before Libraries existed.
        """
        base = cls.base_from_env()
        selected = os.environ.get("THOUGHTS_LIBRARY", "").strip()
        if not selected or selected == base.library_id:
            return base
        from .libraries import library_settings

        return library_settings(base, selected)

    @classmethod
    def base_from_env(cls) -> Settings:
        state_root = _path("THOUGHTS_STATE_ROOT", "./state")
        docs_value = os.environ.get("THOUGHTS_DOCS_ROOT", "").strip()
        helper_value = os.environ.get("THOUGHTS_COMMENT_SYNC_BIN", "").strip()
        source_bundle_value = os.environ.get("THOUGHTS_SOURCE_BUNDLE_OUTPUT", "").strip()
        raw_tokens = os.environ.get("THOUGHTS_BOOTSTRAP_TOKENS", "{}")
        try:
            bootstrap_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as error:
            raise ValueError("THOUGHTS_BOOTSTRAP_TOKENS must be a JSON object") from error
        if not isinstance(bootstrap_tokens, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in bootstrap_tokens.items()
        ):
            raise ValueError("THOUGHTS_BOOTSTRAP_TOKENS must map device IDs to tokens")
        validate_bootstrap_tokens(bootstrap_tokens)
        return cls(
            database_url=os.environ.get(
                "THOUGHTS_DATABASE_URL",
                f"sqlite:///{state_root / 'thoughts.db'}",
            ),
            object_root=_path("THOUGHTS_OBJECT_ROOT", "./objects"),
            state_root=state_root,
            worktree_root=_path("THOUGHTS_WORKTREE_ROOT", "./worktree"),
            docs_root=Path(docs_value).expanduser().resolve() if docs_value else None,
            comment_sync_bin=Path(helper_value).expanduser().resolve() if helper_value else None,
            toolchain_key=os.environ.get("THOUGHTS_TOOLCHAIN_KEY", "development"),
            api_host=os.environ.get("THOUGHTS_API_HOST", "127.0.0.1"),
            api_port=int(os.environ.get("THOUGHTS_API_PORT", "8080")),
            worker_poll_seconds=float(os.environ.get("THOUGHTS_WORKER_POLL_SECONDS", "1")),
            build_timeout_seconds=int(os.environ.get("THOUGHTS_BUILD_TIMEOUT_SECONDS", "14400")),
            max_object_bytes=int(os.environ.get("THOUGHTS_MAX_OBJECT_BYTES", str(512 * 1024 * 1024))),
            allow_development_builds=os.environ.get("THOUGHTS_ALLOW_DEVELOPMENT_BUILDS", "true").lower()
            in {"1", "true", "yes"},
            auto_create_schema=os.environ.get("THOUGHTS_AUTO_CREATE_SCHEMA", "true").lower()
            in {"1", "true", "yes"},
            bootstrap_tokens=bootstrap_tokens,
            source_bundle_root=(
                Path(source_bundle_value).expanduser().resolve()
                if source_bundle_value
                else None
            ),
            anchor_max_attempts=int(os.environ.get("THOUGHTS_ANCHOR_MAX_ATTEMPTS", "5")),
            build_parallel_jobs=max(
                1,
                int(os.environ.get("THOUGHTS_BUILD_PARALLEL_JOBS", "5")),
            ),
            renderer_backend=os.environ.get("THOUGHTS_RENDERER_BACKEND", "native").strip().lower(),
            review_reanchor_max_attempts=int(
                os.environ.get("THOUGHTS_REVIEW_REANCHOR_MAX_ATTEMPTS", "3")
            ),
            logical_comment_promotion_enabled=os.environ.get(
                "THOUGHTS_LOGICAL_COMMENT_PROMOTION_ENABLED", "false"
            ).lower()
            in {"1", "true", "yes"},
            strict_review_correspondence_enabled=os.environ.get(
                "THOUGHTS_STRICT_REVIEW_CORRESPONDENCE", "false"
            ).lower()
            in {"1", "true", "yes"},
            proposal_decisions_strict=proposal_decisions_strict_from_env(),
            public_base_url=normalize_public_base_url(
                os.environ.get("THOUGHTS_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL)
            ),
            auth_failure_limit=int(os.environ.get("THOUGHTS_AUTH_FAILURE_LIMIT", "10")),
            auth_failure_window_seconds=_positive_int("THOUGHTS_AUTH_FAILURE_WINDOW_SECONDS", 300),
            auth_lockout_seconds=_positive_int("THOUGHTS_AUTH_LOCKOUT_SECONDS", 900),
            auth_tracked_clients=_positive_int("THOUGHTS_AUTH_TRACKED_CLIENTS", 10_000),
            library_id=os.environ.get("THOUGHTS_DEFAULT_LIBRARY_ID", "default").strip() or "default",
            sandbox_builds=default_from_environment(os.environ.get("THOUGHTS_SANDBOX_BUILDS")),
            html_editions_enabled=os.environ.get("THOUGHTS_HTML_EDITIONS", "true").strip().lower()
            not in {"0", "false", "no"},
            html_edition_timeout_seconds=_positive_int("THOUGHTS_HTML_EDITION_TIMEOUT_SECONDS", 120),
            api_docs_enabled=os.environ.get("THOUGHTS_API_DOCS", "true").strip().lower()
            not in {"0", "false", "no"},
            library_storage_quota_bytes=_non_negative_int("THOUGHTS_LIBRARY_STORAGE_QUOTA_BYTES", 0),
            preview_parallel_jobs=_positive_int("THOUGHTS_PREVIEW_PARALLEL_JOBS", 2),
            max_queued_builds=_positive_int("THOUGHTS_MAX_QUEUED_BUILDS", 32),
            host_build_slots=_non_negative_int("THOUGHTS_HOST_BUILD_SLOTS", 0),
            host_build_slot_dir=_path("THOUGHTS_HOST_BUILD_SLOT_DIR", str(state_root / "build-slots")),
            database_pool_size=_positive_int("THOUGHTS_DATABASE_POOL_SIZE", 1),
            database_max_overflow=_non_negative_int("THOUGHTS_DATABASE_MAX_OVERFLOW", 14),
            worker_database_pool_size=_positive_int("THOUGHTS_WORKER_DATABASE_POOL_SIZE", 1),
            worker_database_max_overflow=_non_negative_int("THOUGHTS_WORKER_DATABASE_MAX_OVERFLOW", 4),
            worker_idle_poll_seconds=max(
                float(os.environ.get("THOUGHTS_WORKER_POLL_SECONDS", "1")),
                float(os.environ.get("THOUGHTS_WORKER_IDLE_POLL_SECONDS", "5")),
            ),
            workers_on_demand=os.environ.get("THOUGHTS_WORKERS_ON_DEMAND", "").strip().lower() in {"1", "true", "yes"},
            worker_idle_exit_seconds=float(os.environ.get("THOUGHTS_WORKER_IDLE_EXIT_SECONDS", "60")),
        )

    def worker_database(self):
        """This Library's database for one of its worker processes (a small pool)."""
        from .database import Database

        return Database(
            self.database_url,
            pool_size=self.worker_database_pool_size,
            max_overflow=self.worker_database_max_overflow,
        )

    def host_slots(self, kind: str = "builds"):
        """The host-wide limit for one kind of heavy job, shared by every Library's processes."""
        from .host_slots import HostSlots, default_slot_count

        directory = self.host_build_slot_dir or self.state_root / "build-slots"
        return HostSlots(directory / kind, self.host_build_slots or default_slot_count(self.build_parallel_jobs))

    def ensure_directories(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.object_root.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)


def proposal_decisions_strict_from_env() -> bool:
    """``THOUGHTS_PROPOSAL_DECISIONS_STRICT``: only claims and server resolutions decide proposals.

    Off by default (compatible mode): a vanished wrapper without a claim whose text is
    exactly an accept or reject is still retired as accepted or rejected.
    """
    value = os.environ.get("THOUGHTS_PROPOSAL_DECISIONS_STRICT", "false")
    return value.strip().lower() in {"1", "true", "yes"}


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def validate_bootstrap_tokens(tokens: dict[str, str]) -> None:
    """Refuse bootstrap tokens too short for a publicly reachable server.

    The error names the device but never includes the token.
    """
    for device_id, token in tokens.items():
        if len(token) < MINIMUM_TOKEN_LENGTH:
            raise ValueError(
                f"THOUGHTS_BOOTSTRAP_TOKENS: the token for device {device_id!r} must contain "
                f"at least {MINIMUM_TOKEN_LENGTH} characters"
            )


def normalize_public_base_url(value: str) -> str:
    """Return the public HTTPS origin used for links the server emits."""
    candidate = value.strip().rstrip("/")
    parts = urlsplit(candidate)
    try:
        parts.port  # noqa: B018 - validates the port
    except ValueError as error:
        raise ValueError("THOUGHTS_PUBLIC_BASE_URL has an invalid port") from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "THOUGHTS_PUBLIC_BASE_URL must be an https origin without credentials, path, query or fragment"
        )
    return candidate
