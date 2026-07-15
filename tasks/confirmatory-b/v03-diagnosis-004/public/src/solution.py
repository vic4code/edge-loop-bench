def status_counts(rows):
    counts = {}
    for row in rows:
        status = row['status'].lower()
        counts[status] = counts.get(status, 0) + 1
    return counts
