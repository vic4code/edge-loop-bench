def redact_secret(text, secret):
    return text.replace(secret.strip(), '[REDACTED]')
