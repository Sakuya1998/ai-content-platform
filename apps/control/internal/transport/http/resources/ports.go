package resources

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"

	"github.com/sakuya/novel-agent/apps/control/internal/contracts"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/content"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/identity"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project/transfer"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
	"github.com/sakuya/novel-agent/apps/control/internal/transport/http/response"
)

type Messaging interface {
	Generate(context.Context, string, execution.Request) (contracts.Command, error)
}

type Projects interface {
	project.Repository
	Get(context.Context, string, string) (project.Resource, error)
	List(context.Context, string, string, int) ([]project.Resource, error)
	Delete(context.Context, string, string) error
	Update(context.Context, string, string, int64, map[string]any) (project.Resource, error)
	UpdateBrief(context.Context, string, string, int, string, map[string]any, string) (project.Resource, bool, int64, error)
	BriefVersions(context.Context, string, string) ([]map[string]any, error)
}
type Content interface {
	content.Repository
	Documents(context.Context, string, string) ([]map[string]any, error)
	Document(context.Context, string, string) (map[string]any, error)
	VersionByID(context.Context, string, string, string) (json.RawMessage, error)
	Version(context.Context, string, string, int) (json.RawMessage, error)
	Versions(context.Context, string, string) ([]map[string]any, error)
	CreateDocument(context.Context, string, string, string, string) (map[string]any, error)
}
type Tasks interface {
	List(context.Context, string, string, int) ([]map[string]any, error)
	Get(context.Context, string, string) (task.Job, error)
	Control(context.Context, string, contracts.Command) error
	EventSequence(context.Context, string, string, string) (int64, error)
	ReadEvents(context.Context, string, string, int64) ([]contracts.Event, bool, error)
}
type Records interface {
	Records(context.Context, string, string, string, int, string) ([]map[string]any, error)
	Version(context.Context, string, string, string, int, int, string) (map[string]any, error)
	Baseline(context.Context, string, string, int, string) error
	Diff(context.Context, string, string, string, int, int, int, string) (map[string]any, map[string]any, error)
}
type Transfers interface {
	transfer.Repository
	Import(context.Context, string, string, transfer.Backup) (project.Resource, error)
}
type Dependencies struct {
	Projects       Projects
	Content        Content
	Tasks          Tasks
	Records        Records
	Transfers      Transfers
	Exports        *transfer.Service
	Messaging      Messaging
	SubmitCommand  func(context.Context, string, project.Resource, string, map[string]any, string) (contracts.Command, error)
	NormalizeError func(error) error
}

type SnapshotFreezer = execution.SnapshotFreezer
type GenerationRequest = execution.Request
type JobProjection struct {
	ID       string
	Status   string
	Sequence int64
	Result   json.RawMessage
}

var ErrNotFound = project.ErrNotFound
var ErrMessageConflict = task.ErrConflict

func PrincipalFromContext(ctx context.Context) (identity.Principal, bool) {
	return identity.FromContext(ctx)
}
func writeJSON(w http.ResponseWriter, status int, value any) { response.JSON(w, status, value) }
func methodNotAllowed(w http.ResponseWriter)                 { response.MethodNotAllowed(w) }
func sameJSON(left, right []byte) bool {
	var a, b any
	if json.Unmarshal(left, &a) != nil || json.Unmarshal(right, &b) != nil {
		return false
	}
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return bytes.Equal(x, y)
}

func selectedWorkflow(request GenerationRequest) string {
	if request.Operation == "assemble" {
		return "media.assemble"
	}
	if request.Operation == "compose" {
		return "media.compose"
	}
	switch request.DocumentType {
	case "video", "image", "storyboard", "timeline":
		return "media." + request.DocumentType
	case "audio":
		if _, exists := request.Arguments["music"]; exists {
			return "media.music"
		}
		if _, exists := request.Arguments["effect"]; exists {
			return "media.sound_effect"
		}
		return "media.speech"
	}
	if request.SourceVersionID != "" {
		return "content.adapt"
	}
	return "content.generate"
}
