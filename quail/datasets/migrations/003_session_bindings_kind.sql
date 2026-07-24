ALTER TABLE quail_session_bindings
  ADD COLUMN value_kind TEXT NOT NULL DEFAULT 'literal';
