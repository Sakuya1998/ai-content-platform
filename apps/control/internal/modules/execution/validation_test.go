package execution

import (
	"encoding/json"
	"testing"
)

func TestVideoRequestDecodedFromPublicTaskIsValid(t *testing.T) {
	var request Request
	if err := json.Unmarshal([]byte(`{"project_id":"ad49b768-0404-4dac-b770-a166a9b98980","document_type":"video","title":"Video acceptance","objective":"一个人物走进车站","arguments":{"video":{"duration":5,"ratio":"16:9","resolution":"720p"}}}`), &request); err != nil {
		t.Fatal(err)
	}
	if !ValidGeneration(request) {
		t.Fatal("valid public video request rejected")
	}
}
