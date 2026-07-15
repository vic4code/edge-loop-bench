def bounded_score(value, maximum):
    if maximum < 0:
        raise ValueError('negative maximum')
    return max(0, min(value, maximum - 1))
