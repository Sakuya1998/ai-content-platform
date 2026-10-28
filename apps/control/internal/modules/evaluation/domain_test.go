package evaluation

import "testing"

func validPolicy() EvaluationPolicyVersion {
	return EvaluationPolicyVersion{
		PolicyID:          "content-quality",
		Version:           1,
		EngineVersion:     "evaluation@1",
		GateThreshold:     75,
		CriticalThreshold: 60,
		Status:            PolicyStatusDraft,
		Rules: []EvaluationRule{{
			Key:        "text.length@1",
			Dimension:  "length",
			Weight:     1,
			Parameters: map[string]any{"scale": 100},
		}},
	}
}

func TestEvaluationPolicyVersionValidateAcceptsValidDraft(t *testing.T) {
	if err := validPolicy().Validate(); err != nil {
		t.Fatalf("valid policy rejected: %v", err)
	}
}

func TestEvaluationPolicyVersionValidateRejectsInvalidThresholdsAndDuplicateDimensions(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*EvaluationPolicyVersion)
	}{
		{name: "threshold outside score range", mutate: func(policy *EvaluationPolicyVersion) { policy.GateThreshold = 101 }},
		{name: "duplicate dimension", mutate: func(policy *EvaluationPolicyVersion) {
			policy.Rules = append(policy.Rules, EvaluationRule{Key: "text.structure@1", Dimension: "length", Weight: 1})
		}},
		{name: "non-positive rule weight", mutate: func(policy *EvaluationPolicyVersion) { policy.Rules[0].Weight = 0 }},
		{name: "too many rules", mutate: func(policy *EvaluationPolicyVersion) {
			policy.Rules = make([]EvaluationRule, 33)
			for index := range policy.Rules {
				policy.Rules[index] = EvaluationRule{Key: "check", Dimension: "dimension", Weight: 1}
			}
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			policy := validPolicy()
			test.mutate(&policy)
			if err := policy.Validate(); err == nil {
				t.Fatal("invalid policy was accepted")
			}
		})
	}
}
