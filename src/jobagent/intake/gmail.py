"""Read-only Gmail access for LinkedIn job alert emails.

Scope is gmail.readonly and nothing else: the agent cannot send, delete, label or mark mail
as read. Which messages are done is remembered in the database (gmail_messages), not in Gmail.

One-time setup (see README): create a Google Cloud OAuth client of type "Desktop app", save
its JSON as data/private/gmail_client_secret.json, then run `jobagent gmail auth`.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailNotConfigured(RuntimeError):
    pass


@dataclass
class GmailMessage:
    id: str
    sender: str
    received_at: datetime
    subject: str
    text: str


def authorise(client_secret: Path, token: Path) -> None:
    """Interactive one-time consent in the browser; stores a refreshable read-only token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret.exists():
        raise GmailNotConfigured(f"missing {client_secret}; see README, 'Gmail setup'")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")


def build_service(token: Path) -> Any:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:  # optional dependency
        raise GmailNotConfigured("install the Gmail extra: pip install -e .[gmail]") from e
    if not token.exists():
        raise GmailNotConfigured(f"no Gmail token at {token}; run `jobagent gmail auth`")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if set(creds.scopes or []) - set(SCOPES):
        raise GmailNotConfigured(
            "token has scopes beyond gmail.readonly; delete it and re-run auth"
        )
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _plain_text(payload: dict) -> str:
    """Depth-first search for the text/plain part of a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        if text := _plain_text(part):
            return text
    return ""


def list_alert_messages(
    service: Any, senders: list[str], lookback_days: int, max_messages: int = 50
) -> Iterator[GmailMessage]:
    """Yield alert emails from the configured senders received in the lookback window."""
    query = f"from:({' OR '.join(senders)}) newer_than:{lookback_days}d"
    users = service.users()
    page_token = None
    yielded = 0
    while yielded < max_messages:
        resp = (
            users.messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=50)
            .execute()
        )
        for ref in resp.get("messages", []):
            msg = users.messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            yield GmailMessage(
                id=msg["id"],
                sender=headers.get("from", ""),
                received_at=datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC),
                subject=headers.get("subject", ""),
                text=_plain_text(msg["payload"]),
            )
            yielded += 1
            if yielded >= max_messages:
                return
        page_token = resp.get("nextPageToken")
        if not page_token:
            return
