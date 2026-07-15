from config_parse import parse_lines

def get_setting(lines, key, default=None):
    return parse_lines(lines).get(key)
