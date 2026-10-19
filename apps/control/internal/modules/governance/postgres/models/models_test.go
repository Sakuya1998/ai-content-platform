package models

import (
	"encoding/json"
	"testing"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

func TestDecodeModelProfileWriteRejectsUnknownFields(t *testing.T) {
	var input governance.ModelProfileWrite
	if err := decodeModelProfileWrite(json.RawMessage(`{"name":"test","provider":"openai","extra":true}`), &input); err == nil {
		t.Fatal("unknown model profile fields must be rejected")
	}
}

func TestDecodeModelProfileWriteRejectsTrailingJSON(t *testing.T) {
	var input governance.ModelProfileWrite
	if err := decodeModelProfileWrite(json.RawMessage(`{"name":"test","provider":"openai"} {}`), &input); err == nil {
		t.Fatal("trailing JSON values must be rejected")
	}
}

func TestDecodeModelProfileWriteAcceptsOneObject(t *testing.T) {
	var input governance.ModelProfileWrite
	if err := decodeModelProfileWrite(json.RawMessage(`{"name":"test","provider":"openai"}`), &input); err != nil {
		t.Fatalf("valid model profile body rejected: %v", err)
	}
	if input.Name != "test" || input.Provider != "openai" {
		t.Fatalf("decoded unexpected model profile: %#v", input)
	}
}
