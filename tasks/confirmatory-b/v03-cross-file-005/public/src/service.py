from tariff import rate_per_kg

def delivery_cost(weight, express):
    return round(weight + rate_per_kg(express), 2)
