from pricing import final_price

def test_twenty_percent():
    assert final_price(100, 20) == 80

def test_zero():
    assert final_price(50, 0) == 50

def test_full():
    assert final_price(75, 100) == 0
