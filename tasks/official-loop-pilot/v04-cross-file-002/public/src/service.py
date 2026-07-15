from repository import has_key

def record_lookup(records, key):
    value = records.get(key)
    return (bool(value), value)
