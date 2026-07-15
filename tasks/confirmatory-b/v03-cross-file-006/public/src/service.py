from repository import fetch

def read_setting(store, key, default):
    result = fetch(store, key)
    return result['value'] or default
