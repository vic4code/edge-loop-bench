def lookup(store, user_id):
    return (user_id in store, store.get(user_id))
