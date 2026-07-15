def normalize_percent(value):
    number = float(value)
    return max(0.0, min(number, 99.0))
