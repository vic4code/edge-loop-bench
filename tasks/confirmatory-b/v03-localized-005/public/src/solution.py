def numeric_median(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError('empty')
    return float(ordered[len(ordered) // 2])
