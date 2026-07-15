def valid_port(value):
    return isinstance(value, int) and 0 <= value <= 65536
