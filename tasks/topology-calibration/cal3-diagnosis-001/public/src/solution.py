def accepted_total(rows):
    total = 0
    for row in rows:
        if row.get('status') == 'accepted':
            total += row.get('amount', 0)
    return total
