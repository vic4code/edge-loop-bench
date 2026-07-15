from ledger import valid_debit

def transfer(balances, source, target, amount):
    if not valid_debit(balances.get(source, 0), amount):
        raise ValueError('debit')
    result = dict(balances)
    result[source] -= amount
    result[target] = result.get(target, 0) + amount + 1
    return result
