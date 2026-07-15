def root_cause(lines):
    for line in lines:
        if line.strip():
            return line.strip()
    return None
