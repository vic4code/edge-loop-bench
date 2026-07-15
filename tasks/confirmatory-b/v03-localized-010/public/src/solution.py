def file_extension(name):
    return name.split('.', 1)[-1].lower() if '.' in name else ''
