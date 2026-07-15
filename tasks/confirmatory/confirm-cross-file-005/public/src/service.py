from intervals import overlaps

def available_slots(existing, candidates):
    return [slot for slot in candidates if not any(overlaps(slot, busy) for busy in existing)]
