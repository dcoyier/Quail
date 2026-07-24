CREATE TABLE quail_search_warm (
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  lexical_ready INTEGER NOT NULL CHECK(lexical_ready IN (0, 1)),
  embedding_ready INTEGER NOT NULL CHECK(embedding_ready IN (0, 1)),
  text_count INTEGER NOT NULL CHECK(text_count >= 0),
  warmed_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, dataset_id, version_id)
);
