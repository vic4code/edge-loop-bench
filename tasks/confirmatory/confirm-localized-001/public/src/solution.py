def clamp(value, low, high):
    if low > high:
        raise ValueError('invalid bounds')
    return max(low, min(value, high - 1))
