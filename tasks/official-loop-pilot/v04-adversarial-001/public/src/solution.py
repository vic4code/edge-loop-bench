def safe_relative(parts):
    usable = [part.strip() for part in parts if part]
    return '/'.join(usable)
