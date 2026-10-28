package operations

import (
	"net/http"
	"strconv"
	"strings"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
)

const evaluationPoliciesPath = "/api/evaluations/policies"

func (a *OperationsAPI) evaluationPolicies(w http.ResponseWriter, r *http.Request, principal identity.Principal, path string) {
	if a.evaluations == nil {
		operationError(w, evaluation.ErrUnavailable)
		return
	}
	if path == evaluationPoliciesPath {
		switch r.Method {
		case http.MethodGet:
			items, err := a.evaluations.ListPolicies(r.Context(), principal.WorkspaceID, evaluation.PolicyFilters{
				PolicyID: r.URL.Query().Get("policy_id"), Status: r.URL.Query().Get("status"),
			})
			if err != nil {
				operationError(w, err)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"items": items})
		case http.MethodPost:
			var policy evaluation.EvaluationPolicyVersion
			if !decodeJSONWithLimit(w, r, &policy, 256<<10) {
				return
			}
			if err := a.evaluations.CreatePolicy(r.Context(), principal.WorkspaceID, policy.PolicyID, &policy); err != nil {
				operationError(w, err)
				return
			}
			created, err := a.evaluations.GetPolicy(r.Context(), principal.WorkspaceID, policy.PolicyID, policy.Version)
			if err != nil {
				operationError(w, err)
				return
			}
			writeJSON(w, http.StatusCreated, created)
		default:
			methodNotAllowed(w)
		}
		return
	}

	parts := strings.Split(strings.TrimPrefix(path, evaluationPoliciesPath+"/"), "/")
	if len(parts) < 3 || parts[0] == "" || parts[1] != "v" {
		http.NotFound(w, r)
		return
	}
	version, err := strconv.Atoi(parts[2])
	if err != nil || version < 1 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "version must be a positive integer"})
		return
	}
	policyID := parts[0]
	if len(parts) == 3 && r.Method == http.MethodGet {
		policy, err := a.evaluations.GetPolicy(r.Context(), principal.WorkspaceID, policyID, version)
		if err != nil {
			operationError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, policy)
		return
	}
	if len(parts) != 4 || r.Method != http.MethodPost || parts[3] != "publish" && parts[3] != "archive" {
		if r.Method != http.MethodPost && r.Method != http.MethodGet {
			methodNotAllowed(w)
		} else {
			http.NotFound(w, r)
		}
		return
	}
	if parts[3] == "publish" {
		err = a.evaluations.PublishPolicy(r.Context(), principal.WorkspaceID, policyID, version)
	} else {
		err = a.evaluations.ArchivePolicy(r.Context(), principal.WorkspaceID, policyID, version)
	}
	if err != nil {
		operationError(w, err)
		return
	}
	policy, err := a.evaluations.GetPolicy(r.Context(), principal.WorkspaceID, policyID, version)
	if err != nil {
		operationError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, policy)
}
