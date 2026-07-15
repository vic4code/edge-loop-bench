def chunks(items, size):
    if size <= 0:
        raise ValueError('size')
    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]
