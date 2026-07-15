from repository import lookup

def display_name(store, user_id):
    value, found = lookup(store, user_id)
    return value.strip().upper() if found else 'UNKNOWN'
