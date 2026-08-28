package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptrace"
	"sort"
	"strings"
	"time"
)

// State mirrors the /api/state response.
type State struct {
	Today   string            `json:"today" yaml:"today"`
	Devices map[string]Device `json:"devices" yaml:"devices"`
}

type Device struct {
	DeviceStatus          string         `json:"deviceStatus" yaml:"deviceStatus"`
	EnforcementToggle     string         `json:"enforcementToggle" yaml:"enforcementToggle"`
	PauseToggle           string         `json:"pauseToggle" yaml:"pauseToggle"`
	PauseMinutesRemaining int            `json:"pauseMinutesRemaining" yaml:"pauseMinutesRemaining"`
	Days                  map[string]Day `json:"days" yaml:"days"`
}

type Day struct {
	TAMinutes int    `json:"taMinutes" yaml:"taMinutes"`
	TUMinutes int    `json:"tuMinutes" yaml:"tuMinutes"`
	TRMinutes int    `json:"trMinutes" yaml:"trMinutes"`
	TFStart   string `json:"tfStart" yaml:"tfStart"`
	TFEnd     string `json:"tfEnd" yaml:"tfEnd"`
}

// DeviceNames returns the device keys sorted alphabetically.
func (s *State) DeviceNames() []string {
	names := make([]string, 0, len(s.Devices))
	for n := range s.Devices {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// APIError is a non-2xx HTTP response from the kidsout server.
type APIError struct {
	Status int
	Body   string
}

func (e *APIError) Error() string {
	msg := strings.TrimSpace(e.Body)
	if msg == "" {
		msg = http.StatusText(e.Status)
	}
	return fmt.Sprintf("server returned %d: %s", e.Status, msg)
}

// Client is a minimal kidsout API client with leveled request logging.
type Client struct {
	Base      string // e.g. "http://localhost:8080"
	Auth      string // "user:pass"
	Timeout   time.Duration
	Verbosity int // 0..5
	Log       io.Writer
}

func (c *Client) logf(level int, format string, args ...any) {
	if c.Verbosity >= level && c.Log != nil {
		fmt.Fprintf(c.Log, "[v%d] "+format+"\n", append([]any{level}, args...)...)
	}
}

func (c *Client) setAuth(req *http.Request) {
	user, pass, _ := strings.Cut(c.Auth, ":")
	req.SetBasicAuth(user, pass)
}

// logHeaders dumps headers at v3, redacting credentials.
func (c *Client) logHeaders(prefix string, h http.Header) {
	if c.Verbosity < 3 {
		return
	}
	keys := make([]string, 0, len(h))
	for k := range h {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		v := strings.Join(h[k], ", ")
		if strings.EqualFold(k, "Authorization") || strings.EqualFold(k, "Set-Cookie") {
			v = "<redacted>"
		}
		c.logf(3, "%s %s: %s", prefix, k, v)
	}
}

func (c *Client) traceContext(ctx context.Context) context.Context {
	if c.Verbosity < 5 {
		return ctx
	}
	trace := &httptrace.ClientTrace{
		DNSStart: func(i httptrace.DNSStartInfo) { c.logf(5, "dns lookup %s", i.Host) },
		DNSDone: func(i httptrace.DNSDoneInfo) {
			c.logf(5, "dns done (%d addrs, err=%v)", len(i.Addrs), i.Err)
		},
		ConnectStart: func(network, addr string) { c.logf(5, "connecting %s %s", network, addr) },
		ConnectDone: func(network, addr string, err error) {
			c.logf(5, "connected %s %s (err=%v)", network, addr, err)
		},
		GotConn: func(i httptrace.GotConnInfo) { c.logf(5, "got connection (reused=%v)", i.Reused) },
		WroteRequest: func(i httptrace.WroteRequestInfo) {
			c.logf(5, "request written (err=%v)", i.Err)
		},
		GotFirstResponseByte: func() { c.logf(5, "first response byte") },
	}
	return httptrace.WithClientTrace(ctx, trace)
}

// do performs a request and returns the response body for 2xx statuses.
func (c *Client) do(ctx context.Context, method, path string, body any) ([]byte, error) {
	var payload []byte
	var rdr io.Reader
	if body != nil {
		var err error
		payload, err = json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rdr = bytes.NewReader(payload)
	}
	url := c.Base + path
	c.logf(1, "%s %s", method, url)
	if payload != nil {
		c.logf(4, "> %s", payload)
	}

	req, err := http.NewRequestWithContext(c.traceContext(ctx), method, url, rdr)
	if err != nil {
		return nil, err
	}
	c.setAuth(req)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	c.logHeaders(">", req.Header)

	start := time.Now()
	client := &http.Client{Timeout: c.Timeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	c.logf(2, "%s %s -> %s in %s", method, url, resp.Status, time.Since(start).Round(time.Millisecond))
	c.logHeaders("<", resp.Header)

	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if len(data) > 0 {
		c.logf(4, "< %s", bytes.TrimSpace(data))
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, &APIError{Status: resp.StatusCode, Body: string(data)}
	}
	return data, nil
}

// GetState fetches and decodes /api/state; raw is the untouched JSON body.
func (c *Client) GetState(ctx context.Context) (*State, []byte, error) {
	raw, err := c.do(ctx, http.MethodGet, "/api/state", nil)
	if err != nil {
		return nil, nil, err
	}
	var s State
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, nil, fmt.Errorf("decoding /api/state: %w", err)
	}
	return &s, raw, nil
}

func (c *Client) SetPause(ctx context.Context, device string, on bool) error {
	toggle := "pauseOFF"
	if on {
		toggle = "pauseON"
	}
	_, err := c.do(ctx, http.MethodPost, "/api/device/"+device+"/pause",
		map[string]string{"toggle": toggle})
	return err
}

func (c *Client) SetEnforcement(ctx context.Context, device string, on bool) error {
	toggle := "enforcementOFF"
	if on {
		toggle = "enforcementON"
	}
	_, err := c.do(ctx, http.MethodPost, "/api/device/"+device+"/enforcement",
		map[string]string{"toggle": toggle})
	return err
}

func (c *Client) AdjustTA(ctx context.Context, device, weekday string, delta int) error {
	_, err := c.do(ctx, http.MethodPost, "/api/device/"+device+"/ta",
		map[string]any{"weekday": weekday, "deltaMinutes": delta})
	return err
}

func (c *Client) SetTF(ctx context.Context, device, weekday, start, end string) error {
	_, err := c.do(ctx, http.MethodPost, "/api/device/"+device+"/tf",
		map[string]string{"weekday": weekday, "tfStart": start, "tfEnd": end})
	return err
}

// Watch subscribes to /api/events and invokes fn for every SSE data payload.
func (c *Client) Watch(ctx context.Context, fn func(raw []byte) error) error {
	url := c.Base + "/api/events"
	c.logf(1, "GET %s (SSE)", url)
	req, err := http.NewRequestWithContext(c.traceContext(ctx), http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	c.setAuth(req)
	req.Header.Set("Accept", "text/event-stream")
	c.logHeaders(">", req.Header)

	resp, err := (&http.Client{}).Do(req) // no timeout: stream stays open
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	c.logf(2, "GET %s -> %s", url, resp.Status)
	c.logHeaders("<", resp.Header)
	if resp.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return &APIError{Status: resp.StatusCode, Body: string(data)}
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 4<<20)
	var data bytes.Buffer
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "data:"):
			data.WriteString(strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		case line == "": // event boundary
			if data.Len() > 0 {
				payload := append([]byte(nil), data.Bytes()...)
				data.Reset()
				c.logf(4, "< event %s", payload)
				if err := fn(payload); err != nil {
					return err
				}
			}
		case strings.HasPrefix(line, ":"): // keepalive comment
			c.logf(5, "sse keepalive")
		}
	}
	if err := scanner.Err(); err != nil && ctx.Err() == nil {
		return fmt.Errorf("event stream interrupted: %w", err)
	}
	return ctx.Err()
}
