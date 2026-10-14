package postgres

import (
	"context"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/apps/control/internal/contracts"
)

// Mutable fixtures simulate catalog retirement and storage failures after submission.
func (r *MQRepository) WithContentArtifacts(reader contentReader) *MQRepository {
	r.contentArtifacts = reader
	return r
}
func (r *MQRepository) WithExecutionSnapshotStore(writer executionSnapshotWriter) *MQRepository {
	r.executionSnapshotStore = writer
	return r
}
func (r *MQRepository) WithSnapshotFreezer(f SnapshotFreezer) *MQRepository {
	r.snapshotFreezer = f
	return r
}
func (r *MQRepository) WithExecutionResources(f executionResourceResolver) *MQRepository {
	r.executionResourceResolver = f
	return r
}
func (r *MQRepository) WithProjectProjector(f func(context.Context, pgx.Tx, contracts.Event) error) *MQRepository {
	r.projectProjector = f
	return r
}
