def final_price(price, discount_percent):
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("discount must be between 0 and 100")

    discount = price * discount_percent
    return price - discount
