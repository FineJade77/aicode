package resumecmd

import (
	"reflect"
	"testing"
)

func TestParseResumeArgs(t *testing.T) {
	tests := []struct {
		name    string
		args    []string
		want    resumeTarget
		wantErr bool
	}{
		{name: "inspect last", args: []string{"--last"}, want: resumeTarget{UseLast: true, Inspect: true}},
		{name: "resume last with message", args: []string{"--last", "继续", "任务"}, want: resumeTarget{UseLast: true, Message: "继续 任务"}},
		{name: "inspect session", args: []string{"sess_1"}, want: resumeTarget{SessionID: "sess_1", Inspect: true}},
		{name: "resume session with message", args: []string{"sess_1", "继续", "任务"}, want: resumeTarget{SessionID: "sess_1", Message: "继续 任务"}},
		{name: "missing", args: nil, wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := parseResumeArgs(test.args)
			if test.wantErr {
				if err == nil {
					t.Fatal("expected error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("parseResumeArgs() = %#v, want %#v", got, test.want)
			}
		})
	}
}

func TestSessionInfoFromValue(t *testing.T) {
	info, err := sessionInfoFromValue(map[string]any{
		"session_id": "sess_1",
		"workspace":  "/repo",
		"language":   "en-US",
	})
	if err != nil {
		t.Fatal(err)
	}
	want := sessionInfo{SessionID: "sess_1", Workspace: "/repo", Language: "en-US"}
	if info != want {
		t.Fatalf("sessionInfoFromValue() = %#v, want %#v", info, want)
	}
}

func TestSessionInfoFromValueDefaultsLanguage(t *testing.T) {
	info, err := sessionInfoFromValue(map[string]any{
		"session_id": "sess_1",
		"workspace":  "/repo",
	})
	if err != nil {
		t.Fatal(err)
	}
	if info.Language != "zh-CN" {
		t.Fatalf("language = %q", info.Language)
	}
}
