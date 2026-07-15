def parse_bool(text):
    value = text.strip().lower()
    if value in {'true', 'yes'}:
        return True
    if value in {'false', 'no'}:
        return False
    raise ValueError('boolean')
