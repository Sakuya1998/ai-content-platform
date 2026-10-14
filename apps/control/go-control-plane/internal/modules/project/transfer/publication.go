package transfer

import (
	"archive/zip"
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strings"

	"golang.org/x/crypto/scrypt"
)

const EncryptedMagic = "NOVEL_AGENT_ENCRYPTED_BACKUP\x01\n"

func Encrypt(body []byte, password string) ([]byte, error) {
	if password == "" {
		return body, nil
	}
	salt := make([]byte, 16)
	nonce := make([]byte, 12)
	if _, err := rand.Read(salt); err != nil {
		return nil, err
	}
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	key, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, 32)
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	header := map[string]any{"schema_version": "novel-agent-encrypted-backup-v1", "kdf": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": base64.URLEncoding.EncodeToString(salt), "nonce": base64.URLEncoding.EncodeToString(nonce)}
	output := append([]byte(EncryptedMagic), resourceJSON(header)...)
	output = append(output, []byte("\n\n")...)
	return gcm.Seal(output, nonce, body, []byte("novel-agent-backup")), nil
}
func Decrypt(body []byte, password string) ([]byte, error) {
	if !bytes.HasPrefix(body, []byte(EncryptedMagic)) {
		return body, nil
	}
	if password == "" || len(password) > 200 {
		return nil, errResourceInvalid
	}
	parts := bytes.SplitN(body[len(EncryptedMagic):], []byte("\n\n"), 2)
	if len(parts) != 2 || len(parts[0]) > 4096 {
		return nil, errResourceInvalid
	}
	var header struct {
		Version string `json:"schema_version"`
		KDF     string `json:"kdf"`
		N       int    `json:"n"`
		R       int    `json:"r"`
		P       int    `json:"p"`
		Salt    string `json:"salt"`
		Nonce   string `json:"nonce"`
	}
	if json.Unmarshal(parts[0], &header) != nil || header.Version != "novel-agent-encrypted-backup-v1" || header.KDF != "scrypt" || header.N != 16384 || header.R != 8 || header.P != 1 {
		return nil, errResourceInvalid
	}
	salt, e1 := base64.URLEncoding.DecodeString(header.Salt)
	nonce, e2 := base64.URLEncoding.DecodeString(header.Nonce)
	if e1 != nil || e2 != nil || len(salt) != 16 || len(nonce) != 12 {
		return nil, errResourceInvalid
	}
	key, err := scrypt.Key([]byte(password), salt, header.N, header.R, header.P, 32)
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	plain, err := gcm.Open(nil, nonce, parts[1], []byte("novel-agent-backup"))
	if err != nil {
		return nil, errResourceInvalid
	}
	return plain, nil
}
func Blocks(backup Backup) []map[string]any {
	values := []map[string]any{}
	if chapters, ok := backup.Envelope.State["blocks"].([]any); ok {
		for _, chapter := range chapters {
			if row, ok := chapter.(map[string]any); ok {
				values = append(values, row)
			}
		}
	}
	if len(values) == 0 {
		latest := map[int]map[string]any{}
		for _, row := range backup.Envelope.ContentVersions {
			number := resourceInt(row["block_index"])
			if number == 0 {
				number = resourceInt(row["content"])
			}
			if latest[number] == nil || resourceInt(row["version_number"]) > resourceInt(latest[number]["version_number"]) {
				latest[number] = row
			}
		}
		for _, row := range latest {
			values = append(values, row)
		}
	}
	sortResourceChapters(values)
	return values
}
func EncodeZIP(backup Backup) ([]byte, error) {
	files := map[string][]byte{"project.json": resourceJSON(backup.ProjectResource), "progress.json": resourceJSON(map[string]any{"state": backup.Envelope.State, "management_envelope": backup.Envelope, "brief_versions": backup.BriefVersions}), "memory_snapshots.json": resourceJSON(backup.Envelope.MemorySnapshots), "memory_quality_runs.json": resourceJSON(backup.Envelope.MemoryQuality), "checkpoint.json": resourceJSON(map[string]any{"state": backup.Envelope.State})}
	chapters := Blocks(backup)
	for i, chapter := range chapters {
		files[fmt.Sprintf("blocks/%04d.json", i+1)] = resourceJSON(chapter)
	}
	checksums := map[string]string{}
	for name, body := range files {
		checksums[name] = resourceHash(body)
	}
	files["manifest.json"] = resourceJSON(map[string]any{"schema_version": "agent-content-backup-v1", "block_count": len(chapters), "checksums": checksums})
	return zipFiles(files, "", nil)
}
func DecodeZIP(body []byte) (Backup, error) {
	result := Backup{SchemaVersion: "1.0"}
	archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		return result, errResourceInvalid
	}
	if len(archive.File) > 1000 {
		return result, errResourceInvalid
	}
	files := map[string][]byte{}
	var total int64
	for _, file := range archive.File {
		name := file.Name
		if path.IsAbs(name) || path.Clean(name) != name || strings.ContainsAny(name, "\\\x00") || file.Mode()&os.ModeSymlink != 0 || files[name] != nil {
			return result, errResourceInvalid
		}
		if name != "manifest.json" && name != "project.json" && name != "progress.json" && name != "memory_snapshots.json" && name != "memory_quality_runs.json" && name != "checkpoint.json" && !(strings.HasPrefix(name, "blocks/") && strings.HasSuffix(name, ".json") && len(strings.Split(name, "/")) == 2) {
			return result, errResourceInvalid
		}
		if file.UncompressedSize64 > uint64(maxResourceImportBytes) {
			return result, errResourceInvalid
		}
		reader, err := file.Open()
		if err != nil {
			return result, errResourceInvalid
		}
		data, readErr := io.ReadAll(io.LimitReader(reader, maxResourceImportBytes-total+1))
		closeErr := reader.Close()
		if readErr != nil || closeErr != nil {
			return result, errResourceInvalid
		}
		total += int64(len(data))
		if total > maxResourceImportBytes {
			return result, errResourceInvalid
		}
		files[name] = data
	}
	var manifest struct {
		Version   string            `json:"schema_version"`
		Checksums map[string]string `json:"checksums"`
	}
	if json.Unmarshal(files["manifest.json"], &manifest) != nil || manifest.Version != "agent-content-backup-v1" || manifest.Checksums == nil {
		return result, errResourceInvalid
	}
	for name, data := range files {
		if name != "manifest.json" && manifest.Checksums[name] != resourceHash(data) {
			return result, errResourceInvalid
		}
	}
	for name := range manifest.Checksums {
		if files[name] == nil {
			return result, errResourceInvalid
		}
	}
	var source map[string]any
	if json.Unmarshal(files["project.json"], &source) != nil {
		return result, errResourceInvalid
	}
	result.ProjectResource = map[string]any{}
	for _, key := range []string{"title", "content_type", "input", "brief"} {
		if value, ok := source[key]; ok {
			result.ProjectResource[key] = value
		}
	}
	var progress struct {
		State    map[string]any   `json:"state"`
		Envelope *ContentEnvelope `json:"management_envelope"`
		Brief    []map[string]any `json:"brief_versions"`
	}
	if data := files["progress.json"]; len(data) > 0 && json.Unmarshal(data, &progress) != nil {
		return result, errResourceInvalid
	}
	result.BriefVersions = progress.Brief
	if progress.Envelope != nil {
		result.Envelope = *progress.Envelope
	} else {
		result.Envelope.State = progress.State
		var checkpoint struct {
			State map[string]any `json:"state"`
		}
		if data := files["checkpoint.json"]; len(data) > 0 {
			if json.Unmarshal(data, &checkpoint) != nil {
				return result, errResourceInvalid
			}
			if checkpoint.State != nil {
				result.Envelope.State = checkpoint.State
			}
		}
		chapters := []map[string]any{}
		names := []string{}
		for name := range files {
			if strings.HasPrefix(name, "blocks/") {
				names = append(names, name)
			}
		}
		sort.Strings(names)
		for _, name := range names {
			var row map[string]any
			if json.Unmarshal(files[name], &row) != nil {
				return result, errResourceInvalid
			}
			chapters = append(chapters, row)
			copy := map[string]any{}
			for k, v := range row {
				copy[k] = v
			}
			copy["version_number"] = 1
			result.Envelope.ContentVersions = append(result.Envelope.ContentVersions, copy)
		}
		if result.Envelope.State == nil {
			result.Envelope.State = map[string]any{}
		}
		result.Envelope.State["blocks"] = chapters
		if data := files["memory_snapshots.json"]; len(data) > 0 && string(data) != "null" {
			if json.Unmarshal(data, &result.Envelope.MemorySnapshots) != nil {
				return result, errResourceInvalid
			}
		}
		if data := files["memory_quality_runs.json"]; len(data) > 0 && string(data) != "null" {
			if json.Unmarshal(data, &result.Envelope.MemoryQuality) != nil {
				return result, errResourceInvalid
			}
		}
	}
	if err = normalizeResourceInput(result.ProjectResource); err != nil {
		return result, err
	}
	result.SHA256 = Hash(result)
	return result, nil
}
func zipFiles(files map[string][]byte, first string, stored map[string]bool) ([]byte, error) {
	var output bytes.Buffer
	writer := zip.NewWriter(&output)
	names := []string{}
	for name := range files {
		if name != first {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	if first != "" {
		names = append([]string{first}, names...)
	}
	for _, name := range names {
		method := uint16(zip.Deflate)
		if stored[name] {
			method = zip.Store
		}
		entry, err := writer.CreateHeader(&zip.FileHeader{Name: name, Method: method})
		if err != nil {
			return nil, err
		}
		if _, err = entry.Write(files[name]); err != nil {
			return nil, err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}
func xmlText(text string) string {
	var escaped bytes.Buffer
	_ = xml.EscapeText(&escaped, []byte(text))
	return escaped.String()
}
func RenderPublication(backup Backup, format string, metadata map[string]string) ([]byte, error) {
	title := xmlText(resourceText(backup.ProjectResource["title"]))
	chapters := Blocks(backup)
	author := xmlText(metadata["author"])
	language := xmlText(metadata["language"])
	if language == "" {
		language = "zh-CN"
	}
	if format == "docx" {
		var document strings.Builder
		document.WriteString(`<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>`)
		paragraph := func(text string) {
			document.WriteString(`<w:p><w:r><w:t xml:space="preserve">` + xmlText(text) + `</w:t></w:r></w:p>`)
		}
		paragraph(resourceText(backup.ProjectResource["title"]))
		for _, chapter := range chapters {
			paragraph(fmt.Sprintf("%d. %s", resourceInt(chapter["block_index"]), resourceText(chapter["title"])))
			for _, line := range strings.Split(resourceText(chapter["content"]), "\n") {
				paragraph(line)
			}
		}
		document.WriteString(`<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>`)
		return zipFiles(map[string][]byte{"[Content_Types].xml": []byte(`<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>`), "_rels/.rels": []byte(`<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`), "word/document.xml": []byte(document.String())}, "", nil)
	}
	if format != "epub" {
		return nil, errResourceInvalid
	}
	files := map[string][]byte{"mimetype": []byte("application/epub+zip"), "META-INF/container.xml": []byte(`<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>`)}
	var manifest, spine, nav strings.Builder
	for i, chapter := range chapters {
		file := fmt.Sprintf("chapter-%d.xhtml", i+1)
		label := xmlText(resourceText(chapter["title"]))
		if label == "" {
			label = fmt.Sprintf("Chapter %d", i+1)
		}
		var content strings.Builder
		for _, line := range strings.Split(resourceText(chapter["content"]), "\n") {
			content.WriteString("<p>" + xmlText(line) + "</p>")
		}
		files["OEBPS/"+file] = []byte(`<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>` + label + `</title></head><body><h1>` + label + `</h1>` + content.String() + `</body></html>`)
		fmt.Fprintf(&manifest, `<item id="c%d" href="%s" media-type="application/xhtml+xml"/>`, i+1, file)
		fmt.Fprintf(&spine, `<itemref idref="c%d"/>`, i+1)
		fmt.Fprintf(&nav, `<li><a href="%s">%s</a></li>`, file, label)
	}
	files["OEBPS/nav.xhtml"] = []byte(`<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>` + title + `</title></head><body><nav epub:type="toc"><ol>` + nav.String() + `</ol></nav></body></html>`)
	files["OEBPS/content.opf"] = []byte(`<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book">urn:sha256:` + backup.SHA256 + `</dc:identifier><dc:title>` + title + `</dc:title><dc:creator>` + author + `</dc:creator><dc:language>` + language + `</dc:language><meta property="dcterms:modified">2000-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>` + manifest.String() + `</manifest><spine>` + spine.String() + `</spine></package>`)
	return zipFiles(files, "mimetype", map[string]bool{"mimetype": true})
}
