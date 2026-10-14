package governance

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

func CanonicalJSON(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var obj any
	if err = dec.Decode(&obj); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	var encode func(any) error
	quote := func(s string) {
		out.WriteByte('"')
		for _, r := range s {
			switch r {
			case '"':
				out.WriteString(`\"`)
			case '\\':
				out.WriteString(`\\`)
			case '\b':
				out.WriteString(`\b`)
			case '\f':
				out.WriteString(`\f`)
			case '\n':
				out.WriteString(`\n`)
			case '\r':
				out.WriteString(`\r`)
			case '\t':
				out.WriteString(`\t`)
			default:
				if r < 32 {
					fmt.Fprintf(&out, `\u%04x`, r)
				} else {
					out.WriteRune(r)
				}
			}
		}
		out.WriteByte('"')
	}
	encode = func(v any) error {
		switch x := v.(type) {
		case nil:
			out.WriteString("null")
		case bool:
			out.WriteString(strconv.FormatBool(x))
		case string:
			quote(x)
		case json.Number:
			if !strings.ContainsAny(string(x), ".eE") {
				out.WriteString(string(x))
				break
			}
			f, err := x.Float64()
			if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
				return fmt.Errorf("invalid JSON number")
			}
			out.WriteString(pythonPromptFloat(f))
		case []any:
			out.WriteByte('[')
			for i, item := range x {
				if i > 0 {
					out.WriteByte(',')
				}
				if err := encode(item); err != nil {
					return err
				}
			}
			out.WriteByte(']')
		case map[string]any:
			keys := make([]string, 0, len(x))
			for key := range x {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			out.WriteByte('{')
			for i, key := range keys {
				if i > 0 {
					out.WriteByte(',')
				}
				quote(key)
				out.WriteByte(':')
				if err := encode(x[key]); err != nil {
					return err
				}
			}
			out.WriteByte('}')
		default:
			return fmt.Errorf("unsupported JSON value")
		}
		return nil
	}
	if err = encode(obj); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}
func pythonPromptFloat(value float64) string {
	text := strconv.FormatFloat(value, 'e', -1, 64)
	parts := strings.Split(text, "e")
	exponent, _ := strconv.Atoi(parts[1])
	if exponent >= -4 && exponent < 16 {
		text = strconv.FormatFloat(value, 'f', -1, 64)
		if !strings.Contains(text, ".") {
			text += ".0"
		}
		return text
	}
	return text
}
