package postgres

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/contracts"
)

func (a Adapter) ReadEvents(ctx context.Context, scope, id string, after int64) ([]contracts.Event, bool, error) {
	events := []contracts.Event{}
	terminal := false
	err := a.within(ctx, scope, func(tx pgx.Tx) error {
		var status string
		if err := tx.QueryRow(ctx, "SELECT status FROM jobs WHERE workspace_id=$1 AND id=$2", scope, id).Scan(&status); err != nil {
			return err
		}
		terminal = status == "completed" || status == "failed" || status == "cancelled" || status == "waiting_review"
		rows, err := tx.Query(ctx, "SELECT payload FROM runtime_event_inbox WHERE workspace_id=$1 AND job_id=$2 AND sequence>$3 AND applied_at IS NOT NULL ORDER BY sequence LIMIT 200", scope, id, after)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var body []byte
			if err = rows.Scan(&body); err != nil {
				return err
			}
			var event contracts.Event
			if err = json.Unmarshal(body, &event); err != nil {
				return err
			}
			event.Type = strings.ReplaceAll(event.Type, "\n", "")
			events = append(events, event)
		}
		return rows.Err()
	})
	return events, terminal, err
}
