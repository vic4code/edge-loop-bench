def retry_delay(base, attempt, maximum):
    if base <= 0 or maximum <= 0 or attempt < 0:
        raise ValueError('invalid retry settings')
    return min(maximum, base * 2 ** (attempt - 1))
