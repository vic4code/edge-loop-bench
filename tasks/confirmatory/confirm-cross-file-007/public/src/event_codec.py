def decode(value):
    version, kind, payload = value.split('|', 2)
    return {'version': version, 'kind': kind, 'payload': payload}
