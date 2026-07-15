from policy import rebate_rate

def invoice_total(subtotal, member):
    return subtotal - rebate_rate(subtotal, member)
