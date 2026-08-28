package main

import (
	"context"
	"log"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const scriptTimeout = 10 * time.Second

// Engine runs the 1-min evaluation loop and executes device scripts.
type Engine struct {
	devicesDir string
	store      *StateStore
	broadcast  func() // notifies SSE clients after each evaluation
}

func NewEngine(devicesDir string, store *StateStore, broadcast func()) *Engine {
	return &Engine{devicesDir: devicesDir, store: store, broadcast: broadcast}
}

// Run evaluates immediately, then every minute, until ctx is cancelled.
func (e *Engine) Run(ctx context.Context) {
	e.EvaluateAll()
	ticker := time.NewTicker(1 * time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			e.EvaluateAll()
		}
	}
}

// runScript executes a device script with a 10s timeout.
// Returns trimmed stdout and nil error only on exit-code 0.
func (e *Engine) runScript(deviceName, script string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), scriptTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, filepath.Join(e.devicesDir, deviceName, script))
	out, err := cmd.Output()
	return strings.TrimSpace(string(out)), err
}

// getState returns "up", "down" or "unknown" (errors/timeouts -> "unknown").
func (e *Engine) getState(deviceName string) string {
	out, err := e.runScript(deviceName, "getState.sh")
	if err != nil {
		return "unknown"
	}
	switch out {
	case "up", "down":
		return out
	default:
		return "unknown"
	}
}

// withinTimeframe reports whether now ("HH:MM") is within TFstart..TFend.
func withinTimeframe(now time.Time, tfStart, tfEnd string) bool {
	hhmm := now.Format("15:04")
	return hhmm >= tfStart && hhmm <= tfEnd
}

// decideStatus implements the deviceStatus decision flow (first match wins).
func decideStatus(ds *DeviceState, day *DayVars, now time.Time, upState string) string {
	if ds.EnforcementToggle == EnforcementOFF {
		return StatusEnforcementOFF
	}
	if !withinTimeframe(now, day.TFStart, day.TFEnd) {
		return StatusBlockedNotInTimeframe
	}
	if day.TRMinutes() == 0 {
		return StatusBlockedNoTime
	}
	if ds.PauseToggle == PauseON && ds.PauseMinutesRemaining > 0 {
		return StatusBlockedPauseON
	}
	if upState == "up" {
		return StatusInUse
	}
	return StatusNotInUse
}

// EvaluateAll runs one full evaluation pass over all devices (the 1-min flow).
func (e *Engine) EvaluateAll() {
	now := time.Now()
	today := weekdayKeys[now.Weekday()]
	date := now.Format("2006-01-02")

	// getState.sh outside the lock (scripts can take up to 10s each), one goroutine per device
	var names []string
	e.store.Read(func(st *Store) {
		for name := range st.Devices {
			names = append(names, name)
		}
	})
	results := make([]string, len(names))
	var wg sync.WaitGroup
	for i, name := range names {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i] = e.getState(name)
		}()
	}
	wg.Wait()
	upStates := make(map[string]string, len(names))
	for i, name := range names {
		upStates[name] = results[i]
	}

	type action struct{ device, script string }
	var actions []action

	err := e.store.With(func(st *Store) {
		// midnight rollover: reset today's TU (backend-server local time)
		if st.LastTickDate != date {
			st.LastTickDate = date
			for _, ds := range st.Devices {
				if d := ds.Days[today]; d != nil {
					d.TUMinutes = 0
				}
			}
		}
		for name, ds := range st.Devices {
			day := ds.Days[today]
			up := upStates[name]
			ds.appendStateHistory(upStateValue(up))

			// pause countdown; at 0 revert to pauseOFF
			if ds.PauseToggle == PauseON {
				if ds.PauseMinutesRemaining > 0 {
					ds.PauseMinutesRemaining--
				}
				if ds.PauseMinutesRemaining <= 0 {
					ds.PauseToggle = PauseOFF
					ds.PauseMinutesRemaining = 0
				}
			}

			prev := ds.DeviceStatus
			ds.DeviceStatus = decideStatus(ds, day, now, up)

			// getState.sh only reflects reality while the device is not blocked;
			// remember the last reliable reading to reuse when a block is lifted.
			if !hasBlockedPrefix(prev) {
				ds.LastUpState = up
			}
			// entering blockedNotInTimeframe cancels an active pause:
			// the device stays blocked only due to the timeframe
			if ds.DeviceStatus == StatusBlockedNotInTimeframe && ds.PauseToggle == PauseON {
				ds.PauseToggle = PauseOFF
				ds.PauseMinutesRemaining = 0
			}

			// block/unblock (edge-triggered unblock, as per design)
			if strings.HasPrefix(ds.DeviceStatus, "blocked") {
				if up == "up" {
					actions = append(actions, action{name, "block.sh"})
				}
			} else if strings.HasPrefix(prev, "blocked") {
				actions = append(actions, action{name, "unblock.sh"})
			}

			// TU +1min only when inUse
			if ds.DeviceStatus == StatusInUse {
				day.TUMinutes++
				if day.TRMinutes() == 0 {
					ds.DeviceStatus = StatusBlockedNoTime
					if up == "up" {
						actions = append(actions, action{name, "block.sh"})
					}
				}
			}
		}
	})
	if err != nil {
		log.Printf("engine: save failed: %v", err)
	}

	for _, a := range actions {
		if _, err := e.runScript(a.device, a.script); err != nil {
			log.Printf("engine: %s/%s failed: %v", a.device, a.script, err)
		}
	}
	e.broadcast()
}
