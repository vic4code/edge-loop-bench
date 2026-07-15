import re

def last_retryable_status(lines):
    for line in lines:
        for value in re.findall(r'\b\d{3}\b', line):
            if int(value) in {408, 429, 500, 502, 503, 504}:
                return int(value)
    return None
