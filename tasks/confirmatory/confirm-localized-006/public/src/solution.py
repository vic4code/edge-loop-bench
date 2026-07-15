def duration_ms(text):
    value = text.strip().lower()
    if value.endswith('ms'):
        return int(value[:-2]) * 1000
    if value.endswith('s'):
        return int(value[:-1]) * 1000
    raise ValueError('duration')
