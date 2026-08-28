package main

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"
)

const authCookieName = "kidsout_session"

// newAuthToken returns a random session token used for the post-login cookie.
func newAuthToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		log.Fatalf("generating auth token: %v", err)
	}
	return hex.EncodeToString(b)
}

// ---- SSE hub ----

type SSEHub struct {
	mu      sync.Mutex
	clients map[chan []byte]struct{}
}

func NewSSEHub() *SSEHub {
	return &SSEHub{clients: map[chan []byte]struct{}{}}
}

func (h *SSEHub) Broadcast(payload []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for ch := range h.clients {
		select {
		case ch <- payload:
		default: // drop for slow clients; next event will catch them up
		}
	}
}

func (h *SSEHub) ServeHTTP(w http.ResponseWriter, r *http.Request, first []byte) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ch := make(chan []byte, 8)
	h.mu.Lock()
	h.clients[ch] = struct{}{}
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		delete(h.clients, ch)
		h.mu.Unlock()
	}()

	writeEvent := func(b []byte) bool {
		if _, err := fmt.Fprintf(w, "data: %s\n\n", b); err != nil {
			return false
		}
		fl.Flush()
		return true
	}
	if !writeEvent(first) {
		return
	}
	keepAlive := time.NewTicker(25 * time.Second)
	defer keepAlive.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case b := <-ch:
			if !writeEvent(b) {
				return
			}
		case <-keepAlive.C:
			if _, err := fmt.Fprint(w, ": keepalive\n\n"); err != nil {
				return
			}
			fl.Flush()
		}
	}
}

// ---- API state shape (what the webpage consumes) ----

type apiDay struct {
	TAMinutes int    `json:"taMinutes"`
	TUMinutes int    `json:"tuMinutes"`
	TRMinutes int    `json:"trMinutes"`
	TFStart   string `json:"tfStart"`
	TFEnd     string `json:"tfEnd"`
}

type apiDevice struct {
	DeviceStatus          string            `json:"deviceStatus"`
	EnforcementToggle     string            `json:"enforcementToggle"`
	PauseToggle           string            `json:"pauseToggle"`
	PauseMinutesRemaining int               `json:"pauseMinutesRemaining"`
	Days                  map[string]apiDay `json:"days"`
	StateHistory          []int             `json:"stateHistory"`
}

type apiState struct {
	Today   string               `json:"today"` // "fri"
	Devices map[string]apiDevice `json:"devices"`
}

func snapshotJSON(store *StateStore) []byte {
	out := apiState{
		Today:   weekdayKeys[time.Now().Weekday()],
		Devices: map[string]apiDevice{},
	}
	store.Read(func(st *Store) {
		for name, ds := range st.Devices {
			ad := apiDevice{
				DeviceStatus:          ds.DeviceStatus,
				EnforcementToggle:     ds.EnforcementToggle,
				PauseToggle:           ds.PauseToggle,
				PauseMinutesRemaining: ds.PauseMinutesRemaining,
				Days:                  map[string]apiDay{},
				StateHistory:          ds.StateHistory,
			}
			for wd, d := range ds.Days {
				ad.Days[wd] = apiDay{
					TAMinutes: d.TAMinutes, TUMinutes: d.TUMinutes, TRMinutes: d.TRMinutes(),
					TFStart: d.TFStart, TFEnd: d.TFEnd,
				}
			}
			out.Devices[name] = ad
		}
	})
	b, _ := json.Marshal(out)
	return b
}

// ---- HTTP server ----

type Server struct {
	store     *StateStore
	engine    *Engine
	hub       *SSEHub
	authToken string // per-run session token handed out as a cookie after Basic Auth
}

func (s *Server) broadcastState() {
	s.hub.Broadcast(snapshotJSON(s.store))
}

// basicAuth protects all pages/APIs; credentials live in runtimestore.yaml
// and are re-read per request so the admin can change them without restart.
//
// After a successful Basic Auth it also sets an HttpOnly session cookie and
// accepts that cookie on subsequent requests. This is what makes the SSE
// EventSource connection work: EventSource cannot forward Basic Auth
// credentials reliably, so without the cookie its /api/events request would
// keep returning 401 and browsers would re-pop the login modal in a loop.
func (s *Server) basicAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if c, err := r.Cookie(authCookieName); err == nil &&
			subtle.ConstantTimeCompare([]byte(c.Value), []byte(s.authToken)) == 1 {
			next.ServeHTTP(w, r)
			return
		}
		var wantUser, wantPass string
		s.store.Read(func(st *Store) { wantUser, wantPass = st.Auth.Username, st.Auth.Password })
		user, pass, ok := r.BasicAuth()
		if !ok ||
			subtle.ConstantTimeCompare([]byte(user), []byte(wantUser)) != 1 ||
			subtle.ConstantTimeCompare([]byte(pass), []byte(wantPass)) != 1 {
			w.Header().Set("WWW-Authenticate", `Basic realm="kidsout", charset="UTF-8"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		http.SetCookie(w, &http.Cookie{
			Name:     authCookieName,
			Value:    s.authToken,
			Path:     "/",
			HttpOnly: true,
			SameSite: http.SameSiteLaxMode,
		})
		next.ServeHTTP(w, r)
	})
}

// afterMutation: re-evaluate immediately so scripts/status react without waiting the 1-min tick.
// EvaluateAll already broadcasts; but it also increments TU/pause counters — so instead
// re-decide status only, run block/unblock edges, then broadcast.
func (s *Server) recomputeStatusNow() {
	now := time.Now()
	today := weekdayKeys[now.Weekday()]

	names := []string{}
	s.store.Read(func(st *Store) {
		for n := range st.Devices {
			names = append(names, n)
		}
	})
	upStates := map[string]string{}
	for _, n := range names {
		upStates[n] = s.engine.getState(n)
	}

	type action struct{ device, script string }
	var actions []action
	err := s.store.With(func(st *Store) {
		for name, ds := range st.Devices {
			day := ds.Days[today]
			prev := ds.DeviceStatus
			// getState.sh is unreliable while a device is blocked (it reports
			// "down"). When lifting a block, reuse the last reliable reading so the
			// status doesn't briefly flash notInUse before the next tick corrects it.
			up := upStates[name]
			if hasBlockedPrefix(prev) {
				up = ds.LastUpState
			} else {
				ds.LastUpState = up
			}
			ds.DeviceStatus = decideStatus(ds, day, now, up)
			if hasBlockedPrefix(ds.DeviceStatus) {
				if upStates[name] == "up" {
					actions = append(actions, action{name, "block.sh"})
				}
			} else if hasBlockedPrefix(prev) {
				actions = append(actions, action{name, "unblock.sh"})
			}
		}
	})
	if err != nil {
		log.Printf("api: save failed: %v", err)
	}
	for _, a := range actions {
		if _, err := s.engine.runScript(a.device, a.script); err != nil {
			log.Printf("api: %s/%s failed: %v", a.device, a.script, err)
		}
	}
	s.broadcastState()
}

func hasBlockedPrefix(status string) bool {
	return len(status) >= 7 && status[:7] == "blocked"
}

func validHHMM(v string) bool {
	_, err := time.Parse("15:04", v)
	return err == nil
}

func (s *Server) deviceExists(name string) bool {
	found := false
	s.store.Read(func(st *Store) { _, found = st.Devices[name] })
	return found
}

func (s *Server) Routes(mux *http.ServeMux, webFS http.Handler) {
	mux.Handle("GET /", webFS)

	mux.HandleFunc("GET /api/state", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write(snapshotJSON(s.store))
	})

	mux.HandleFunc("GET /api/events", func(w http.ResponseWriter, r *http.Request) {
		s.hub.ServeHTTP(w, r, snapshotJSON(s.store))
	})

	// POST /api/device/{name}/ta  {"weekday":"fri","deltaMinutes":10}
	mux.HandleFunc("POST /api/device/{name}/ta", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		var req struct {
			Weekday      string `json:"weekday"`
			DeltaMinutes int    `json:"deltaMinutes"`
		}
		if !s.deviceExists(name) || json.NewDecoder(r.Body).Decode(&req) != nil || !validWeekday(req.Weekday) {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		s.store.With(func(st *Store) {
			d := st.Devices[name].Days[req.Weekday]
			d.TAMinutes += req.DeltaMinutes
			if d.TAMinutes < 0 {
				d.TAMinutes = 0
			}
			if d.TAMinutes > taMaxMinutes {
				d.TAMinutes = taMaxMinutes
			}
		})
		s.recomputeStatusNow()
		w.WriteHeader(http.StatusNoContent)
	})

	// POST /api/device/{name}/tf  {"weekday":"fri","tfStart":"09:00","tfEnd":"21:00"}
	mux.HandleFunc("POST /api/device/{name}/tf", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		var req struct {
			Weekday string `json:"weekday"`
			TFStart string `json:"tfStart"`
			TFEnd   string `json:"tfEnd"`
		}
		if !s.deviceExists(name) || json.NewDecoder(r.Body).Decode(&req) != nil ||
			!validWeekday(req.Weekday) || !validHHMM(req.TFStart) || !validHHMM(req.TFEnd) ||
			req.TFStart >= req.TFEnd { // crossing midnight forbidden
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		s.store.With(func(st *Store) {
			d := st.Devices[name].Days[req.Weekday]
			d.TFStart, d.TFEnd = req.TFStart, req.TFEnd
		})
		s.recomputeStatusNow()
		w.WriteHeader(http.StatusNoContent)
	})

	// POST /api/device/{name}/pause  {"toggle":"pauseON"|"pauseOFF"}
	mux.HandleFunc("POST /api/device/{name}/pause", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		var req struct {
			Toggle string `json:"toggle"`
		}
		if !s.deviceExists(name) || json.NewDecoder(r.Body).Decode(&req) != nil ||
			(req.Toggle != PauseON && req.Toggle != PauseOFF) {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		rejected := false
		s.store.With(func(st *Store) {
			ds := st.Devices[name]
			// pause is ghosted (no-op) while TF/TR already block the device
			if req.Toggle == PauseON &&
				(ds.DeviceStatus == StatusBlockedNoTime || ds.DeviceStatus == StatusBlockedNotInTimeframe) {
				rejected = true
				return
			}
			ds.PauseToggle = req.Toggle
			if req.Toggle == PauseON {
				ds.PauseMinutesRemaining = pauseTotalMinutes
			} else {
				ds.PauseMinutesRemaining = 0
			}
		})
		if rejected {
			http.Error(w, "pause not applicable while TF/TR block", http.StatusConflict)
			return
		}
		s.recomputeStatusNow()
		w.WriteHeader(http.StatusNoContent)
	})

	// POST /api/device/{name}/enforcement  {"toggle":"enforcementON"|"enforcementOFF"}
	mux.HandleFunc("POST /api/device/{name}/enforcement", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		var req struct {
			Toggle string `json:"toggle"`
		}
		if !s.deviceExists(name) || json.NewDecoder(r.Body).Decode(&req) != nil ||
			(req.Toggle != EnforcementON && req.Toggle != EnforcementOFF) {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		s.store.With(func(st *Store) {
			st.Devices[name].EnforcementToggle = req.Toggle
		})
		s.recomputeStatusNow()
		w.WriteHeader(http.StatusNoContent)
	})
}

func validWeekday(wd string) bool {
	for _, k := range weekdayKeys {
		if k == wd {
			return true
		}
	}
	return false
}
