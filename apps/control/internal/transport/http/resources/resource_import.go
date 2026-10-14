package resources

import (
	"bytes"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"strings"
)

func readResourceImport(w http.ResponseWriter, r *http.Request) (ResourceBackup, error) {
	if r.ContentLength > maxResourceImportBytes {
		return ResourceBackup{}, errResourceInvalid
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxResourceImportBytes)
	contentType, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	var body []byte
	format := r.URL.Query().Get("format")
	password := r.Header.Get("X-Backup-Password")
	filename := "imported.txt"
	if contentType == "multipart/form-data" {
		reader, err := r.MultipartReader()
		if err != nil {
			return ResourceBackup{}, errResourceInvalid
		}
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				return ResourceBackup{}, errResourceInvalid
			}
			data, err := io.ReadAll(io.LimitReader(part, maxResourceImportBytes+1))
			if err != nil || int64(len(data)) > maxResourceImportBytes {
				return ResourceBackup{}, errResourceInvalid
			}
			if part.FileName() != "" {
				if body != nil {
					return ResourceBackup{}, errResourceInvalid
				}
				body = data
				filename = part.FileName()
			} else {
				if len(data) > 2000 {
					return ResourceBackup{}, errResourceInvalid
				}
				switch part.FormName() {
				case "password":
					password = string(data)
				case "format":
					format = string(data)
				case "title":
					filename = string(data) + ".txt"
				}
			}
			_ = part.Close()
		}
	} else {
		var err error
		body, err = io.ReadAll(r.Body)
		if err != nil {
			return ResourceBackup{}, errResourceInvalid
		}
	}
	if len(body) == 0 || len(password) > 200 {
		return ResourceBackup{}, errResourceInvalid
	}
	decrypted, err := decryptResourceBackup(body, password)
	if err != nil {
		return ResourceBackup{}, err
	}
	body = decrypted
	if bytes.HasPrefix(body, []byte("PK")) {
		if format == "docx" || strings.HasSuffix(strings.ToLower(filename), ".docx") {
			return decodeResourcePublication(body, "docx", filename)
		}
		if format == "epub" || strings.HasSuffix(strings.ToLower(filename), ".epub") {
			return decodeResourcePublication(body, "epub", filename)
		}
		return decodeResourceBackupZIP(body)
	}
	if format == "txt" || format == "markdown" || format == "md" || contentType == "text/plain" || contentType == "text/markdown" {
		return resourceImportText(string(body), filename), nil
	}
	var raw json.RawMessage
	if json.Unmarshal(body, &raw) != nil {
		return ResourceBackup{}, errResourceInvalid
	}
	return decodeResourceBackup(raw)
}
