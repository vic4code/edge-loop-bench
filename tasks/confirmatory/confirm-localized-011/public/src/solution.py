def window_sums(values, size):
    if size <= 0:
        raise ValueError('size')
    return [sum(values[i:i + size]) for i in range(len(values) - size)]
