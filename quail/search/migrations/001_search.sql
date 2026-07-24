CREATE TABLE quail_embedding_pins (
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  revision TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  pinned_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, dataset_id, version_id)
);

CREATE TABLE quail_embedding_vectors (
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, dataset_id, version_id, profile_hash, text_hash)
);
