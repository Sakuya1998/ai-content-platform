package postgres

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/governance"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/identity"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/database"
)

type catalogRequest struct {
	ctx     context.Context
	action  governance.Action
	payload json.RawMessage
	filters map[string]string
}

func (r *catalogRequest) Context() context.Context { return r.ctx }

type catalogResult struct{ result governance.CatalogResult }

func catalogComplete(w *catalogResult, code int, value any) {
	kind := map[int]string{200: "ok", 201: "created", 204: "empty", 400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "missing", 405: "unsupported", 409: "conflict", 413: "too_large", 422: "invalid", 503: "unavailable"}[code]
	if kind == "" {
		kind = "unavailable"
	}
	w.result = governance.CatalogResult{Kind: kind, Value: value}
}
func catalogReject(w *catalogResult, code int, message string) {
	catalogComplete(w, code, map[string]string{"error": message})
}
func catalogNotFound(w *catalogResult)    { catalogReject(w, 404, "resource not found") }
func catalogUnsupported(w *catalogResult) { catalogReject(w, 405, "operation not supported") }
func catalogDBError(w *catalogResult, err error) {
	var pgerr *pgconn.PgError
	if errors.Is(err, pgx.ErrNoRows) {
		catalogReject(w, 404, "resource not found")
	} else if errors.As(err, &pgerr) && (pgerr.Code == "23505" || pgerr.Code == "23503") {
		catalogReject(w, 409, "resource conflict")
	} else {
		catalogReject(w, 503, "storage unavailable")
	}
}
func catalogPromptError(w *catalogResult, err error) {
	var invalid *governance.PromptValidationError
	if errors.As(err, &invalid) {
		catalogReject(w, 422, invalid.Error())
		return
	}
	catalogDBError(w, err)
}
func decodeCatalogJSON(w *catalogResult, r *catalogRequest, value any) bool {
	decoder := json.NewDecoder(bytes.NewReader(r.payload))
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil {
		catalogReject(w, 400, "invalid request body")
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		catalogReject(w, 400, "request body must contain one JSON object")
		return false
	}
	return true
}
func decodeCatalogBody(r *catalogRequest, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(r.payload))
	decoder.DisallowUnknownFields()
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return governance.PromptInvalid("invalid request body: %v", err)
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return governance.PromptInvalid("request body must contain one JSON object")
	}
	return nil
}
func workspaceTx(ctx context.Context, pool *pgxpool.Pool, workspace string) (pgx.Tx, error) {
	if pool == nil {
		return nil, errors.New("catalog storage unavailable")
	}
	return database.BeginWorkspaceTx(ctx, pool, workspace)
}

type Principal = identity.Principal
