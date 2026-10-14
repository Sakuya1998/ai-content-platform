package governance

func closedObject(value any, fields ...string) (map[string]any, bool) {
	object, ok := value.(map[string]any)
	if !ok {
		return nil, false
	}
	allowed := map[string]bool{}
	for _, field := range fields {
		allowed[field] = true
	}
	for field := range object {
		if !allowed[field] {
			return nil, false
		}
	}
	return object, true
}
func modelContains(models []string, name string) bool {
	for _, model := range models {
		if model == name {
			return true
		}
	}
	return false
}

var ModelContains = modelContains
