# Poison fixture for guard 7 — direct artifact insert (must fail lint).
INSERT INTO artifacts (artifact_id, content_hash, storage_uri, artifact_kind, provenance)
VALUES ('art_poison', 'sha256:dead', 'file:///tmp/x', 'page', '{}'::jsonb);
