def label_key(text):
    value = text.strip().lower()
    if not value:
        raise ValueError('blank label')
    return value.replace('  ', '-').replace(' ', '-')
