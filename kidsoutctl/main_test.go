package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func testState() State {
	day := func(ta, tu int) Day {
		return Day{TAMinutes: ta, TUMinutes: tu, TRMinutes: max(0, ta-tu), TFStart: "09:00", TFEnd: "21:00"}
	}
	days := map[string]Day{}
	for _, d := range weekOrder {
		days[d] = day(120, 40)
	}
	return State{
		Today: "fri",
		Devices: map[string]Device{
			"xbox":   {DeviceStatus: "inUse", EnforcementToggle: "enforcementON", PauseToggle: "pauseOFF", Days: days},
			"tv":     {DeviceStatus: "blockedNoTime", EnforcementToggle: "enforcementON", PauseToggle: "pauseOFF", Days: days},
			"tablet": {DeviceStatus: "blockedPauseON", EnforcementToggle: "enforcementON", PauseToggle: "pauseON", PauseMinutesRemaining: 12, Days: days},
		},
	}
}

// newTestServer records mutation posts and serves the canned state.
func newTestServer(t *testing.T, posts *[]string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, pass, ok := r.BasicAuth()
		if !ok || user != "u" || pass != "p" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch {
		case r.Method == "GET" && r.URL.Path == "/api/state":
			json.NewEncoder(w).Encode(testState())
		case r.Method == "POST":
			body, _ := readAllString(r)
			*posts = append(*posts, r.URL.Path+" "+body)
			w.WriteHeader(http.StatusNoContent)
		default:
			http.NotFound(w, r)
		}
	}))
}

func readAllString(r *http.Request) (string, error) {
	var b bytes.Buffer
	_, err := b.ReadFrom(r.Body)
	return strings.TrimSpace(b.String()), err
}

func runCLI(t *testing.T, server string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	t.Setenv("KOAUTH", "u:p")
	t.Setenv("KIDSOUT_AUTH", "")
	t.Setenv("NO_COLOR", "1")
	var out, errBuf bytes.Buffer
	full := append([]string{"--server", server}, args...)
	code = run(full, &out, &errBuf)
	return code, out.String(), errBuf.String()
}

func TestGetTable(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, out, _ := runCLI(t, srv.URL, "get")
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	for _, want := range []string{"today: fri", "DEVICE", "xbox", "inUse", "tv", "blockedNoTime", "tablet", "on (12m left)", "2h00m", "09:00-21:00"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
}

func TestGetSingleDeviceAndUnknown(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, out, _ := runCLI(t, srv.URL, "get", "xbox")
	if code != 0 || !strings.Contains(out, "xbox") || strings.Contains(out, "tablet") {
		t.Errorf("single-device get wrong (code=%d):\n%s", code, out)
	}
	code, _, errOut := runCLI(t, srv.URL, "get", "nosuch")
	if code != 1 || !strings.Contains(errOut, "unknown device") {
		t.Errorf("unknown device: code=%d stderr=%q", code, errOut)
	}
}

func TestGetJSONAndYAML(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, out, _ := runCLI(t, srv.URL, "get", "-o", "json")
	if code != 0 {
		t.Fatalf("exit code = %d", code)
	}
	var s State
	if err := json.Unmarshal([]byte(out), &s); err != nil || s.Today != "fri" {
		t.Errorf("json output not decodable: %v\n%s", err, out)
	}

	code, out, _ = runCLI(t, srv.URL, "get", "-o", "yaml")
	if code != 0 || !strings.Contains(out, "today: fri") {
		t.Errorf("yaml output wrong (code=%d):\n%s", code, out)
	}
}

func TestGetWeek(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, out, _ := runCLI(t, srv.URL, "get", "--week", "xbox")
	if code != 0 {
		t.Fatalf("exit code = %d", code)
	}
	for _, d := range weekOrder {
		if !strings.Contains(out, d) {
			t.Errorf("week view missing %q:\n%s", d, out)
		}
	}
}

func TestPauseUnpauseEnforceUnenforce(t *testing.T) {
	cases := []struct {
		cmd      string
		wantPost string
	}{
		{"pause", `/api/device/xbox/pause {"toggle":"pauseON"}`},
		{"unpause", `/api/device/xbox/pause {"toggle":"pauseOFF"}`},
		{"resume", `/api/device/xbox/pause {"toggle":"pauseOFF"}`},
		{"enforce", `/api/device/xbox/enforcement {"toggle":"enforcementON"}`},
		{"unenforce", `/api/device/xbox/enforcement {"toggle":"enforcementOFF"}`},
	}
	for _, tc := range cases {
		t.Run(tc.cmd, func(t *testing.T) {
			var posts []string
			srv := newTestServer(t, &posts)
			defer srv.Close()
			code, _, errOut := runCLI(t, srv.URL, tc.cmd, "xbox")
			if code != 0 {
				t.Fatalf("exit code = %d, stderr=%q", code, errOut)
			}
			if len(posts) != 1 || posts[0] != tc.wantPost {
				t.Errorf("posts = %v, want [%s]", posts, tc.wantPost)
			}
		})
	}
}

func TestPauseAll(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, _, _ := runCLI(t, srv.URL, "pause", "--all")
	if code != 0 {
		t.Fatalf("exit code = %d", code)
	}
	if len(posts) != 3 {
		t.Errorf("expected 3 pause posts, got %v", posts)
	}
	code, _, errOut := runCLI(t, srv.URL, "pause", "--all", "xbox")
	if code != 2 || !strings.Contains(errOut, "not both") {
		t.Errorf("--all with names: code=%d stderr=%q", code, errOut)
	}
}

func TestTA(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, _, _ := runCLI(t, srv.URL, "ta", "xbox", "fri", "-15")
	if code != 0 {
		t.Fatalf("exit code = %d", code)
	}
	want := `/api/device/xbox/ta {"deltaMinutes":-15,"weekday":"fri"}`
	if len(posts) != 1 || posts[0] != want {
		t.Errorf("posts = %v, want [%s]", posts, want)
	}

	code, _, _ = runCLI(t, srv.URL, "ta", "xbox", "today", "30")
	if code != 0 {
		t.Fatalf("ta today: exit code = %d", code)
	}
	want = `/api/device/xbox/ta {"deltaMinutes":30,"weekday":"fri"}`
	if posts[len(posts)-1] != want {
		t.Errorf("last post = %q, want %q", posts[len(posts)-1], want)
	}

	code, _, errOut := runCLI(t, srv.URL, "ta", "xbox", "someday", "30")
	if code != 2 || !strings.Contains(errOut, "invalid weekday") {
		t.Errorf("bad weekday: code=%d stderr=%q", code, errOut)
	}
	code, _, errOut = runCLI(t, srv.URL, "ta", "xbox", "fri", "lots")
	if code != 2 || !strings.Contains(errOut, "invalid minutes") {
		t.Errorf("bad delta: code=%d stderr=%q", code, errOut)
	}
}

func TestTF(t *testing.T) {
	var posts []string
	srv := newTestServer(t, &posts)
	defer srv.Close()

	code, _, _ := runCLI(t, srv.URL, "tf", "tablet", "sat", "10:00", "22:00")
	if code != 0 {
		t.Fatalf("exit code = %d", code)
	}
	want := `/api/device/tablet/tf {"tfEnd":"22:00","tfStart":"10:00","weekday":"sat"}`
	if len(posts) != 1 || posts[0] != want {
		t.Errorf("posts = %v, want [%s]", posts, want)
	}

	for _, bad := range [][]string{
		{"tf", "tablet", "sat", "25:00", "22:00"},
		{"tf", "tablet", "sat", "9:00", "22:00"},
		{"tf", "tablet", "sat", "22:00", "10:00"},
	} {
		code, _, _ := runCLI(t, srv.URL, bad...)
		if code != 2 {
			t.Errorf("%v: exit code = %d, want 2", bad, code)
		}
	}
}

func TestExitCodes(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/state" {
			json.NewEncoder(w).Encode(testState())
			return
		}
		http.Error(w, "pause has no effect", http.StatusConflict)
	}))
	defer srv.Close()

	code, _, _ := runCLI(t, srv.URL, "unknowncmd")
	if code != 2 {
		t.Errorf("unknown command: code = %d, want 2", code)
	}

	// 409 conflict on a single-target mutation surfaces exit code 1 (batch)
	// but the underlying ta path returns 4 for direct APIError.
	code, _, errOut := runCLI(t, srv.URL, "ta", "xbox", "fri", "30")
	if code != 4 || !strings.Contains(errOut, "409") {
		t.Errorf("conflict: code=%d stderr=%q", code, errOut)
	}

	auth := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
	}))
	defer auth.Close()
	code, _, _ = runCLI(t, auth.URL, "get")
	if code != 3 {
		t.Errorf("401: code = %d, want 3", code)
	}
}

func TestMissingAuthEnv(t *testing.T) {
	t.Setenv("KOAUTH", "")
	t.Setenv("KIDSOUT_AUTH", "")
	var out, errBuf bytes.Buffer
	code := run([]string{"get"}, &out, &errBuf)
	if code != 3 || !strings.Contains(errBuf.String(), "KOAUTH") {
		t.Errorf("missing auth: code=%d stderr=%q", code, errBuf.String())
	}
}

func TestSplitFlagsInterspersed(t *testing.T) {
	fs := flag.NewFlagSet("t", flag.ContinueOnError)
	o := fs.String("o", "table", "")
	all := fs.Bool("all", false, "")
	pos, err := splitFlags(fs, []string{"ta", "xbox", "-o", "json", "fri", "-15", "--all"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"ta", "xbox", "fri", "-15"}
	if fmt.Sprint(pos) != fmt.Sprint(want) {
		t.Errorf("pos = %v, want %v", pos, want)
	}
	if *o != "json" || !*all {
		t.Errorf("flags not parsed: o=%q all=%v", *o, *all)
	}
}

func TestVersionAndHelp(t *testing.T) {
	var out, errBuf bytes.Buffer
	if code := run([]string{"version"}, &out, &errBuf); code != 0 || !strings.Contains(out.String(), "kidsoutctl") {
		t.Errorf("version: code=%d out=%q", code, out.String())
	}
	out.Reset()
	if code := run([]string{"help"}, &out, &errBuf); code != 0 || !strings.Contains(out.String(), "Usage:") {
		t.Errorf("help: code=%d", code)
	}
	out.Reset()
	if code := run([]string{"completion", "bash"}, &out, &errBuf); code != 0 || !strings.Contains(out.String(), "complete -F") {
		t.Errorf("completion: code=%d", code)
	}
}

func TestFmtMinutes(t *testing.T) {
	cases := map[int]string{0: "0m", 45: "45m", 60: "1h00m", 125: "2h05m"}
	for in, want := range cases {
		if got := fmtMinutes(in); got != want {
			t.Errorf("fmtMinutes(%d) = %q, want %q", in, got, want)
		}
	}
}

func TestWatchOnce(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/events" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		raw, _ := json.Marshal(testState())
		fmt.Fprintf(w, "data: %s\n\n", raw)
		w.(http.Flusher).Flush()
		// keep the stream open; client should exit after first event (--once)
	}))
	defer srv.Close()

	code, out, errOut := runCLI(t, srv.URL, "watch", "--once")
	if code != 0 {
		t.Fatalf("watch --once: code=%d stderr=%q", code, errOut)
	}
	if !strings.Contains(out, "xbox") {
		t.Errorf("watch output missing device:\n%s", out)
	}
}
