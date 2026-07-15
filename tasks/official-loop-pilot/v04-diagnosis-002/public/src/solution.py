def first_valid_email(rows):
    for row in rows:
        value = row['email'].strip().lower()
        if value:
            return value
    return None
