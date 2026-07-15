from policy import discount_rate

def final_price(subtotal, tier):
    return round(subtotal - discount_rate(tier), 2)
