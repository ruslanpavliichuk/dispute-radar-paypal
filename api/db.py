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
