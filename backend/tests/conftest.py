import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import DATA_DIR  # noqa: E402
from app.services.crm import CRMService  # noqa: E402


@pytest.fixture()
def crm() -> CRMService:
    return CRMService(DATA_DIR / "customers.json")


def get_customer_order(crm: CRMService, email: str, order_id: str):
    customer = crm.find_customer(email)
    assert customer is not None, f"seed data missing customer {email}"
    match = crm.get_order(order_id)
    assert match is not None, f"seed data missing order {order_id}"
    return customer, match[1]
