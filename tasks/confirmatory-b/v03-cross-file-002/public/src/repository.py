def counts(state, sku):
    return state.setdefault(sku, {'available': 0, 'reserved': 0})
