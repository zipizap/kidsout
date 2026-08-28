package main

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"gopkg.in/yaml.v3"
)

// ANSI SGR codes.
const (
	cReset  = "\033[0m"
	cBold   = "\033[1m"
	cDim    = "\033[2m"
	cRed    = "\033[31m"
	cGreen  = "\033[32m"
	cYellow = "\033[33m"
	cBlue   = "\033[34m"
	cCyan   = "\033[36m"
)

type painter struct{ enabled bool }

func (p painter) paint(code, s string) string {
	if !p.enabled || s == "" {
		return s
	}
	return code + s + cReset
}

// statusColor maps deviceStatus values to a color conveying severity.
func statusColor(status string) string {
	switch status {
	case "inUse":
		return cGreen
	case "notInUse":
		return cBlue
	case "blockedNoTime", "blockedNotInTimeframe":
		return cRed
	case "blockedPauseON":
		return cYellow
	case "enforcementOFF":
		return cCyan
	default:
		return cDim
	}
}

// weekOrder starts on mon to match the human week.
var weekOrder = []string{"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

func validWeekday(d string) bool {
	for _, w := range weekOrder {
		if w == d {
			return true
		}
	}
	return false
}

// table renders rows with aligned columns; header is painted bold+dim.
type table struct {
	header []string
	rows   [][]string // each cell: [text, colorCode]
	colors [][]string
}

func (t *table) addRow(cells []string, colors []string) {
	t.rows = append(t.rows, cells)
	t.colors = append(t.colors, colors)
}

func (t *table) write(w io.Writer, p painter) {
	widths := make([]int, len(t.header))
	for i, h := range t.header {
		widths[i] = len(h)
	}
	for _, row := range t.rows {
		for i, c := range row {
			if len(c) > widths[i] {
				widths[i] = len(c)
			}
		}
	}
	var b strings.Builder
	for i, h := range t.header {
		pad := widths[i] - len(h)
		b.WriteString(p.paint(cBold+cDim, h))
		if i < len(t.header)-1 {
			b.WriteString(strings.Repeat(" ", pad+2))
		}
	}
	fmt.Fprintln(w, strings.TrimRight(b.String(), " "))
	for r, row := range t.rows {
		b.Reset()
		for i, c := range row {
			pad := widths[i] - len(c)
			cell := c
			if code := t.colors[r][i]; code != "" {
				cell = p.paint(code, c)
			}
			b.WriteString(cell)
			if i < len(row)-1 {
				b.WriteString(strings.Repeat(" ", pad+2))
			}
		}
		fmt.Fprintln(w, strings.TrimRight(b.String(), " "))
	}
}

func fmtMinutes(m int) string {
	if m >= 60 {
		return fmt.Sprintf("%dh%02dm", m/60, m%60)
	}
	return fmt.Sprintf("%dm", m)
}

// renderStateTable prints the per-device summary for today.
func renderStateTable(w io.Writer, p painter, s *State, only []string) error {
	names := s.DeviceNames()
	if len(only) > 0 {
		names = only
	}
	t := &table{header: []string{"DEVICE", "STATUS", "ENFORCE", "PAUSE", "ALLOWED", "USED", "REMAINING", "TIMEFRAME"}}
	for _, name := range names {
		d, ok := s.Devices[name]
		if !ok {
			return fmt.Errorf("unknown device %q (known: %s)", name, strings.Join(s.DeviceNames(), ", "))
		}
		day := d.Days[s.Today]
		pause := "off"
		pauseColor := cDim
		if d.PauseToggle == "pauseON" {
			pause = fmt.Sprintf("on (%dm left)", d.PauseMinutesRemaining)
			pauseColor = cYellow
		}
		enforce := "on"
		enforceColor := ""
		if d.EnforcementToggle == "enforcementOFF" {
			enforce = "off (free use)"
			enforceColor = cCyan
		}
		remColor := cGreen
		if day.TRMinutes == 0 {
			remColor = cRed
		} else if day.TRMinutes <= 15 {
			remColor = cYellow
		}
		t.addRow(
			[]string{
				name, d.DeviceStatus, enforce, pause,
				fmtMinutes(day.TAMinutes), fmtMinutes(day.TUMinutes), fmtMinutes(day.TRMinutes),
				day.TFStart + "-" + day.TFEnd,
			},
			[]string{cBold, statusColor(d.DeviceStatus), enforceColor, pauseColor, "", "", remColor, ""},
		)
	}
	fmt.Fprintf(w, "%s %s\n", p.paint(cDim, "today:"), p.paint(cBold, s.Today))
	t.write(w, p)
	return nil
}

// renderWeekTable prints the full weekly schedule per device.
func renderWeekTable(w io.Writer, p painter, s *State, only []string) error {
	names := s.DeviceNames()
	if len(only) > 0 {
		names = only
	}
	for i, name := range names {
		d, ok := s.Devices[name]
		if !ok {
			return fmt.Errorf("unknown device %q (known: %s)", name, strings.Join(s.DeviceNames(), ", "))
		}
		if i > 0 {
			fmt.Fprintln(w)
		}
		fmt.Fprintf(w, "%s %s\n", p.paint(cBold, name), p.paint(statusColor(d.DeviceStatus), "("+d.DeviceStatus+")"))
		t := &table{header: []string{"DAY", "ALLOWED", "USED", "REMAINING", "TIMEFRAME"}}
		for _, day := range weekOrder {
			dv := d.Days[day]
			dayColor := ""
			if day == s.Today {
				dayColor = cBold + cGreen
			}
			t.addRow(
				[]string{day, fmtMinutes(dv.TAMinutes), fmtMinutes(dv.TUMinutes), fmtMinutes(dv.TRMinutes), dv.TFStart + "-" + dv.TFEnd},
				[]string{dayColor, "", "", "", ""},
			)
		}
		t.write(w, p)
	}
	return nil
}

// renderRaw emits the API JSON as-is (pretty) or converted to YAML.
func renderRaw(w io.Writer, format string, raw []byte) error {
	switch format {
	case "json":
		var buf strings.Builder
		var v any
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		enc := json.NewEncoder(&buf)
		enc.SetIndent("", "  ")
		if err := enc.Encode(v); err != nil {
			return err
		}
		fmt.Fprint(w, buf.String())
	case "yaml":
		var v any
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		data, err := yaml.Marshal(v)
		if err != nil {
			return err
		}
		fmt.Fprint(w, string(data))
	default:
		return fmt.Errorf("unsupported format %q", format)
	}
	return nil
}
