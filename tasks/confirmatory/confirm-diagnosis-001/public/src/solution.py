import re

def first_error_code(lines):
    found = re.findall(r'ERROR \[([^]]+)\]', '\n'.join(lines))
    return found[-1] if found else None
