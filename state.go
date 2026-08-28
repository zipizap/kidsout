package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// deviceStatus values
const (
	StatusInUse                 = "inUse"
	StatusNotInUse              = "notInUse"
	StatusBlockedNoTime         = "blockedNoTime"
	StatusBlockedNotInTimeframe = "blockedNotInTimeframe"
	StatusBlockedPauseON        = "blockedPauseON"
	StatusEnforcementOFF        = "enforcementOFF"
)

const (
	EnforcementON  = "enforcementON"
	EnforcementOFF = "enforcementOFF"
	PauseON        = "pauseON"
	PauseOFF       = "pauseOFF"
)

const (
	pauseTotalMinutes = 20
	taStepMinutes     = 10
	taMaxMinutes      = 24 * 60
)

// weekdayKeys in Go time.Weekday order (Sunday=0)
var weekdayKeys = [7]string{"sun", "mon", "tue", "wed", "thu", "fri", "sat"}

// DayVars holds the per-weekday, per-device vars.
type DayVars struct {
	TAMinutes int    `yaml:"taMinutes" json:"taMinutes"` // time allowed, recurring per weekday
	TUMinutes int    `yaml:"tuMinutes" json:"tuMinutes"` // time used (today's weekday only is live)
	TFStart   string `yaml:"tfStart" json:"tfStart"`     // "09:00"
	TFEnd     string `yaml:"tfEnd" json:"tfEnd"`         // "21:00"
}

// TRMinutes = max(0, TA-TU)
func (d DayVars) TRMinutes() int {
	tr := d.TAMinutes - d.TUMinutes
	if tr < 0 {
		return 0
	}
	return tr
}

// DeviceState holds all runtime vars of one device.
type DeviceState struct {
	EnforcementToggle     string              `yaml:"enforcementToggle" json:"enforcementToggle"`
	PauseToggle           string              `yaml:"pauseToggle" json:"pauseToggle"`
	PauseMinutesRemaining int                 `yaml:"pauseMinutesRemaining" json:"pauseMinutesRemaining"`
	DeviceStatus          string              `yaml:"deviceStatus" json:"deviceStatus"`
	Days                  map[string]*DayVars `yaml:"days" json:"days"` // key: "mon".."sun"

	// StateHistory tracks recent getState.sh results (-1 unknown, 0 down, 1 up),
	// oldest first; in-memory only (not persisted to runtimestore.yaml).
	StateHistory []int `yaml:"-" json:"stateHistory"`

	// LastUpState is the most recent getState.sh reading taken while the device
	// was NOT physically blocked ("up"/"down"). getState.sh is unreliable while a
	// device is blocked (it reports "down"), so this preserves the true state to
	// use when a block is lifted. In-memory only.
	LastUpState string `yaml:"-" json:"-"`
}

// maxStateHistory caps StateHistory to the last N engine ticks.
const maxStateHistory = 20

// upStateValue maps a getState.sh result to its numeric history value.
func upStateValue(up string) int {
	switch up {
	case "up":
		return 1
	case "down":
		return 0
	default:
		return -1
	}
}

// appendStateHistory appends v, trimming from the front once over maxStateHistory.
func (ds *DeviceState) appendStateHistory(v int) {
	ds.StateHistory = append(ds.StateHistory, v)
	if over := len(ds.StateHistory) - maxStateHistory; over > 0 {
		ds.StateHistory = ds.StateHistory[over:]
	}
}

// AuthConfig holds the HTTP Basic Auth credentials, stored in plaintext in
// runtimestore.yaml on purpose so the admin can see/change them in the file.
type AuthConfig struct {
	Username string `yaml:"username" json:"-"`
	Password string `yaml:"password" json:"-"`
}

// Store is the persisted runtime state (runtimestore.yaml).
type Store struct {
	LastTickDate string                  `yaml:"lastTickDate" json:"-"` // "2006-01-02", for midnight TU reset
	Auth         AuthConfig              `yaml:"auth" json:"-"`
	Devices      map[string]*DeviceState `yaml:"devices" json:"devices"`
}

func defaultDayVars() *DayVars {
	return &DayVars{TAMinutes: 120, TUMinutes: 0, TFStart: "09:00", TFEnd: "21:00"}
}

func defaultDeviceState() *DeviceState {
	days := map[string]*DayVars{}
	for _, wd := range weekdayKeys {
		days[wd] = defaultDayVars()
	}
	return &DeviceState{
		EnforcementToggle: EnforcementON,
		PauseToggle:       PauseOFF,
		DeviceStatus:      StatusNotInUse,
		Days:              days,
	}
}

// StateStore wraps the Store with locking and atomic YAML persistence.
type StateStore struct {
	mu   sync.Mutex
	path string
	data *Store
}

func LoadStateStore(path string, deviceNames []string) (*StateStore, error) {
	s := &StateStore{path: path, data: &Store{Devices: map[string]*DeviceState{}}}
	b, err := os.ReadFile(path)
	if err == nil {
		if err := yaml.Unmarshal(b, s.data); err != nil {
			return nil, fmt.Errorf("parsing %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if s.data.Devices == nil {
		s.data.Devices = map[string]*DeviceState{}
	}
	// ensure every configured device has complete state
	for _, name := range deviceNames {
		ds, ok := s.data.Devices[name]
		if !ok {
			s.data.Devices[name] = defaultDeviceState()
			continue
		}
		if ds.EnforcementToggle == "" {
			ds.EnforcementToggle = EnforcementON
		}
		if ds.PauseToggle == "" {
			ds.PauseToggle = PauseOFF
		}
		if ds.DeviceStatus == "" {
			ds.DeviceStatus = StatusNotInUse
		}
		if ds.Days == nil {
			ds.Days = map[string]*DayVars{}
		}
		for _, wd := range weekdayKeys {
			if ds.Days[wd] == nil {
				ds.Days[wd] = defaultDayVars()
			}
		}
	}
	// drop devices no longer configured
	for name := range s.data.Devices {
		found := false
		for _, n := range deviceNames {
			if n == name {
				found = true
				break
			}
		}
		if !found {
			delete(s.data.Devices, name)
		}
	}
	if s.data.LastTickDate == "" {
		s.data.LastTickDate = time.Now().Format("2006-01-02")
	}
	if s.data.Auth.Username == "" {
		s.data.Auth.Username = "mae"
	}
	if s.data.Auth.Password == "" {
		s.data.Auth.Password = "pai"
	}
	return s, s.saveLocked()
}

// With runs fn with exclusive access to the store, then persists atomically.
func (s *StateStore) With(fn func(*Store)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	fn(s.data)
	return s.saveLocked()
}

// Read runs fn with exclusive read access (no persistence).
func (s *StateStore) Read(fn func(*Store)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	fn(s.data)
}

// saveLocked writes YAML atomically (tmp file + rename). Caller must hold mu.
func (s *StateStore) saveLocked() error {
	b, err := yaml.Marshal(s.data)
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// DiscoverDevices scans devicesDir for subdirs containing the 3 required scripts.
func DiscoverDevices(devicesDir string) ([]string, error) {
	entries, err := os.ReadDir(devicesDir)
	if err != nil {
		return nil, fmt.Errorf("reading devices dir %s: %w", devicesDir, err)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		ok := true
		for _, script := range []string{"getState.sh", "block.sh", "unblock.sh"} {
			if _, err := os.Stat(filepath.Join(devicesDir, e.Name(), script)); err != nil {
				ok = false
				break
			}
		}
		if ok {
			names = append(names, e.Name())
		}
	}
	return names, nil
}
