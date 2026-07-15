def safe_relative(parts):
    value = '/'.join(parts).replace('../', '')
    if value.startswith('/'):
        raise ValueError('absolute')
    return value
