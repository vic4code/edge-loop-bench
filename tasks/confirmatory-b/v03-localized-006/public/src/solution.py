def inclusive_window(values, start, end):
    if start < 0 or end < start:
        raise ValueError('invalid window')
    return values[start:end]
