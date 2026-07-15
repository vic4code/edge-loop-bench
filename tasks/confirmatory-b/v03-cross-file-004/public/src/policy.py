def has_role(user, role):
    return role in user.get('roles', [])
