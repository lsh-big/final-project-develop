from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Literal, Protocol


IMPORT_DAYS = 30
IMPORT_LIMIT = 50


@dataclass(frozen=True)
class MailOwner:
    organization_id: str
    user_id: str


@dataclass(frozen=True)
class MailboxKey:
    owner: MailOwner
    provider: Literal["demo", "google_workspace", "microsoft_365"]
    provider_account_id: str


@dataclass(frozen=True)
class MailConnection:
    key: MailboxKey
    test_account: bool = False
    ai_approved: bool = False


@dataclass(frozen=True)
class ProviderMessage:
    message_id: str
    conversation_id: str | None
    sender: str
    subject: str
    received_at: datetime
    plain_text: str = field(repr=False)
    inbox: bool = True
    unread: bool = True

    @property
    def fingerprint(self):
        content = json.dumps((self.subject, self.plain_text), ensure_ascii=False)
        return sha256(content.encode("utf-8")).hexdigest()


class UnreadInboxReader(Protocol):
    def read_unread_inbox(self, mailbox: MailboxKey, *, since: datetime | None, until: datetime,
                          limit: int | None) -> Iterable[ProviderMessage]:
        """Return plain-text messages using provider read endpoints without changing remote state."""
        ...


@dataclass(frozen=True)
class MailKey:
    mailbox: MailboxKey
    provider_message_id: str


@dataclass(frozen=True)
class ImportedMail:
    key: MailKey
    message: ProviderMessage
    work_status: Literal["pending", "in_progress", "done"] = "pending"
    analyzed_fingerprint: str | None = None


@dataclass(frozen=True)
class ImportBatch:
    updates: tuple[ImportedMail, ...]
    inserted: int


def require_mail_owner(connection: MailConnection, actor: MailOwner):
    # The eventual route must obtain actor from verified server authentication.
    if not actor.organization_id or not actor.user_id or not connection.key.provider_account_id or connection.key.owner != actor:
        raise PermissionError("회사 메일을 조회할 권한이 없습니다.")


def import_unread_mail(reader: UnreadInboxReader, connection: MailConnection, actor: MailOwner,
                       existing: Mapping[MailKey, ImportedMail], now: datetime, *,
                       days: int | None = IMPORT_DAYS, limit: int | None = IMPORT_LIMIT) -> ImportBatch:
    """Prepare account-scoped upserts. Persistence must enforce uniqueness on MailKey."""
    require_mail_owner(connection, actor)
    if now.utcoffset() is None:
        raise ValueError("Import time must include a timezone")
    since = now - timedelta(days=days) if days is not None else None
    fetched = reader.read_unread_inbox(connection.key, since=since, until=now, limit=limit)
    messages = sorted(
        (message for message in fetched if message.inbox and message.unread and message.message_id
         and message.received_at.utcoffset() is not None and message.received_at <= now
         and (since is None or since <= message.received_at)),
        key=lambda message: (message.received_at, message.message_id),
        reverse=True,
    )
    updates = {}
    inserted = 0
    for message in messages:
        key = MailKey(connection.key, message.message_id)
        if key in updates:
            continue
        previous = existing.get(key)
        updates[key] = replace(previous, message=message) if previous else ImportedMail(key, message)
        inserted += int(previous is None)
        if len(updates) == limit:
            break
    return ImportBatch(tuple(updates.values()), inserted)


def visible_conversations(connection: MailConnection, actor: MailOwner, records: Iterable[ImportedMail]):
    require_mail_owner(connection, actor)
    groups = {}
    for record in records:
        if record.key.mailbox != connection.key:
            continue
        message = record.message
        group = ("conversation", message.conversation_id) if message.conversation_id else ("message", message.message_id)
        groups.setdefault(group, []).append(record)
    for group in groups.values():
        group.sort(key=lambda record: (record.message.received_at, record.message.message_id))
    return sorted(groups.values(), key=lambda group: group[-1].message.received_at, reverse=True)


def messages_needing_analysis(connection: MailConnection, actor: MailOwner, records: Iterable[ImportedMail]):
    require_mail_owner(connection, actor)
    if not (connection.test_account and connection.ai_approved):
        return []
    return [record for record in records if record.key.mailbox == connection.key
            and record.analyzed_fingerprint != record.message.fingerprint]
