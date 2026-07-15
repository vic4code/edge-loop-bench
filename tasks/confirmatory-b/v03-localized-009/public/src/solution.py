def chunk_at(values, index, size):
    if size <= 0 or index < 0:
        raise ValueError('invalid chunk')
    start = index * size
    return values[start:start + size - 1]
