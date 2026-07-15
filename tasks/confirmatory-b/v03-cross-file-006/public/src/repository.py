def fetch(store, key):
    return {'found': key in store, 'value': store.get(key)}
