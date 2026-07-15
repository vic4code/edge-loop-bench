def valid_ids(rows):
    return [row['id'] for row in rows if row.get('active')]
