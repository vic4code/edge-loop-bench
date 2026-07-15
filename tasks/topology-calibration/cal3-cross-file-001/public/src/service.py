from policy import shipping_rate

def shipping_quote(subtotal, priority):
    return subtotal + shipping_rate(subtotal, priority)
