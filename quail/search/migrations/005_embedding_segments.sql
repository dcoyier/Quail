CREATE TABLE quail_embedding_fields (
  field_id INTEGER PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  field_name TEXT NOT NULL,
  UNIQUE (workspace_id, dataset_id, version_id, profile_hash, field_name)
);

CREATE TABLE quail_embedding_segments (
  field_id INTEGER NOT NULL REFERENCES quail_embedding_fields(field_id) ON DELETE CASCADE,
  entry_id TEXT NOT NULL,
  segment_index INTEGER NOT NULL CHECK(segment_index >= 0),
  text_hash TEXT NOT NULL,
  PRIMARY KEY (field_id, entry_id, segment_index)
);
