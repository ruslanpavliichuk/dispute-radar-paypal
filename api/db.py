from sqlalchemy import (
    CHAR,
    Column,
    DateTime,
    Engine,
    Float,
    Integer,
    JSON,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
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
    # failed tries by the agent; last_error is an exception type name or a skip reason, never a response body
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
)

# One row per thing an agent proposes (here: the response agent, one per dispute). status: PROPOSED ->
# REJECTED | SENDING -> SENT | FAILED. action uses PayPal's link rel names (underscores; the URL paths
# are hyphenated). features/defaulted/win_prob/reasons/ev/evidence are the numbers behind the proposal;
# draft is built from our own order data only. Buyer text is never stored (same rule as webhook_events).
proposals = Table(
    "proposals",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent", Text, nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("source_event_id", Text),
    Column("status", Text, nullable=False, server_default="PROPOSED"),
    Column("action", Text, nullable=False),  # provide_evidence | send_message | make_offer | accept_claim | upload_tracking | goodwill_refund
    Column("amount_minor", Integer),
    Column("currency", CHAR(3)),
    Column("claim_type", Text),
    Column("claim_source", Text),  # reason | classifier
    Column("features", JSON),
    Column("defaulted", JSON),  # feature names we had to default
    Column("win_prob", Float),
    Column("reasons", JSON),
    Column("ev", JSON),
    Column("evidence", JSON),
    Column("draft", Text),
    Column("draft_source", Text),  # llm | template
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
    Column("sent_at", DateTime(timezone=True)),
    Column("error", Text),
    Column("opened_at", DateTime(timezone=True)),  # response agent: PayPal create_time of the dispute
    Column("due_at", DateTime(timezone=True)),  # response agent: seller_response_due_date
    UniqueConstraint("agent", "subject_id"),
)

# Shipment progress for an order, written by scripts/simulate_tracking.py (no public endpoint). The prevention
# agent re-scores the order on each row. kind: accepted | in_transit | stuck_at_depot | out_for_delivery | delivered.
# processed_at / attempts / last_error work as on webhook_events.
tracking_events = Table(
    "tracking_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paypal_order_id", Text, nullable=False, index=True),
    Column("kind", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("tracking_number", Text),
    Column("carrier", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
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
            conn.execute(text(
                "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
            ))
            conn.execute(text("ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS last_error TEXT"))
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ"))
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS due_at TIMESTAMPTZ"))
            # rows from before this column are not waiting for the agent; only dispute-created events ever are
            conn.execute(text(
                "UPDATE webhook_events SET processed_at = received_at "
                "WHERE processed_at IS NULL AND event_type <> 'CUSTOMER.DISPUTE.CREATED'"
            ))
