def rate_for(region):
    return {'north': 0.05, 'south': 0.08}.get(region, 0.0)
