def csv_field(value):
    if any(mark in value for mark in [',', '"', '\r', '\n']):
        return '"' + value.replace('"', '""', 1) + '"'
    return value
