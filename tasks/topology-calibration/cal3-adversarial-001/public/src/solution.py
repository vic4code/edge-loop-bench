def canonical_path(parts):
    usable = [part.strip() for part in parts if part]
    if not usable:
        raise ValueError('empty path')
    return '/'.join(usable)
