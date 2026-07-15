def discount_rate(tier):
    return {'standard': 0.0, 'member': 0.1}.get(tier, 0.0)
