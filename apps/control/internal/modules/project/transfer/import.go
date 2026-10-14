package transfer

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
	"io"
	"os"
	"path"
	"regexp"
	"sort"
	"strings"
)

const maxResourceImportBytes int64 = 32 << 20

var errResourceInvalid = errors.New("invalid resource request")
var ErrInvalid = errResourceInvalid

type ContentEnvelope = execution.Envelope

func resourceHash(body []byte) string { sum := sha256.Sum256(body); return hex.EncodeToString(sum[:]) }
func resourceJSON(value any) []byte   { body, _ := json.Marshal(value); return body }
func resourceInt(value any) int       { return task.Integer(value) }
func resourceText(value any) string   { valueText, _ := value.(string); return valueText }
func normalizeResourceInput(value map[string]any) error {
	if project.Normalize(value) != nil {
		return errResourceInvalid
	}
	return nil
}
func sortResourceChapters(rows []map[string]any) {
	sort.SliceStable(rows, func(i, j int) bool { return resourceInt(rows[i]["block_index"]) < resourceInt(rows[j]["block_index"]) })
}

type BackupFile struct {
	Path   string `json:"path"`
	Data   string `json:"data"`
	SHA256 string `json:"sha256"`
}
type Backup struct {
	SchemaVersion   string             `json:"schema_version"`
	ProjectResource map[string]any     `json:"project"`
	Envelope        execution.Envelope `json:"envelope"`
	BriefVersions   []map[string]any   `json:"brief_versions,omitempty"`
	Files           []BackupFile       `json:"files,omitempty"`
	SHA256          string             `json:"sha256,omitempty"`
}

func Hash(backup Backup) string {
	backup.SHA256 = ""
	return resourceHash(resourceJSON(backup))
}
func DecodeBackup(body []byte) (Backup, error) {
	var backup Backup
	if int64(len(body)) > maxResourceImportBytes {
		return backup, errResourceInvalid
	}
	if err := json.Unmarshal(body, &backup); err != nil {
		return backup, errResourceInvalid
	}
	if backup.SchemaVersion != "1.0" || backup.ProjectResource == nil {
		return backup, errResourceInvalid
	}
	if backup.SHA256 == "" || backup.SHA256 != Hash(backup) {
		return backup, errResourceInvalid
	}
	seen := map[string]bool{}
	var size int64
	for _, file := range backup.Files {
		if file.Path == "" || path.IsAbs(file.Path) || path.Clean(file.Path) != file.Path || strings.ContainsAny(file.Path, "\\\x00") || seen[file.Path] {
			return backup, errResourceInvalid
		}
		for _, part := range strings.Split(file.Path, "/") {
			if part == ".." || part == "." || part == "" {
				return backup, errResourceInvalid
			}
		}
		seen[file.Path] = true
		data, err := base64.StdEncoding.DecodeString(file.Data)
		if err != nil || resourceHash(data) != file.SHA256 {
			return backup, errResourceInvalid
		}
		size += int64(len(data))
		if size > maxResourceImportBytes {
			return backup, errResourceInvalid
		}
	}
	if err := normalizeResourceInput(backup.ProjectResource); err != nil {
		return backup, err
	}
	return backup, nil
}

var resourceChapterHeading = regexp.MustCompile(`^(?:#{1,6}\s*)?(?:第[0-9零一二三四五六七八九十百千]+[章节回卷]|[0-9]+[.)、]|Chapter\s+[0-9]+)\s*.*$`)

func ImportText(text, filename string) Backup {
	text = strings.TrimPrefix(text, "\ufeff")
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	title := strings.TrimSuffix(path.Base(filename), path.Ext(filename))
	if title == "" {
		title = "Imported novel"
	}
	chapters := []map[string]any{}
	var current strings.Builder
	heading := ""
	flush := func() {
		content := strings.TrimSpace(current.String())
		if content != "" {
			chapters = append(chapters, map[string]any{"block_index": len(chapters) + 1, "version_number": 1, "title": heading, "content": content, "status": "final"})
		}
		current.Reset()
	}
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if i == 0 && strings.HasPrefix(trimmed, "# ") {
			title = strings.TrimSpace(strings.TrimPrefix(trimmed, "# "))
			continue
		}
		if resourceChapterHeading.MatchString(trimmed) {
			flush()
			heading = strings.TrimLeft(trimmed, "# ")
			continue
		}
		current.WriteString(line + "\n")
	}
	flush()
	if len(chapters) == 0 {
		chapters = append(chapters, map[string]any{"block_index": 1, "version_number": 1, "title": "", "content": "", "status": "final"})
	}
	total := len(chapters)
	if total > 50 {
		total = 50
	}
	backup := Backup{SchemaVersion: "1.0", ProjectResource: map[string]any{"title": title, "content_type": "copy", "input": map[string]any{}, "brief": map[string]any{}}, Envelope: ContentEnvelope{State: map[string]any{"blocks": chapters, "block_count": total}, ContentVersions: chapters}}
	_ = normalizeResourceInput(backup.ProjectResource)
	backup.SHA256 = Hash(backup)
	return backup
}
func DecodePublication(body []byte, format, filename string) (Backup, error) {
	archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil || len(archive.File) > 1000 {
		return Backup{}, errResourceInvalid
	}
	files := map[string][]byte{}
	var total int64
	for _, file := range archive.File {
		if file.FileInfo().IsDir() {
			continue
		}
		if path.IsAbs(file.Name) || path.Clean(file.Name) != file.Name || strings.ContainsAny(file.Name, "\\\x00") || file.Mode()&os.ModeSymlink != 0 || file.UncompressedSize64 > uint64(maxResourceImportBytes) {
			return Backup{}, errResourceInvalid
		}
		if files[file.Name] != nil {
			return Backup{}, errResourceInvalid
		}
		reader, err := file.Open()
		if err != nil {
			return Backup{}, errResourceInvalid
		}
		data, err := io.ReadAll(io.LimitReader(reader, maxResourceImportBytes-total+1))
		_ = reader.Close()
		if err != nil {
			return Backup{}, errResourceInvalid
		}
		total += int64(len(data))
		if total > maxResourceImportBytes {
			return Backup{}, errResourceInvalid
		}
		files[file.Name] = data
	}
	if format == "docx" {
		document := files["word/document.xml"]
		if document == nil {
			return Backup{}, errResourceInvalid
		}
		text, err := xmlDocumentText(document, "docx")
		if err != nil {
			return Backup{}, err
		}
		lines := strings.Split(strings.TrimSpace(text), "\n")
		if len(lines) > 1 {
			text = "# " + lines[0] + "\n" + strings.Join(lines[1:], "\n")
		}
		backup := ImportText(text, filename)
		if err := normalizeResourceInput(backup.ProjectResource); err != nil {
			return Backup{}, err
		}
		return backup, nil
	}
	// Resolve EPUB's declared reading order instead of guessing archive entry order.
	container := files["META-INF/container.xml"]
	var root struct {
		Rootfiles []struct {
			Path string `xml:"full-path,attr"`
		} `xml:"rootfiles>rootfile"`
	}
	if xml.Unmarshal(container, &root) != nil || len(root.Rootfiles) == 0 {
		return Backup{}, errResourceInvalid
	}
	opfPath := root.Rootfiles[0].Path
	var opf struct {
		Title string `xml:"metadata>title"`
		Items []struct {
			ID   string `xml:"id,attr"`
			Href string `xml:"href,attr"`
		} `xml:"manifest>item"`
		Spine []struct {
			ID string `xml:"idref,attr"`
		} `xml:"spine>itemref"`
	}
	if xml.Unmarshal(files[opfPath], &opf) != nil {
		return Backup{}, errResourceInvalid
	}
	items := map[string]string{}
	for _, item := range opf.Items {
		if strings.Contains(item.Href, ":") || path.IsAbs(item.Href) {
			return Backup{}, errResourceInvalid
		}
		items[item.ID] = path.Join(path.Dir(opfPath), item.Href)
	}
	var text strings.Builder
	if opf.Title != "" {
		text.WriteString("# " + opf.Title + "\n")
	}
	for index, item := range opf.Spine {
		data := files[items[item.ID]]
		if data == nil {
			return Backup{}, errResourceInvalid
		}
		part, err := xmlDocumentText(data, "epub")
		if err != nil {
			return Backup{}, err
		}
		lines := strings.Split(strings.TrimSpace(part), "\n")
		heading := ""
		content := part
		if len(lines) > 0 {
			heading = lines[0]
			content = strings.Join(lines[1:], "\n")
		}
		text.WriteString(fmt.Sprintf("Chapter %d %s\n%s\n", index+1, heading, content))
	}
	if text.Len() == 0 {
		return Backup{}, errResourceInvalid
	}
	backup := ImportText(text.String(), filename)
	if err := normalizeResourceInput(backup.ProjectResource); err != nil {
		return Backup{}, err
	}
	return backup, nil
}
func xmlDocumentText(body []byte, format string) (string, error) {
	decoder := xml.NewDecoder(bytes.NewReader(body))
	var output strings.Builder
	capture := 0
	inBody := false
	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", errResourceInvalid
		}
		switch value := token.(type) {
		case xml.StartElement:
			if value.Name.Local == "body" {
				inBody = true
			}
			if format == "docx" && value.Name.Local == "t" {
				capture++
			}
			if format == "epub" && inBody && value.Name.Local != "script" && value.Name.Local != "style" {
				capture++
			}
		case xml.CharData:
			if capture > 0 {
				output.Write([]byte(value))
			}
		case xml.EndElement:
			if format == "docx" && value.Name.Local == "t" && capture > 0 {
				capture--
			}
			if format == "epub" && inBody && value.Name.Local != "script" && value.Name.Local != "style" && capture > 0 {
				capture--
			}
			if value.Name.Local == "p" || value.Name.Local == "h1" || value.Name.Local == "h2" || value.Name.Local == "br" {
				output.WriteByte('\n')
			}
			if value.Name.Local == "body" {
				inBody = false
			}
		}
	}
	return output.String(), nil
}
