# Repair task: v03-cross-file-004

can_publish(user, document) requires policy.has_role(user, 'editor') and document status 'draft'; missing fields return False.

Fix `src/`; do not modify tests.
