def parse_lines(lines):
    return dict(line.split('=', 1) for line in lines if '=' in line)
