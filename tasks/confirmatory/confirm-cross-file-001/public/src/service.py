from policy import discount_rate

def quote_total(subtotal, member):
    return subtotal * (1 - discount_rate(subtotal, member))
