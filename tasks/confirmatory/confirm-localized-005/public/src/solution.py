import math

def nearest_rank(values, percentile):
    ordered = sorted(values)
    if not ordered or not 0 < percentile <= 100:
        raise ValueError('input')
    return ordered[max(0, math.floor(percentile / 100 * len(ordered)) - 1)]
