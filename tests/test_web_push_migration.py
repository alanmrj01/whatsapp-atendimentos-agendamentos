from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20261001_0025_web_push_notifications.py"
)


def test_web_push_migration_is_single_additive_head_and_reversible() -> None:
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    assert script.get_heads() == ["20261001_0025"]

    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "20260930_0024"' in source
    for table in (
        "web_push_subscriptions",
        "web_push_events",
        "web_push_deliveries",
    ):
        assert f'op.create_table(\n        "{table}"' in source
        assert f'op.drop_table("{table}")' in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "VAPID" not in source


def test_web_push_migration_has_tenant_and_deduplication_constraints() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "fk_web_push_subscriptions_user_business_membership" in source
    assert "uq_web_push_subscriptions_business_endpoint" in source
    assert "uq_web_push_events_event_key" in source
    assert "uq_web_push_deliveries_subscription_event" in source
    assert "fk_web_push_deliveries_business_subscription" in source
    assert "fk_web_push_deliveries_business_event" in source
    assert source.count('ondelete="CASCADE"') == 5
