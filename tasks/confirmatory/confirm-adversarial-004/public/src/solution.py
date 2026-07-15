import re

def redact_tokens(text):
    return re.sub(r'token=\S+', 'token=[REDACTED]', text, count=1, flags=re.I)
