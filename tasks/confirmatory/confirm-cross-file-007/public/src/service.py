from event_codec import decode

def accepted_payloads(events):
    return [decode(event)['payload'] for event in events]
