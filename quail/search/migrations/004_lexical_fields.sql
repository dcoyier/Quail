-- Rebuild lexical corpus registry keyed by source field.
-- Prior version-wide corpora become orphaned tables.
-- Fingerprint bump forces re-warm.
DROP TABLE IF EXISTS quail_lexical_corpus;

CREATE TABLE quail_lexical_corpus (
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  field_name TEXT NOT NULL,
  doc_table TEXT NOT NULL,
  terms_table TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, dataset_id, version_id, field_name)
);
