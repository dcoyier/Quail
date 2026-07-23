CREATE TABLE quail_workspaces(
  id TEXT PRIMARY KEY CHECK(length(id) > 0),
  created_at TEXT NOT NULL
);

CREATE TABLE quail_datasets(
  workspace_id TEXT NOT NULL REFERENCES quail_workspaces(id) ON DELETE RESTRICT,
  id TEXT NOT NULL CHECK(length(id) > 0),
  name TEXT,
  active_version_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id, id),
  FOREIGN KEY(workspace_id, id, active_version_id)
    REFERENCES quail_dataset_versions(workspace_id, dataset_id, id) ON DELETE RESTRICT
);

CREATE TABLE quail_dataset_versions(
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  id TEXT NOT NULL CHECK(length(id) > 0),
  content_hash TEXT NOT NULL,
  row_count INTEGER NOT NULL CHECK(row_count >= 0),
  field_count INTEGER NOT NULL CHECK(field_count >= 0),
  status TEXT NOT NULL CHECK(status IN ('importing', 'ready', 'failed')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id, dataset_id, id),
  FOREIGN KEY(workspace_id, dataset_id)
    REFERENCES quail_datasets(workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE quail_source_fields(
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK(length(name) > 0),
  position INTEGER NOT NULL CHECK(position >= 0),
  PRIMARY KEY(workspace_id, dataset_id, dataset_version_id, name),
  UNIQUE(workspace_id, dataset_id, dataset_version_id, position),
  FOREIGN KEY(workspace_id, dataset_id, dataset_version_id)
    REFERENCES quail_dataset_versions(workspace_id, dataset_id, id) ON DELETE RESTRICT
);

CREATE TABLE quail_entries(
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL,
  id TEXT NOT NULL CHECK(length(id) > 0),
  position INTEGER NOT NULL CHECK(position >= 0),
  PRIMARY KEY(workspace_id, dataset_id, dataset_version_id, id),
  UNIQUE(workspace_id, dataset_id, dataset_version_id, position),
  FOREIGN KEY(workspace_id, dataset_id, dataset_version_id)
    REFERENCES quail_dataset_versions(workspace_id, dataset_id, id) ON DELETE RESTRICT
);

CREATE TABLE quail_source_values(
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  field_name TEXT NOT NULL,
  value_json TEXT NOT NULL,
  value_hash TEXT NOT NULL,
  PRIMARY KEY(workspace_id, dataset_id, dataset_version_id, entry_id, field_name),
  FOREIGN KEY(workspace_id, dataset_id, dataset_version_id, entry_id)
    REFERENCES quail_entries(workspace_id, dataset_id, dataset_version_id, id) ON DELETE RESTRICT,
  FOREIGN KEY(workspace_id, dataset_id, dataset_version_id, field_name)
    REFERENCES quail_source_fields(workspace_id, dataset_id, dataset_version_id, name) ON DELETE RESTRICT
);

CREATE INDEX quail_entries_order
  ON quail_entries(workspace_id, dataset_id, dataset_version_id, position, id);

CREATE INDEX quail_source_values_field
  ON quail_source_values(workspace_id, dataset_id, dataset_version_id, field_name, entry_id);
