def extract_timestamp(line):
    return line.split('at=', 1)[1] if 'at=' in line else None
