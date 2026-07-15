def rebate_rate(subtotal, member):
    return 0.08 if member and subtotal >= 50 else 0.0
