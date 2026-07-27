package renderer

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

type rendererSSEFixture struct {
	ContractVersion    string           `json:"contract_version"`
	Events             []map[string]any `json:"events"`
	ForwardCompatEvent map[string]any   `json:"forward_compat_event"`
}

func TestRendererHandlesEveryV2FixtureEvent(t *testing.T) {
	fixture := loadRendererSSEFixture(t)
	for _, event := range fixture.Events {
		eventType, _ := event["type"].(string)
		t.Run(eventType, func(t *testing.T) {
			output := captureRenderEvent(event)
			if output == "" {
				t.Fatalf("known event %q rendered no output", eventType)
			}
			if strings.Contains(output, `"type":`) {
				t.Fatalf("known event %q fell through to raw JSON: %s", eventType, output)
			}
		})
	}
}

func TestRendererUsesCurrentTokenBudgetFields(t *testing.T) {
	fixture := loadRendererSSEFixture(t)
	event := fixtureEvent(t, fixture.Events, "context.budget")
	output := captureRenderEvent(event)

	assertContains(t, output, "Context budget: history 100 -> 50 tokens")
}

func TestRendererIgnoresUnknownFieldsOnKnownEvents(t *testing.T) {
	fixture := loadRendererSSEFixture(t)
	event := fixtureEvent(t, fixture.Events, "tool.output")
	output := captureRenderEvent(event)

	if strings.Contains(output, "future_field") {
		t.Fatalf("known event rendered an unknown field: %s", output)
	}
}

func TestRendererShowsUnknownEventAsJSON(t *testing.T) {
	fixture := loadRendererSSEFixture(t)
	output := captureRenderEvent(fixture.ForwardCompatEvent)

	assertContains(t, output, `"type": "future.progress"`)
	assertContains(t, output, `"future_field"`)
}

func loadRendererSSEFixture(t *testing.T) rendererSSEFixture {
	t.Helper()
	_, currentFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot resolve renderer fixture path")
	}
	path := filepath.Join(filepath.Dir(currentFile), "..", "..", "..", "schemas", "fixtures", "sse-events.v2.json")
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture rendererSSEFixture
	if err := json.Unmarshal(content, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.ContractVersion != "2.0" {
		t.Fatalf("contract version = %q", fixture.ContractVersion)
	}
	return fixture
}

func fixtureEvent(t *testing.T, events []map[string]any, eventType string) map[string]any {
	t.Helper()
	for _, event := range events {
		if event["type"] == eventType {
			return event
		}
	}
	t.Fatalf("fixture event %q not found", eventType)
	return nil
}
