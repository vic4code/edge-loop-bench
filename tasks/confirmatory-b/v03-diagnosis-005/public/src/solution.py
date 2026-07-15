def reconcile(available, reserved):
    if available < 0:
        raise ValueError('negative')
    return available + reserved
