def parse_switch(value):
    if isinstance(value, bool):
        return value
    return value in {'yes', 'true', '1'}
