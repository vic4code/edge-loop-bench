from repository import available

def consume_quota(store, name, units):
    if units <= available(store, name):
        store[name] = available(store, name) - units
        return True
    return False
