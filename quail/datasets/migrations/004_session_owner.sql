-- Session ownership for Clerk: optional owner (TOML [[users]].id).
-- Unrestricted sessions keep owner_user_id NULL.

ALTER TABLE quail_sessions ADD COLUMN owner_user_id TEXT
  CHECK(owner_user_id IS NULL OR length(owner_user_id) > 0);

CREATE INDEX quail_sessions_owner
  ON quail_sessions(owner_user_id, status)
  WHERE owner_user_id IS NOT NULL;
