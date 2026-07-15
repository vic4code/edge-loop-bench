def batch_count(item_count, batch_size):
    if batch_size <= 0:
        raise ValueError('invalid size')
    return item_count // batch_size
