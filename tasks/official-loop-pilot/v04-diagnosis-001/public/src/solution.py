def active_total(rows):
    return sum(row['amount'] for row in rows if row['active'])
