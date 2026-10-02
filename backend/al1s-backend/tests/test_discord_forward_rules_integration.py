"""Isolated PostgreSQL checks for Discord trigger rules and action routing.

Set AL1S_TEST_DATABASE_URL to a disposable database migrated through head.
"""

import os
import unittest
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.discord_forward_rules import DiscordForwardRuleRepository
from al1s.adapters.postgres.notification_models import (
    NotificationChannelRow,
    NotificationDeliveryRow,
)
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.adapters.postgres.review_models import MessageReviewRow
from al1s.notifications.discord_forward_rules import DiscordForwardRuleService
from al1s.notifications.discord_forward_types import ActionInput, RuleInput
from al1s.notifications.dispatcher import NotificationDispatcher
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.lexicon import LexiconData
from al1s.notifications.review_service import ReviewService
from al1s.notifications.types import AdapterReceipt, ChannelKind
from al1s.secrets.security import FernetSecretCipher


class DiscordForwardRuleIntegration(unittest.TestCase):
    def setUp(self):
        url = os.environ.get("AL1S_TEST_DATABASE_URL")
        if not url:
            self.skipTest("isolated PostgreSQL required")
        self.engine = create_engine(url)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.sessions = sessionmaker(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.source, self.qq_bot = uuid4(), uuid4()
        self.qq_channel, self.smtp_channel = uuid4(), uuid4()
        with self.sessions.begin() as session:
            session.add_all(
                [
                    BotServiceRow(id=self.source, name=str(self.source), kind="discord_bridge"),
                    BotServiceRow(id=self.qq_bot, name=str(self.qq_bot), kind="onebot_gateway"),
                    NotificationChannelRow(
                        id=self.qq_channel, name=str(self.qq_channel), kind="qq",
                        enabled=True, settings={}, bot_service_id=self.qq_bot,
                    ),
                    NotificationChannelRow(
                        id=self.smtp_channel, name=str(self.smtp_channel), kind="smtp",
                        enabled=True, settings={},
                    ),
                ]
            )
        self.rules = DiscordForwardRuleService(DiscordForwardRuleRepository(self.sessions))
        self.lexicon = MagicMock()
        self.lexicon.match.return_value = (
            LexiconData("a" * 40, "b" * 64, "MIT License", "{}", ("word",)),
            ("word",),
        )
        self.reviews = ReviewService(
            lambda: NotificationSqlAlchemyUnitOfWork(self.sessions),
            FernetSecretCipher("test-review-secret-" + "x" * 32),
            self.lexicon,
        )

    def tearDown(self):
        if hasattr(self, "transaction"):
            self.transaction.rollback()
            self.connection.close()
            self.engine.dispose()

    def _input(self, *, trigger_kind="contains", trigger_text="word"):
        return RuleInput(
            bot_service_id=self.source, name="message rule",
            guild_id="123456789012345678", channel_id="223456789012345678",
            trigger_kind=trigger_kind, trigger_text=trigger_text,
            frequency_count=None, frequency_window_seconds=None, cooldown_seconds=None,
            enabled=True,
            actions=(
                ActionInput(self.qq_channel, "qq_private", "123456", "none"),
                ActionInput(self.qq_channel, "qq_group", "234567", "lexicon"),
                ActionInput(self.smtp_channel, "smtp", "me@example.com", "none"),
            ),
        )

    def test_multiple_actions_review_and_replay(self):
        saved = self.rules.save(self._input())
        rule_id = UUID(saved["id"])
        assert len(self.rules.enabled_for_worker(self.source)) == 1
        assert len(self.rules.list_page(self.source)[0]["actions"]) == 3
        first = self.reviews.receive_rule_match(
            self.source, rule_id, 1, "123456789012345678", "223456789012345678",
            ["323456789012345678"], "word",
        )
        assert first["actions"] == 3 and not first["replayed"]
        again = self.reviews.receive_rule_match(
            self.source, rule_id, 1, "123456789012345678", "223456789012345678",
            ["323456789012345678"], "word",
        )
        assert again["replayed"]
        with self.sessions() as session:
            rows = session.scalars(
                select(MessageReviewRow).where(MessageReviewRow.service_id == self.source)
            ).all()
            assert sorted(row.state for row in rows) == ["approved", "approved", "pending"]
            pending = next(row for row in rows if row.state == "pending")
            deliveries = session.scalars(select(NotificationDeliveryRow)).all()
            assert sorted(row.channel_kind for row in deliveries) == ["qq", "smtp"]
            assert {row.targets[0] for row in deliveries} == {"private:123456", "me@example.com"}
        self.reviews.approve(pending.id, 1, "approved word")
        with self.sessions() as session:
            deliveries = session.scalars(select(NotificationDeliveryRow)).all()
            assert len(deliveries) == 3
            assert {row.targets[0] for row in deliveries} == {
                "private:123456", "group:234567", "me@example.com"
            }
        sent = []

        class Adapter:
            def send(self, message, *, idempotency_key):
                sent.append((message.targets[0], message.body))
                return AdapterReceipt(idempotency_key)

        dispatcher = NotificationDispatcher(
            uow_factory=lambda: NotificationSqlAlchemyUnitOfWork(self.sessions),
            adapters={ChannelKind.QQ: Adapter(), ChannelKind.SMTP: Adapter()},
            cipher=self.reviews._cipher,
            worker_id="discord-rule-test",
            review_body=self.reviews.sending_body,
        )
        assert dispatcher.dispatch_once().sent == 3
        assert sorted(sent) == sorted(
            [("private:123456", "word"), ("group:234567", "approved word"),
             ("me@example.com", "word")]
        )

    def test_stale_version_and_validation(self):
        saved = self.rules.save(self._input())
        rule_id = UUID(saved["id"])
        with self.assertRaises(NotificationDomainError):
            self.rules.save(self._input(), rule_id=rule_id, expected_version=2)
        invalid = self._input(trigger_kind="acrostic", trigger_text="a" * 65)
        with self.assertRaises(NotificationDomainError):
            self.rules.save(invalid)
        self.rules.delete(rule_id, 1)
        with self.assertRaises(NotificationDomainError):
            self.reviews.receive_rule_match(
                self.source, rule_id, 1, "123456789012345678", "223456789012345678",
                ["323456789012345678"], "word",
            )

    def test_clean_group_sends_and_lexicon_failure_holds_only_reviewed_action(self):
        saved = self.rules.save(self._input(trigger_text="clean"))
        rule_id = UUID(saved["id"])
        current_lexicon = self.lexicon.match.return_value[0]
        self.lexicon.match.return_value = (current_lexicon, ())
        self.reviews.receive_rule_match(
            self.source, rule_id, 1, "123456789012345678", "223456789012345678",
            ["323456789012345678"], "clean",
        )
        with self.sessions() as session:
            states = session.scalars(
                select(MessageReviewRow.state).where(MessageReviewRow.service_id == self.source)
            ).all()
            assert states == ["approved"] * 3
            assert len(session.scalars(select(NotificationDeliveryRow)).all()) == 3
        self.lexicon.match.side_effect = RuntimeError("lexicon unavailable")
        self.reviews.receive_rule_match(
            self.source, rule_id, 1, "123456789012345678", "223456789012345678",
            ["423456789012345678"], "clean again",
        )
        with self.sessions() as session:
            states = session.scalars(
                select(MessageReviewRow.state).where(MessageReviewRow.service_id == self.source)
            ).all()
            assert sorted(states) == ["approved"] * 5 + ["hold"]
            assert len(session.scalars(select(NotificationDeliveryRow)).all()) == 5


if __name__ == "__main__":
    unittest.main()
