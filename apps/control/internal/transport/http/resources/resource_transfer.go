package resources

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project/transfer"
)

type ResourceBackup = transfer.Backup
type ResourceBackupFile = transfer.BackupFile

var backupHash = transfer.Hash
var decodeResourceBackup = transfer.DecodeBackup
var encryptResourceBackup = transfer.Encrypt
var decryptResourceBackup = transfer.Decrypt
var resourceChapters = transfer.Blocks
var encodeResourceBackupZIP = transfer.EncodeZIP
var decodeResourceBackupZIP = transfer.DecodeZIP
var renderResourcePublication = transfer.RenderPublication
var resourceImportText = transfer.ImportText
var decodeResourcePublication = transfer.DecodePublication

const resourceEncryptedMagic = transfer.EncryptedMagic

func (a *ResourceAPI) importHTTP(w http.ResponseWriter, r *http.Request, scope string) {
	if r.Method != "POST" {
		methodNotAllowed(w)
		return
	}
	backup, err := readResourceImport(w, r)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if key == "" {
		key = "import:" + backup.SHA256
	}
	if len(key) > 200 {
		a.resourceError(w, errResourceInvalid)
		return
	}
	inputKey := "import:" + key
	if len(inputKey) > 200 {
		inputKey = resourceHash([]byte(inputKey))
	}
	novel, err := a.transfers.Import(r.Context(), scope, inputKey, backup)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 201, map[string]any{"project": novel, "imported_blocks": len(backup.Envelope.ContentVersions), "source_format": "backup", "runtime_restore_required": true})
}
func (a *ResourceAPI) buildBackup(ctx context.Context, scope, id string) (ResourceBackup, error) {
	return a.transfers.Backup(ctx, scope, id)
}
func (a *ResourceAPI) exportBytes(ctx context.Context, scope, id, format string) ([]byte, string, error) {
	backup, err := a.buildBackup(ctx, scope, id)
	if err != nil {
		return nil, "", err
	}
	if format == "backup" {
		body, err := encodeResourceBackupZIP(backup)
		return body, "application/zip", err
	}
	if format == "docx" || format == "epub" {
		body, err := renderResourcePublication(backup, format, nil)
		return body, resourceExportMediaType(format), err
	}
	if format != "markdown" && format != "txt" {
		return nil, "", errResourceInvalid
	}
	var chapters []map[string]any
	if values, ok := backup.Envelope.State["blocks"].([]any); ok {
		for _, value := range values {
			if row, ok := value.(map[string]any); ok {
				chapters = append(chapters, row)
			}
		}
	}
	if len(chapters) == 0 {
		latest := map[int]map[string]any{}
		for _, row := range backup.Envelope.ContentVersions {
			n := resourceInt(row["block_index"])
			if n == 0 {
				n = resourceInt(row["content"])
			}
			if latest[n] == nil || resourceInt(row["version_number"]) > resourceInt(latest[n]["version_number"]) {
				copy := map[string]any{}
				for k, v := range row {
					copy[k] = v
				}
				copy["block_index"] = n
				latest[n] = copy
			}
		}
		for _, row := range latest {
			chapters = append(chapters, row)
		}
	}
	sortResourceChapters(chapters)
	var out strings.Builder
	title := resourceText(backup.ProjectResource["title"])
	if format == "markdown" {
		out.WriteString("# ")
	}
	out.WriteString(title + "\n\n")
	for _, chapter := range chapters {
		if format == "markdown" {
			out.WriteString("## ")
		}
		out.WriteString(fmt.Sprintf("%d. %s\n\n%s\n\n", resourceInt(chapter["block_index"]), resourceText(chapter["title"]), resourceText(chapter["content"])))
	}
	return []byte(out.String()), "text/plain; charset=utf-8", nil
}
func (a *ResourceAPI) exportHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource, tail []string) {
	if len(tail) == 0 && r.Method == "GET" {
		format := r.URL.Query().Get("format")
		if format == "" {
			format = "backup"
		}
		body, contentType, err := a.exportBytes(r.Context(), scope, novel.ID, format)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		resourceDownload(w, body, contentType, novel.ID, format)
		return
	}
	if len(tail) == 1 && tail[0] == "jobs" && r.Method == "POST" {
		var req struct {
			Format    string `json:"format"`
			Password  string `json:"password"`
			Author    string `json:"author"`
			Publisher string `json:"publisher"`
			Language  string `json:"language"`
		}
		if !readResourceBody(w, r, &req) {
			return
		}
		if req.Format == "" {
			req.Format = "backup"
		}
		if len(req.Password) > 200 || (req.Password != "" && req.Format != "backup") || !resourceExportFormat(req.Format) {
			a.resourceError(w, errResourceInvalid)
			return
		}
		id := uuid.NewString()
		backup, err := a.buildBackup(r.Context(), scope, novel.ID)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		metadata := map[string]string{"author": req.Author, "publisher": req.Publisher, "language": req.Language}
		pending, err := a.exports.EncodePending(backup, metadata, req.Password)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		err = a.transfers.QueueTransfer(r.Context(), scope, id, novel.ID, req.Format, pending)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		go a.exports.Execute(scope, id)
		writeJSON(w, 202, map[string]any{"job_id": id, "project_id": novel.ID, "status": "queued", "format": req.Format})
		return
	}
	methodNotAllowed(w)
}
func resourceDownload(w http.ResponseWriter, body []byte, contentType, id, format string) {
	extension := map[string]string{"backup": "project-backup.zip", "markdown": "md", "txt": "txt", "docx": "docx", "epub": "epub"}[format]
	if strings.HasPrefix(string(body), resourceEncryptedMagic) {
		extension = "project-backup.enc"
		contentType = "application/octet-stream"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=\"%s.%s\"", id, extension))
	w.Header().Set("X-Content-SHA256", resourceHash(body))
	w.Header().Set("Content-Length", fmt.Sprint(len(body)))
	w.WriteHeader(200)
	_, _ = w.Write(body)
}
func (a *ResourceAPI) transferHTTP(w http.ResponseWriter, r *http.Request, scope string, parts []string) {
	if len(parts) < 1 {
		http.NotFound(w, r)
		return
	}
	if _, err := uuid.Parse(parts[0]); err != nil {
		a.resourceError(w, ErrNotFound)
		return
	}
	id := parts[0]
	value, err := a.transfers.Transfer(r.Context(), scope, id)
	novel, status, format, hash, failure, body := value.ProjectID, value.Status, value.Format, value.Hash, value.Failure, value.Body
	if err != nil {
		a.resourceError(w, err)
		return
	}
	if len(parts) == 1 && r.Method == "GET" {
		writeJSON(w, 200, map[string]any{"job_id": id, "project_id": novel, "status": status, "format": format, "sha256": hash, "error": failure})
		return
	}
	if len(parts) == 2 && parts[1] == "cancel" && r.Method == "POST" {
		err := a.transfers.CancelTransfer(r.Context(), scope, id)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"job_id": id, "status": "cancelled"})
		return
	}
	if len(parts) == 2 && parts[1] == "download" && r.Method == "GET" {
		if status != "completed" || resourceHash(body) != hash {
			a.resourceError(w, ErrMessageConflict)
			return
		}
		contentType := resourceExportMediaType(format)
		resourceDownload(w, body, contentType, novel, format)
		return
	}
	methodNotAllowed(w)
}
func resourceExportFormat(format string) bool {
	return format == "backup" || format == "markdown" || format == "txt" || format == "docx" || format == "epub"
}
func resourceExportMediaType(format string) string {
	switch format {
	case "backup":
		return "application/zip"
	case "markdown":
		return "text/markdown; charset=utf-8"
	case "docx":
		return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
	case "epub":
		return "application/epub+zip"
	default:
		return "text/plain; charset=utf-8"
	}
}
