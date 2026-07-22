-- N2: Gate 0–1 foundation — jobs, tasks, artifacts, lineage, workflows, outbox
-- Sources: control_plane_v3/v4, doc 07 §5, doc 09 guard 3

CREATE TABLE research_jobs (
    job_id TEXT PRIMARY KEY,
    parent_job_id TEXT REFERENCES research_jobs (job_id),
    job_kind TEXT NOT NULL CHECK (
        job_kind IN ('dossier', 'collection', 'scoring', 'validation', 'decision')
    ),
    niche_id TEXT,
    constraints_ref TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'CREATED',
            'PLANNING',
            'DISCOVERING',
            'ACQUIRING',
            'EXTRACTING',
            'INDEXING',
            'SYNTHESIZING',
            'COMPLETED',
            'PAUSED',
            'CANCEL_REQUESTED',
            'CANCELLED',
            'FAILED',
            'COMPLETED_WITH_GAPS'
        )
    ),
    config_hash TEXT NOT NULL,
    budget JSONB NOT NULL,
    as_of TIMESTAMPTZ,
    ttl_seconds INTEGER CHECK (ttl_seconds IS NULL OR ttl_seconds >= 0),
    deadline_at TIMESTAMPTZ,
    provenance JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_research_jobs_parent
    ON research_jobs (parent_job_id)
    WHERE parent_job_id IS NOT NULL;

CREATE INDEX idx_research_jobs_status ON research_jobs (status);

CREATE INDEX idx_research_jobs_niche
    ON research_jobs (niche_id)
    WHERE niche_id IS NOT NULL;

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_jobs (job_id) ON DELETE CASCADE,
    task_kind TEXT,
    lane TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state TEXT NOT NULL CHECK (
        state IN (
            'PENDING',
            'READY',
            'LEASED',
            'RUNNING',
            'SUCCEEDED',
            'RETRY_WAIT',
            'BLOCKED',
            'FAILED',
            'DEAD_LETTER'
        )
    ),
    idempotency_key TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    traceparent TEXT,
    not_before TIMESTAMPTZ,
    retry_at TIMESTAMPTZ,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    result_artifact_id TEXT,
    completed_at TIMESTAMPTZ,
    provenance JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_tasks_idempotency ON tasks (idempotency_key);

CREATE INDEX idx_tasks_scheduler
    ON tasks (state, not_before, priority, created_at);

CREATE INDEX idx_tasks_retry
    ON tasks (retry_at)
    WHERE state = 'RETRY_WAIT';

CREATE INDEX idx_tasks_lease_expiry
    ON tasks (lease_expires_at)
    WHERE state IN ('LEASED', 'RUNNING');

CREATE INDEX idx_tasks_job_state ON tasks (job_id, state);

CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks (task_id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks (task_id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);

CREATE INDEX idx_task_dependencies_depends
    ON task_dependencies (depends_on_task_id);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    media_type TEXT,
    artifact_kind TEXT NOT NULL,
    compressed_bytes BIGINT CHECK (compressed_bytes IS NULL OR compressed_bytes >= 0),
    uncompressed_bytes BIGINT CHECK (uncompressed_bytes IS NULL OR uncompressed_bytes >= 0),
    created_by_task TEXT REFERENCES tasks (task_id),
    derived_from JSONB NOT NULL DEFAULT '[]'::jsonb,
    provenance JSONB NOT NULL,
    schema_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_artifacts_content_dedup
    ON artifacts (content_hash, COALESCE(schema_version, ''), artifact_kind);

CREATE INDEX idx_artifacts_created_by_task
    ON artifacts (created_by_task)
    WHERE created_by_task IS NOT NULL;

ALTER TABLE tasks
    ADD CONSTRAINT fk_tasks_result_artifact
    FOREIGN KEY (result_artifact_id) REFERENCES artifacts (artifact_id);

CREATE TABLE lineage_edges (
    child_kind TEXT NOT NULL,
    child_id TEXT NOT NULL,
    parent_kind TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    version_tag TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT lineage_edges_unique_edge
        UNIQUE (child_kind, child_id, parent_kind, parent_id)
);

CREATE INDEX idx_lineage_edges_child
    ON lineage_edges (child_kind, child_id);

CREATE INDEX idx_lineage_edges_parent
    ON lineage_edges (parent_kind, parent_id);

CREATE OR REPLACE FUNCTION prevent_lineage_edge_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'lineage_edges is append-only';
END;
$$;

CREATE TRIGGER lineage_edges_no_update
    BEFORE UPDATE ON lineage_edges
    FOR EACH ROW
    EXECUTE FUNCTION prevent_lineage_edge_mutation();

CREATE TRIGGER lineage_edges_no_delete
    BEFORE DELETE ON lineage_edges
    FOR EACH ROW
    EXECUTE FUNCTION prevent_lineage_edge_mutation();

CREATE TABLE query_specs (
    query_spec_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_jobs (job_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    engine TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_query_specs_job ON query_specs (job_id);

CREATE TABLE outbox_events (
    event_id BIGSERIAL PRIMARY KEY,
    task_id TEXT REFERENCES tasks (task_id),
    stream_name TEXT NOT NULL,
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_outbox_unpublished
    ON outbox_events (created_at)
    WHERE published_at IS NULL;

CREATE TABLE workflow_defs (
    workflow_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    graph_yaml_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

CREATE TABLE workflow_nodes (
    workflow_id TEXT NOT NULL REFERENCES workflow_defs (workflow_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT,
    input_schemas JSONB NOT NULL DEFAULT '[]'::jsonb,
    output_schemas JSONB NOT NULL DEFAULT '[]'::jsonb,
    verifier TEXT,
    loop_budget INTEGER CHECK (loop_budget IS NULL OR loop_budget >= 1),
    PRIMARY KEY (workflow_id, node_id)
);

CREATE TABLE workflow_edges (
    workflow_id TEXT NOT NULL REFERENCES workflow_defs (workflow_id) ON DELETE CASCADE,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    condition_expr TEXT,
    max_trips INTEGER CHECK (max_trips IS NULL OR max_trips >= 1),
    PRIMARY KEY (workflow_id, from_node, to_node, edge_type),
    FOREIGN KEY (workflow_id, from_node)
        REFERENCES workflow_nodes (workflow_id, node_id),
    FOREIGN KEY (workflow_id, to_node)
        REFERENCES workflow_nodes (workflow_id, node_id)
);

CREATE TABLE workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflow_defs (workflow_id),
    job_id TEXT NOT NULL REFERENCES research_jobs (job_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE node_executions (
    run_id TEXT NOT NULL REFERENCES workflow_runs (run_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    task_id TEXT REFERENCES tasks (task_id),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    verdict TEXT,
    decision_ref TEXT,
    PRIMARY KEY (run_id, node_id, attempt)
);
