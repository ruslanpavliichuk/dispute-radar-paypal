from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from paypalserversdk.exceptions.api_exception import ApiException
from paypalserversdk.models.order import Order
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from api.config import settings
from api.db import orders
from api.main import app
from api.shop import CATALOGUE, money

PRODUCT = "wool-beanie"
# Fixed test catalogue: api/catalogue.json is a refreshable Channel3 snapshot
TEST_CATALOGUE = {
    PRODUCT: {
        "id": PRODUCT, "title": "Merino wool beanie", "brand": "Northfield",
        "image_url": None, "price_minor": 1800, "currency": "GBP",
    }
}
REAL_CATALOGUE = CATALOGUE


def paypal_order(
    status="COMPLETED", value="18.00", currency="GBP", capture_id="CAP1",
    payer=True, payments=True, amount=True, account_id=None,
):
    """A PayPal order built with the real SDK model, so absent fields are absent attributes."""
    capture = {"id": capture_id, "status": status}
    if amount:
        capture["amount"] = {"currency_code": currency, "value": value}
    unit = {"reference_id": PRODUCT}
    if payments:
        unit["payments"] = {"captures": [capture]}
    data = {"id": "ORDER1", "purchase_units": [unit]}
    if payer:
        data["payer"] = {"payer_id": "PAYER1"}
    if account_id:
        data["payment_source"] = {"paypal": {"account_id": account_id}}
    return Order.from_dictionary(data)


def api_error(code):
    return ApiException("boom", SimpleNamespace(status_code=code))


class FakeOrders:
    """Stands in for the Server SDK's OrdersController (it bypasses httpx, so respx can't mock it)."""

    def __init__(self):
        self.created, self.captured, self.fetched = [], [], []
        self.capture_result = paypal_order()
        self.capture_error = None
        self.create_error = None
        self.get_result = paypal_order()
        self.on_capture = None
        self._n = 0

    def create_order(self, options):
        if self.create_error:
            raise self.create_error
        self.created.append(options)
        self._n += 1
        return SimpleNamespace(body=SimpleNamespace(id=f"ORDER{self._n}"))

    def capture_order(self, options):
        self.captured.append(options)
        if self.on_capture:
            self.on_capture()
        if self.capture_error:
            raise self.capture_error
        return SimpleNamespace(body=self.capture_result)

    def get_order(self, options):
        self.fetched.append(options)
        return SimpleNamespace(body=self.get_result)


class WriteFailingEngine:
    """Reads work, writes fail: the database goes away after PayPal has answered."""

    def __init__(self, real):
        self.real = real

    def connect(self):
        return self.real.connect()

    def begin(self):
        raise OperationalError("UPDATE orders", {}, Exception("db down"))

    def dispose(self):
        self.real.dispose()


@pytest.fixture
def shop(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr("api.shop.CATALOGUE", TEST_CATALOGUE)
    fake = FakeOrders()
    with TestClient(app) as client:
        app.state.orders_api = fake
        client.fake = fake
        client.row = _row
        yield client


def _row(order_id):
    with app.state.engine.connect() as conn:
        return conn.execute(
            select(orders).where(orders.c.paypal_order_id == order_id)
        ).mappings().first()


def place(shop, product=PRODUCT):
    return shop.post("/api/shop/orders", json={"product_id": product}).json()["order_id"]


def capture(shop, order_id):
    return shop.post(f"/api/shop/orders/{order_id}/capture")


@pytest.mark.parametrize("minor,expected", [(0, "0.00"), (5, "0.05"), (1299, "12.99"), (100000, "1000.00")])
def test_money_formats_pence_without_floats(minor, expected):
    assert money(minor) == expected


def test_committed_catalogue_is_valid():
    products = list(REAL_CATALOGUE.values())
    assert 6 <= len(products) <= 10
    assert len(REAL_CATALOGUE) == len(products)  # ids are unique
    for p in products:
        assert p["id"] and p["title"] and p["currency"] == "GBP"
        assert 0 < p["price_minor"] <= 20000, p["title"]  # a demo item, not a £1,849 table


def test_products_are_listed_with_decimal_prices(shop):
    products = shop.get("/api/shop/products").json()
    beanie = next(p for p in products if p["id"] == PRODUCT)
    assert beanie["price"] == "18.00" and beanie["currency"] == "GBP"


# --- create ---


def test_create_order_uses_catalogue_price_and_stores_a_row(shop):
    order_id = place(shop)

    sent = shop.fake.created[0]
    unit = sent["body"].purchase_units[0]
    assert unit.amount.value == "18.00" and unit.amount.currency_code == "GBP"
    assert sent["paypal_request_id"]
    row = shop.row(order_id)
    assert (row["status"], row["amount_minor"], row["capture_id"]) == ("CREATED", 1800, None)


def test_create_order_rejects_unknown_product(shop):
    assert shop.post("/api/shop/orders", json={"product_id": "nope"}).status_code == 404
    assert not shop.fake.created


def test_create_order_ignores_client_supplied_amount(shop):
    response = shop.post("/api/shop/orders", json={"product_id": PRODUCT, "amount": "0.01"})
    assert response.status_code == 422
    assert not shop.fake.created


@pytest.mark.parametrize("error", [api_error(500), TimeoutError("timed out")], ids=["api", "timeout"])
def test_create_order_paypal_failure_is_502_and_stores_nothing(shop, error):
    shop.fake.create_error = error
    assert shop.post("/api/shop/orders", json={"product_id": PRODUCT}).status_code == 502
    with app.state.engine.connect() as conn:
        assert conn.execute(select(orders)).first() is None


def test_create_order_db_failure_is_500(shop):
    app.state.engine = WriteFailingEngine(app.state.engine)
    assert shop.post("/api/shop/orders", json={"product_id": PRODUCT}).status_code == 500


# --- capture ---


def test_capture_stores_capture_id_and_is_idempotent(shop):
    order_id = place(shop)

    first = capture(shop, order_id)
    second = capture(shop, order_id)

    assert first.status_code == 200
    assert first.json() == {"status": "COMPLETED", "capture_id": "CAP1"}
    assert second.json() == first.json()
    assert len(shop.fake.captured) == 1
    assert shop.fake.captured[0]["paypal_request_id"] == f"capture-{order_id}"
    row = shop.row(order_id)
    assert (row["status"], row["capture_id"], row["payer_id"]) == ("COMPLETED", "CAP1", "PAYER1")
    assert row["captured_at"] is not None


@pytest.mark.parametrize(
    "kwargs",
    [{"value": "0.01"}, {"currency": "USD"}, {"amount": False}],
    ids=["amount", "currency", "amount-missing"],
)
def test_capture_mismatch_is_flagged_and_terminal(shop, kwargs):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(**kwargs)

    assert capture(shop, order_id).status_code == 502
    assert shop.row(order_id)["status"] == "MISMATCH"
    assert capture(shop, order_id).status_code == 409
    assert len(shop.fake.captured) == 1


def test_capture_response_without_a_capture_is_502_and_not_completed(shop):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(payments=False)

    assert capture(shop, order_id).status_code == 502
    assert shop.row(order_id)["status"] == "CREATED"


def test_capture_without_payer_still_completes(shop):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(payer=False)

    assert capture(shop, order_id).status_code == 200
    assert shop.row(order_id)["payer_id"] is None


def test_capture_takes_payer_from_payment_source(shop):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(payer=False, account_id="ACC1")

    assert capture(shop, order_id).status_code == 200
    assert shop.row(order_id)["payer_id"] == "ACC1"


def test_capture_unexpected_status_is_recorded_unknown(shop):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(status="WEIRD")

    assert capture(shop, order_id).status_code == 502
    assert shop.row(order_id)["status"] == "UNKNOWN"


@pytest.mark.parametrize("error", [api_error(500), TimeoutError("timed out")], ids=["api", "timeout"])
def test_capture_paypal_error_leaves_order_retryable(shop, error):
    order_id = place(shop)
    shop.fake.capture_error = error

    assert capture(shop, order_id).status_code == 502
    row = shop.row(order_id)
    assert (row["status"], row["capture_id"]) == ("CREATED", None)

    shop.fake.capture_error = None
    assert capture(shop, order_id).status_code == 200


def test_capture_422_reads_the_order_when_already_captured(shop):
    order_id = place(shop)
    shop.fake.capture_error = api_error(422)

    response = capture(shop, order_id)

    assert response.status_code == 200
    assert shop.fake.fetched == [{"id": order_id}]
    assert shop.row(order_id)["status"] == "COMPLETED"


def test_capture_422_without_a_capture_is_402_and_retryable(shop):
    order_id = place(shop)
    shop.fake.capture_error = api_error(422)
    shop.fake.get_result = paypal_order(payments=False)

    assert capture(shop, order_id).status_code == 402
    assert shop.row(order_id)["status"] == "CREATED"


def test_capture_pending_is_stored_as_pending(shop):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(status="PENDING")

    response = capture(shop, order_id)

    assert response.json()["status"] == "PENDING"
    assert shop.row(order_id)["status"] == "PENDING"


def test_capture_declined_is_402_then_terminal(shop):
    order_id = place(shop)
    shop.fake.capture_result = paypal_order(status="DECLINED")

    assert capture(shop, order_id).status_code == 402
    assert shop.row(order_id)["status"] == "DECLINED"
    assert capture(shop, order_id).status_code == 409
    assert len(shop.fake.captured) == 1


def test_capture_db_write_failure_is_500_and_row_stays_created(shop):
    order_id = place(shop)
    real = app.state.engine
    app.state.engine = WriteFailingEngine(real)

    assert capture(shop, order_id).status_code == 500
    app.state.engine = real
    assert shop.row(order_id)["status"] == "CREATED"


def test_capture_loses_race_and_reports_the_winners_result(shop):
    order_id = place(shop)

    def other_request_wins():
        with app.state.engine.begin() as conn:
            conn.execute(
                update(orders)
                .where(orders.c.paypal_order_id == order_id)
                .values(status="COMPLETED", capture_id="WINNER")
            )

    shop.fake.on_capture = other_request_wins

    response = capture(shop, order_id)

    assert response.json() == {"status": "COMPLETED", "capture_id": "WINNER"}
    assert shop.row(order_id)["capture_id"] == "WINNER"


def test_capture_unknown_or_malformed_order(shop):
    assert capture(shop, "NOSUCHORDER").status_code == 404
    assert shop.post("/api/shop/orders/lower-case/capture").status_code == 422
    assert not shop.fake.captured


def test_startup_refuses_a_non_sandbox_paypal_base(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr(settings, "paypal_api_base", "https://api-m.paypal.com")
    with pytest.raises(RuntimeError, match="sandbox"):
        with TestClient(app):
            pass
