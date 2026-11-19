from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.features.company_mail import (
    MailboxKey, MailConnection, MailOwner, ProviderMessage, import_unread_mail,
    messages_needing_analysis, visible_conversations,
)
from app.features.mail_samples import mail_samples


NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
OWNER = MailOwner("test-company", "owner")


class SampleReader:
    def __init__(self, messages):
        self.messages = tuple(messages)
        self.calls = []

    def read_unread_inbox(self, mailbox, *, since, until, limit):
        self.calls.append((mailbox, since, until, limit))
        return self.messages


def connection(owner=OWNER, provider="google_workspace", **policy):
    return MailConnection(MailboxKey(owner, provider, "synthetic-account-id"), **policy)


def test_reimport_keeps_work_and_analysis_state_and_does_not_mark_read():
    account = connection(test_account=True, ai_approved=True)
    samples = [message for message in mail_samples(NOW.date()) if message["thread_id"] == "quote"]
    messages = tuple(ProviderMessage(
        message["id"], message["thread_id"], message["sender"], message["subject"],
        datetime.fromisoformat(message["received_at"]), "\n".join(message["sentences"]),
    ) for message in samples)
    reader = SampleReader(messages)
    first = import_unread_mail(reader, account, OWNER, {}, NOW)
    assert first.inserted == 2
    completed = replace(first.updates[0], work_status="done",
                        analyzed_fingerprint=first.updates[0].message.fingerprint)
    saved = {completed.key: completed, first.updates[1].key: first.updates[1]}
    again = import_unread_mail(reader, account, OWNER, saved, NOW)
    assert again.inserted == 0
    assert len(again.updates) == 2 and again.updates[0] == completed
    [thread] = visible_conversations(account, OWNER, again.updates)
    assert [record.key.provider_message_id for record in thread] == [message.message_id for message in messages]
    assert reader.calls == [(account.key, NOW - timedelta(days=30), NOW, 50)] * 2
    assert reader.messages == messages and all(message.unread for message in reader.messages)
    assert all(record.message.unread for record in thread)
    assert messages_needing_analysis(account, OWNER, again.updates) == [first.updates[1]]
    assert messages_needing_analysis(replace(account, ai_approved=False), OWNER, again.updates) == []
    assert messages_needing_analysis(replace(account, test_account=False), OWNER, again.updates) == []
    changed = replace(completed.message, plain_text="시연용으로 정정된 원문")
    updated = import_unread_mail(SampleReader([changed]), account, OWNER, saved, NOW)
    assert updated.inserted == 0 and updated.updates[0].work_status == "done"
    assert messages_needing_analysis(account, OWNER, updated.updates) == list(updated.updates)
    assert changed.plain_text not in repr(updated)


def test_window_latest_fifty_and_unread_inbox_only():
    account = connection()
    template = ProviderMessage("edge", None, "샘플 발신자", "제목", NOW - timedelta(days=30), "원문")
    filtered = SampleReader([
        template,
        replace(template, message_id="old", received_at=template.received_at - timedelta(seconds=1)),
        replace(template, message_id="future", received_at=NOW + timedelta(seconds=1)),
        replace(template, message_id="read", unread=False),
        replace(template, message_id="sent", inbox=False),
        replace(template, message_id="no-timezone", received_at=NOW.replace(tzinfo=None)),
    ])
    batch = import_unread_mail(filtered, account, OWNER, {}, NOW)
    assert [record.key.provider_message_id for record in batch.updates] == ["edge"]
    recent = [replace(template, message_id=str(index), received_at=NOW - timedelta(minutes=index))
              for index in range(54)]
    batch = import_unread_mail(SampleReader([recent[0], *reversed(recent)]), account, OWNER, {}, NOW)
    assert batch.inserted == 50
    assert [record.key.provider_message_id for record in batch.updates] == list(map(str, range(50)))


def test_other_users_and_organizations_cannot_read_or_import():
    main = connection(test_account=True, ai_approved=True)
    peer = MailOwner(OWNER.organization_id, "other-user")
    outside = MailOwner("another-organization", OWNER.user_id)
    original = ProviderMessage("same-provider-id", "same-thread", "샘플", "제목", NOW, "샘플 원문")
    reader = SampleReader([original])
    owners = (OWNER, peer, outside)
    records = [record for owner in owners
               for record in import_unread_mail(reader, connection(owner), owner, {}, NOW).updates]
    records.extend(import_unread_mail(reader, connection(provider="microsoft_365"), OWNER, {}, NOW).updates)
    assert len({record.key for record in records}) == 4
    assert visible_conversations(main, OWNER, records) == [[records[0]]]
    assert messages_needing_analysis(main, OWNER, records) == [records[0]]
    for actor in (peer, outside):
        with pytest.raises(PermissionError):
            import_unread_mail(reader, main, actor, {}, NOW)
        with pytest.raises(PermissionError):
            visible_conversations(main, actor, records)
        with pytest.raises(PermissionError):
            messages_needing_analysis(main, actor, records)
    assert len(reader.calls) == 4
