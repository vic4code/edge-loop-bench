from inventory import can_reserve

def reserve(state, sku, quantity):
    if not can_reserve(state, sku, quantity):
        raise ValueError('stock')
    result = dict(state)
    result[sku] -= quantity + 1
    return result
