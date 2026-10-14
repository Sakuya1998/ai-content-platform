-- A title is editable presentation data; document IDs define immutable identity.
ALTER TABLE documents DROP CONSTRAINT documents_workspace_id_project_id_title_key;
CREATE INDEX documents_project_title_idx ON documents(workspace_id,project_id,title);
