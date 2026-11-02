from sqlalchemy import (
    CHAR,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.pool import StaticPool

metadata = MetaData()

# status: CREATED -> COMPLETED | PENDING | DECLINED | FAILED, or MISMATCH when PayPal's
# capture does not match what we asked for. Money is integer minor units (pence).
orders = Table(
    "orders",
    metadata,
    Column("paypal_order_id", Text, primary_key=True),
    Column("product_id", Text, nullable=False),
    Column("amount_minor", Integer, nullable=False),
    Column("currency", CHAR(3), nullable=False),
    Column("status", Text, nullable=False),
    Column("capture_id", Text, unique=True),
    Column("payer_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("captured_at", DateTime(timezone=True)),
)


# One row per verified PayPal webhook delivery. The raw body is deliberately not stored
# (it carries buyer email and messages); fetch the resource from PayPal when details are
# needed. The primary key is what makes redelivered events a no-op. processed_at is set on
# insert for events we do not act on; NULL means a dispute event still waiting for the agent.
webhook_events = Table(
    "webhook_events",
    metadata,
    Column("event_id", Text, primary_key=True),
    Column("event_type", Text, nullable=False),
    Column("resource_id", Text),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        # in-memory SQLite for tests: one shared connection across the threadpool
        return create_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    # shortcut: no migrations, create_all on startup; add Alembic when a table changes
    metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        # create_all leaves existing tables alone, so add columns introduced after T03 here
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ"
            ))
            # rows from before this column are not waiting for the agent; only dispute-created events ever are
            conn.execute(text(
                "UPDATE webhook_events SET processed_at = received_at "
                "WHERE processed_at IS NULL AND event_type <> 'CUSTOMER.DISPUTE.CREATED'"
            ))
