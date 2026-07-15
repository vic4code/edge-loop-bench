def classify_timeout(message):
    return 'timeout' if 'timeout' in message else 'other'
