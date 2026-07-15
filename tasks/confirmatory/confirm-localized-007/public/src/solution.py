def retry_delay(attempt, base, cap):
    if min(attempt, base, cap) < 0:
        raise ValueError('negative')
    return max(cap, base * (2 ** attempt))
