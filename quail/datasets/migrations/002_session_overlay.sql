CREATE TABLE quail_sessions(
  id TEXT PRIMARY KEY CHECK(length(id) > 0),
  workspace_id TEXT NOT NULL REFERENCES quail_workspaces(id) ON DELETE RESTRICT,
  status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
  state_revision INTEGER NOT NULL DEFAULT 0 CHECK(state_revision >= 0),
  created_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL,
  UNIQUE(id, workspace_id)
);

CREATE INDEX quail_sessions_workspace
  ON quail_sessions(workspace_id, status, last_used_at);

CREATE TABLE quail_analysis_scopes(
  session_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL,
  field_registry_revision INTEGER NOT NULL DEFAULT 0 CHECK(field_registry_revision >= 0),
  value_revision INTEGER NOT NULL DEFAULT 0 CHECK(value_revision >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, workspace_id, dataset_id, dataset_version_id),
  FOREIGN KEY(session_id, workspace_id)
    REFERENCES quail_sessions(id, workspace_id) ON DELETE CASCADE,
  FOREIGN KEY(workspace_id, dataset_id, dataset_version_id)
    REFERENCES quail_dataset_versions(workspace_id, dataset_id, id) ON DELETE RESTRICT
);

CREATE TABLE quail_analysis_fields(
  session_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK(length(name) > 0),
  position INTEGER NOT NULL CHECK(position >= 0),
  value_revision INTEGER NOT NULL DEFAULT 0 CHECK(value_revision >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, workspace_id, dataset_id, dataset_version_id, name),
  UNIQUE(session_id, workspace_id, dataset_id, dataset_version_id, position),
  FOREIGN KEY(session_id, workspace_id, dataset_id, dataset_version_id)
    REFERENCES quail_analysis_scopes(session_id, workspace_id, dataset_id, dataset_version_id)
    ON DELETE CASCADE
);

CREATE TABLE quail_analysis_values(
  session_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  field_name TEXT NOT NULL,
  value_json TEXT NOT NULL,
  value_hash TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, workspace_id, dataset_id, dataset_version_id, entry_id, field_name),
  FOREIGN KEY(session_id, workspace_id, dataset_id, dataset_version_id, field_name)
    REFERENCES quail_analysis_fields(session_id, workspace_id, dataset_id, dataset_version_id, name)
    ON DELETE CASCADE,
  FOREIGN KEY(workspace_id, dataset_id, dataset_version_id, entry_id)
    REFERENCES quail_entries(workspace_id, dataset_id, dataset_version_id, id) ON DELETE RESTRICT
);

CREATE INDEX quail_analysis_values_field
  ON quail_analysis_values(
    session_id, workspace_id, dataset_id, dataset_version_id, field_name, entry_id
  );

CREATE TABLE quail_session_bindings(
  session_id TEXT NOT NULL REFERENCES quail_sessions(id) ON DELETE CASCADE,
  name TEXT NOT NULL CHECK(length(name) > 0),
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, name)
);
