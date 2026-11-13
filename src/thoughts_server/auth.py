# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Device

# 32 random bytes: 256 bits, encoded as 43 URL-safe characters.
TOKEN_RANDOM_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_RANDOM_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, or None when there is none."""
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip() or None


def internal_token_hash() -> str:
    """A token_hash for an internal device, which writes to the database and never authenticates.

    It is the hash of random bytes nobody keeps, so no bearer token matches it.
    """
    return hashlib.sha256(secrets.token_bytes(TOKEN_RANDOM_BYTES)).hexdigest()


# Earlier servers derived internal devices' token hashes from strings in this code,
# which made those strings valid bearer tokens.
_GUESSABLE_INTERNAL_TOKENS = {
    "thoughts-review-worker": "thoughts-internal-review-worker",
    "thoughts-anchor-worker": "thoughts-internal-anchor-worker",
    "thoughts-agent-proposals": "thoughts-agent-proposals",
    "thoughts-codex-repair": "thoughts-codex-repair",
    # str(uuid.uuid5(logical_comments.SYSTEM_NAMESPACE, "thoughts-logical-comments"))
    "thoughts-logical-comments": str(
        uuid.uuid5(uuid.UUID("245ba1a0-fb06-4c85-bc73-c415d21d3ce6"), "thoughts-logical-comments")
    ),
}


def retire_guessable_internal_tokens(session: Session) -> list[str]:
    """Give internal devices that still have a guessable token hash an unusable one."""
    retired = []
    for device_id, token in _GUESSABLE_INTERNAL_TOKENS.items():
        device = session.get(Device, device_id)
        if device is not None and device.token_hash == token_hash(token):
            device.token_hash = internal_token_hash()
            retired.append(device_id)
    if retired:
        session.commit()
    return retired


def create_device(session: Session, device_id: str, name: str, token: str | None = None) -> str:
    if not device_id or len(device_id) > 128:
        raise ValueError("device ID must contain 1 to 128 characters")
    if session.get(Device, device_id) is not None:
        raise ValueError(f"device already exists: {device_id}")
    plain_token = token or generate_token()
    session.add(Device(id=device_id, name=name, token_hash=token_hash(plain_token), enabled=True))
    session.commit()
    return plain_token


def authenticate(session: Session, token: str) -> Device | None:
    digest = token_hash(token)
    device = session.scalar(select(Device).where(Device.token_hash == digest, Device.enabled.is_(True)))
    if device is None or not secrets.compare_digest(device.token_hash, digest):
        return None
    return device


def install_bootstrap_devices(session: Session, tokens: dict[str, str]) -> None:
    changed = False
    for device_id, token in tokens.items():
        if session.get(Device, device_id) is None:
            session.add(
                Device(
                    id=device_id,
                    name=f"Bootstrap device {device_id}",
                    token_hash=token_hash(token),
                    enabled=True,
                )
            )
            changed = True
    if changed:
        session.commit()
