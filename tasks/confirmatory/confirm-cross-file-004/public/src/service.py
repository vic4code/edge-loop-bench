from roles import normalized_roles

def is_admin(headers):
    return any('admin' in role for role in normalized_roles(headers.get('roles', '')))
