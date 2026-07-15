def latest_by_key(rows, key):
    matches = [row for row in rows if row['key'] == key]
    return matches[-1] if matches else None
