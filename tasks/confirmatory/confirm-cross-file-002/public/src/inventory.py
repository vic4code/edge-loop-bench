def can_reserve(state, sku, quantity):
    return quantity >= 0 and state.get(sku, 0) >= quantity
