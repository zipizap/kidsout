package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestDecideStatusOrder(t *testing.T) {
	noon := time.Date(2026, 7, 31, 12, 0, 0, 0, time.Local)
	day := &DayVars{TAMinutes: 60, TUMinutes: 0, TFStart: "09:00", TFEnd: "21:00"}
	ds := &DeviceState{EnforcementToggle: EnforcementON, PauseToggle: PauseOFF}

	cases := []struct {
		name  string
		setup func()
		up    string
		want  string
	}{
		{"up -> inUse", func() {}, "up", StatusInUse},
		{"down -> notInUse", func() {}, "down", StatusNotInUse},
		{"unknown -> notInUse", func() {}, "unknown", StatusNotInUse},
		{"pauseON -> blockedPauseON", func() {
			ds.PauseToggle = PauseON
			ds.PauseMinutesRemaining = 5
		}, "up", StatusBlockedPauseON},
		{"TR=0 wins over pause", func() {
			day.TUMinutes = 60
		}, "up", StatusBlockedNoTime},
		{"outside TF wins over TR", func() {
			day.TFStart, day.TFEnd = "13:00", "21:00"
		}, "up", StatusBlockedNotInTimeframe},
		{"enforcementOFF wins over all", func() {
			ds.EnforcementToggle = EnforcementOFF
		}, "up", StatusEnforcementOFF},
	}
	for _, c := range cases {
		c.setup()
		if got := decideStatus(ds, day, noon, c.up); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

func TestBlockedNotInTimeframeCancelsPause(t *testing.T) {
	store, err := LoadStateStore(filepath.Join(t.TempDir(), "rs.yaml"), []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	today := weekdayKeys[time.Now().Weekday()]
	store.With(func(st *Store) {
		ds := st.Devices["dev1"]
		ds.PauseToggle = PauseON
		ds.PauseMinutesRemaining = 15
		// timeframe that "now" can never be within -> blockedNotInTimeframe
		ds.Days[today].TFStart, ds.Days[today].TFEnd = "25:00", "25:01"
	})

	engine := NewEngine(t.TempDir(), store, func() {})
	engine.EvaluateAll()

	store.Read(func(st *Store) {
		ds := st.Devices["dev1"]
		if ds.DeviceStatus != StatusBlockedNotInTimeframe {
			t.Fatalf("deviceStatus: got %q, want %q", ds.DeviceStatus, StatusBlockedNotInTimeframe)
		}
		if ds.PauseToggle != PauseOFF {
			t.Errorf("pauseToggle: got %q, want %q", ds.PauseToggle, PauseOFF)
		}
		if ds.PauseMinutesRemaining != 0 {
			t.Errorf("pauseMinutesRemaining: got %d, want 0", ds.PauseMinutesRemaining)
		}
	})
}

func TestWithinTimeframeBoundaries(t *testing.T) {
	at := func(hhmm string) time.Time {
		p, _ := time.Parse("15:04", hhmm)
		return time.Date(2026, 7, 31, p.Hour(), p.Minute(), 0, 0, time.Local)
	}
	if !withinTimeframe(at("09:00"), "09:00", "21:00") {
		t.Error("TFstart should be inclusive")
	}
	if !withinTimeframe(at("21:00"), "09:00", "21:00") {
		t.Error("TFend should be inclusive")
	}
	if withinTimeframe(at("08:59"), "09:00", "21:00") || withinTimeframe(at("21:01"), "09:00", "21:00") {
		t.Error("outside TF must not match")
	}
}

func TestTRClamp(t *testing.T) {
	d := DayVars{TAMinutes: 30, TUMinutes: 45}
	if got := d.TRMinutes(); got != 0 {
		t.Errorf("TR must clamp to 0, got %d", got)
	}
}

func TestBasicAuth(t *testing.T) {
	store, err := LoadStateStore(filepath.Join(t.TempDir(), "rs.yaml"), []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{store: store, hub: NewSSEHub()}
	h := srv.basicAuth(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	check := func(user, pass string, withCreds bool, want int) {
		t.Helper()
		req := httptest.NewRequest("GET", "/", nil)
		if withCreds {
			req.SetBasicAuth(user, pass)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Errorf("auth(%q,%q): got %d, want %d", user, pass, rec.Code, want)
		}
	}
	check("", "", false, http.StatusUnauthorized)
	check("mae", "wrong", true, http.StatusUnauthorized)
	check("wrong", "pai", true, http.StatusUnauthorized)
	check("mae", "pai", true, http.StatusOK) // yaml defaults
}

func TestTAStepDelta(t *testing.T) {
	if taStepMinutes != 10 {
		t.Fatalf("taStepMinutes: got %d, want 10", taStepMinutes)
	}

	store, err := LoadStateStore(filepath.Join(t.TempDir(), "rs.yaml"), []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{store: store, hub: NewSSEHub()}
	srv.engine = NewEngine(t.TempDir(), store, srv.broadcastState)
	mux := http.NewServeMux()
	srv.Routes(mux, http.NotFoundHandler())

	today := weekdayKeys[time.Now().Weekday()]
	before := 0
	store.Read(func(st *Store) { before = st.Devices["dev1"].Days[today].TAMinutes })

	post := func(delta int) {
		t.Helper()
		body := strings.NewReader(`{"weekday":"` + today + `","deltaMinutes":` + strconv.Itoa(delta) + `}`)
		req := httptest.NewRequest("POST", "/api/device/dev1/ta", body)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusNoContent {
			t.Fatalf("ta post: got %d, want %d", rec.Code, http.StatusNoContent)
		}
	}

	post(taStepMinutes)
	after := 0
	store.Read(func(st *Store) { after = st.Devices["dev1"].Days[today].TAMinutes })
	if after != before+taStepMinutes {
		t.Errorf("TA after +step: got %d, want %d", after, before+taStepMinutes)
	}

	post(-taStepMinutes)
	store.Read(func(st *Store) { after = st.Devices["dev1"].Days[today].TAMinutes })
	if after != before {
		t.Errorf("TA after -step: got %d, want %d", after, before)
	}
}

// TestFreeUseModeDoesNotAccrueTU verifies that a device in free-use-mode
// (enforcementOFF) is never blocked and its time used (TU) does not increment,
// while an enforced device that is up does accrue TU.
func TestFreeUseModeDoesNotAccrueTU(t *testing.T) {
	devicesDir := t.TempDir()
	devDir := filepath.Join(devicesDir, "dev1")
	if err := os.MkdirAll(devDir, 0o755); err != nil {
		t.Fatal(err)
	}
	writeScript := func(name, body string) {
		if err := os.WriteFile(filepath.Join(devDir, name), []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	writeScript("getState.sh", "#!/usr/bin/env bash\necho up\n")
	writeScript("block.sh", "#!/usr/bin/env bash\nexit 0\n")
	writeScript("unblock.sh", "#!/usr/bin/env bash\nexit 0\n")

	store, err := LoadStateStore(filepath.Join(t.TempDir(), "rs.yaml"), []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	eng := NewEngine(devicesDir, store, func() {})
	today := weekdayKeys[time.Now().Weekday()]

	// full-day timeframe so the test does not depend on the wall-clock time
	store.With(func(st *Store) {
		d := st.Devices["dev1"].Days[today]
		d.TFStart, d.TFEnd = "00:00", "23:59"
	})

	read := func() (status string, tu int) {
		store.Read(func(st *Store) {
			status = st.Devices["dev1"].DeviceStatus
			tu = st.Devices["dev1"].Days[today].TUMinutes
		})
		return
	}

	// enforcementON + device up -> inUse -> TU accrues
	eng.EvaluateAll()
	if status, tu := read(); status != StatusInUse || tu != 1 {
		t.Fatalf("enforced+up: got status=%q tu=%d, want inUse tu=1", status, tu)
	}

	// switch to free-use-mode (enforcementOFF) -> status enforcementOFF, TU frozen
	store.With(func(st *Store) { st.Devices["dev1"].EnforcementToggle = EnforcementOFF })
	eng.EvaluateAll()
	if status, tu := read(); status != StatusEnforcementOFF || tu != 1 {
		t.Errorf("free-use-mode must not accrue TU: got status=%q tu=%d, want enforcementOFF tu=1", status, tu)
	}
}

// TestStateHistoryAppendAndCap verifies each tick appends the mapped getState.sh
// result (up=1, down=0, unknown=-1) and that the history is capped at maxStateHistory.
func TestStateHistoryAppendAndCap(t *testing.T) {
	devicesDir := t.TempDir()
	devDir := filepath.Join(devicesDir, "dev1")
	if err := os.MkdirAll(devDir, 0o755); err != nil {
		t.Fatal(err)
	}
	scriptPath := filepath.Join(devDir, "getState.sh")
	writeScript := func(body string) {
		if err := os.WriteFile(scriptPath, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}

	store, err := LoadStateStore(filepath.Join(t.TempDir(), "rs.yaml"), []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	eng := NewEngine(devicesDir, store, func() {})
	history := func() []int {
		var h []int
		store.Read(func(st *Store) { h = st.Devices["dev1"].StateHistory })
		return h
	}

	writeScript("#!/usr/bin/env bash\necho up\n")
	eng.EvaluateAll()
	if got := history(); len(got) != 1 || got[0] != 1 {
		t.Fatalf("after up tick: got %v, want [1]", got)
	}

	writeScript("#!/usr/bin/env bash\necho down\n")
	eng.EvaluateAll()
	if got := history(); len(got) != 2 || got[1] != 0 {
		t.Fatalf("after down tick: got %v, want [1 0]", got)
	}

	writeScript("#!/usr/bin/env bash\necho garbage\n")
	eng.EvaluateAll()
	if got := history(); len(got) != 3 || got[2] != -1 {
		t.Fatalf("after unknown tick: got %v, want [1 0 -1]", got)
	}

	for i := 0; i < maxStateHistory+5; i++ {
		eng.EvaluateAll()
	}
	if got := history(); len(got) != maxStateHistory {
		t.Fatalf("history len: got %d, want %d (capped)", len(got), maxStateHistory)
	}
}

// TestStateHistoryNotPersistedButInAPI verifies stateHistory is excluded from
// runtimestore.yaml (in-memory only) but included in the API JSON snapshot.
func TestStateHistoryNotPersistedButInAPI(t *testing.T) {
	rsPath := filepath.Join(t.TempDir(), "rs.yaml")
	store, err := LoadStateStore(rsPath, []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	store.With(func(st *Store) {
		st.Devices["dev1"].appendStateHistory(1)
		st.Devices["dev1"].appendStateHistory(0)
	})

	raw, err := os.ReadFile(rsPath)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "stateHistory") || strings.Contains(string(raw), "StateHistory") {
		t.Errorf("runtimestore.yaml must not contain history: %s", raw)
	}

	b := snapshotJSON(store)
	if !strings.Contains(string(b), `"stateHistory":[1,0]`) {
		t.Errorf("API JSON must contain stateHistory: %s", b)
	}
}

// TestUnpauseKeepsInUseWhenDeviceUp verifies that unpausing an up device does not
// briefly flash notInUse. While paused the device is physically blocked, so
// getState.sh reports "down"; the recompute must reuse the last reliable "up"
// reading instead of trusting that blocked reading.
func TestUnpauseKeepsInUseWhenDeviceUp(t *testing.T) {
	devicesDir := t.TempDir()
	devDir := filepath.Join(devicesDir, "dev1")
	if err := os.MkdirAll(devDir, 0o755); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(devDir, "blocked")
	writeScript := func(name, body string) {
		if err := os.WriteFile(filepath.Join(devDir, name), []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	// getState.sh mimics reality: a blocked device reports "down", else "up".
	writeScript("getState.sh", "#!/usr/bin/env bash\n[ -f '"+marker+"' ] && echo down || echo up\n")
	writeScript("block.sh", "#!/usr/bin/env bash\ntouch '"+marker+"'\n")
	writeScript("unblock.sh", "#!/usr/bin/env bash\nrm -f '"+marker+"'\n")

	store, err := LoadStateStore(filepath.Join(t.TempDir(), "rs.yaml"), []string{"dev1"})
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{store: store, hub: NewSSEHub()}
	srv.engine = NewEngine(devicesDir, store, srv.broadcastState)
	mux := http.NewServeMux()
	srv.Routes(mux, http.NotFoundHandler())

	today := weekdayKeys[time.Now().Weekday()]
	store.With(func(st *Store) {
		d := st.Devices["dev1"].Days[today]
		d.TFStart, d.TFEnd = "00:00", "23:59"
	})

	status := func() (s string) {
		store.Read(func(st *Store) { s = st.Devices["dev1"].DeviceStatus })
		return
	}
	postPause := func(toggle string) {
		t.Helper()
		body := strings.NewReader(`{"toggle":"` + toggle + `"}`)
		req := httptest.NewRequest("POST", "/api/device/dev1/pause", body)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusNoContent {
			t.Fatalf("pause %s: got %d, want %d", toggle, rec.Code, http.StatusNoContent)
		}
	}

	// device up -> inUse
	srv.engine.EvaluateAll()
	if s := status(); s != StatusInUse {
		t.Fatalf("initial: got %q, want inUse", s)
	}

	postPause(PauseON)
	if s := status(); s != StatusBlockedPauseON {
		t.Fatalf("after pauseON: got %q, want blockedPauseON", s)
	}

	postPause(PauseOFF)
	if s := status(); s != StatusInUse {
		t.Fatalf("after pauseOFF: got %q, want inUse (must not flash notInUse)", s)
	}
}
