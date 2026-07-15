from repository import counts

def reserve_units(state, sku, units):
    record = counts(state, sku)
    record['available'] -= units
    record['reserved'] += units
    return record['available'] >= 0
