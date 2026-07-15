def relative_key(path):
    parts = path.replace('\\', '/').split('/')
    return '/'.join(part for part in parts if part not in {'', '.'})
