def normalize_key(text):
    value = text.strip().lower()
    if not value:
        raise ValueError('blank')
    return value.replace('  ', '_').replace(' ', '_')
