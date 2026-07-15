def safe_average(values):
    usable = [value for value in values if value is not None]
    return sum(usable) / len(usable)
