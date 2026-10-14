// Package objectstorage defines object keys for the shared artifact bucket.
package objectstorage

import (
	"errors"
	"regexp"
)

var safeSegment = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

func ObjectKey(workspaceID, projectID, category, resourceID, filename, versionID string) (string, error) {
	versioned := category == "documents" || category == "assets"
	if !versioned && category != "uploads" && category != "exports" {
		return "", errors.New("unsupported object category")
	}
	if versioned && versionID == "" {
		return "", errors.New("documents and assets require a version ID")
	}
	if !versioned && versionID != "" {
		return "", errors.New("uploads and exports do not accept a version ID")
	}
	segments := []string{workspaceID, projectID, resourceID, filename}
	if versioned {
		segments = append(segments, versionID)
	}
	for _, value := range segments {
		if !safeSegment.MatchString(value) {
			return "", errors.New("object key segments must be safe IDs or filenames")
		}
	}
	key := "workspaces/" + workspaceID + "/projects/" + projectID + "/" + category + "/" + resourceID
	if versioned {
		key += "/versions/" + versionID
	}
	return key + "/" + filename, nil
}
