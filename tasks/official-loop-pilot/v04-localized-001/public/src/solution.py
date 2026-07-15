def page_count(total_items, page_size):
    if page_size <= 0:
        raise ValueError('invalid size')
    return total_items // page_size
