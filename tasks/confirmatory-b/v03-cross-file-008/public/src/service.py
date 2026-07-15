from repository import contains

def cache_read(cache, key):
    value = cache.get(key)
    return (bool(value), value)
