def canonical_words(text):
    return '-'.join(text.strip().casefold().split(' '))
