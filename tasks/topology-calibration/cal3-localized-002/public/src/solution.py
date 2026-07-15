def compact_code(text):
    value = text.strip().lower()
    if not value:
        raise ValueError('blank code')
    return value.replace(' ', '_')
