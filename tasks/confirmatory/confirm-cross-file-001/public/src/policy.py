def discount_rate(subtotal, member):
    return 0.1 if member and subtotal > 100 else 0.0
