from paging import total_pages

def page_meta(total, page, size):
    if page < 0 or size < 0:
        raise ValueError('paging')
    return {'page': page, 'size': size, 'total_pages': total_pages(total, size)}
