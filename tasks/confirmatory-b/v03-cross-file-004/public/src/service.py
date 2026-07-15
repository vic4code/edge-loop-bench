from policy import has_role

def can_publish(user, document):
    return has_role(user, 'editor') or document['status'] == 'draft'
