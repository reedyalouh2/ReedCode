import pytest

from pricing import final_price


@pytest.mark.parametrize("price,discount,expected", [
    (100, 20, 80),
    (50, 0, 50),
    (75, 100, 0),
    (80, 12.5, 70),
    (0, 50, 0),
    (19.99, 15, 16.9915),
])
def test_discounts(price, discount, expected):
    assert final_price(price, discount) == pytest.approx(expected)


@pytest.mark.parametrize("discount", [-1, 101])
def test_invalid_discount(discount):
    with pytest.raises(ValueError, match="discount"):
        final_price(100, discount)
