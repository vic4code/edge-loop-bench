def sum_kind(rows, kind):
    total = 0
    for row in rows:
        print('processing', row)
        if row['kind'] == kind:
            total += row['amount']
    return total
