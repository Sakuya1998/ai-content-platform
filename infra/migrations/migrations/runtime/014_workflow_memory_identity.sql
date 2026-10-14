-- A workflow memory ID remains stable when its semantic type changes.
CREATE UNIQUE INDEX memory_nodes_workflow_source ON memory_nodes
    (workspace_id,project_id,workflow_id,source_id,source_version) WHERE workflow_id<>'';
