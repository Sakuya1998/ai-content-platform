-- Stateful edits are serialized; independent media derivations can run together.
DROP INDEX jobs_single_active_project_idx;
CREATE UNIQUE INDEX jobs_single_active_project_mutation_idx ON jobs(project_id)
 WHERE status IN ('queued','running','cancel_requested')
 AND request->'payload'->>'operation' IN
 ('run','resume','revision','candidates','canon','evaluation','memory_rebuild','memory_evaluate');
