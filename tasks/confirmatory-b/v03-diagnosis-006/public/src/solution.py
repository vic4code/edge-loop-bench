def group_values(rows):
    result = {}
    for row in rows:
        result.setdefault(row['group'], []).append(row['value'])
    return result
