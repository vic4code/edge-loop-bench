def stable_unique(values):
    return list(dict.fromkeys(reversed(values)))[::-1]
