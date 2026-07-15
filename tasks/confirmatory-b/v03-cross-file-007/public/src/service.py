from tax import rate_for

def taxed_total(subtotal, region):
    return subtotal + rate_for(region)
