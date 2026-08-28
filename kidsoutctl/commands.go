package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"syscall"
)

// errWatchDone signals a clean --once exit from the SSE loop.
var errWatchDone = errors.New("watch done")

// app binds the client, options and output streams for command handlers.
type app struct {
	c   *Client
	o   *options
	out io.Writer
	err io.Writer
	p   painter
}

// resolveDevices expands --all (or validates the given names) against /api/state.
func (a *app) resolveDevices(ctx context.Context, args []string, verb string) ([]string, error) {
	s, _, err := a.c.GetState(ctx)
	if err != nil {
		return nil, err
	}
	if a.o.all {
		if len(args) > 0 {
			return nil, usageError(fmt.Sprintf("%s: give device names or --all, not both", verb))
		}
		return s.DeviceNames(), nil
	}
	if len(args) == 0 {
		return nil, usageError(fmt.Sprintf("%s: need at least one device name or --all", verb))
	}
	for _, d := range args {
		if _, ok := s.Devices[d]; !ok {
			return nil, fmt.Errorf("unknown device %q (known: %v)", d, s.DeviceNames())
		}
	}
	return args, nil
}

// resolveWeekday accepts a weekday key or "today".
func (a *app) resolveWeekday(ctx context.Context, arg string) (string, error) {
	if arg == "today" {
		s, _, err := a.c.GetState(ctx)
		if err != nil {
			return "", err
		}
		return s.Today, nil
	}
	if !validWeekday(arg) {
		return "", usageError(fmt.Sprintf("invalid weekday %q (want %v or today)", arg, weekOrder))
	}
	return arg, nil
}

func (a *app) cmdGet(args []string) error {
	ctx := context.Background()
	s, raw, err := a.c.GetState(ctx)
	if err != nil {
		return err
	}
	if a.o.output != "table" {
		return renderRaw(a.out, a.o.output, raw)
	}
	if a.o.week {
		return renderWeekTable(a.out, a.p, s, args)
	}
	return renderStateTable(a.out, a.p, s, args)
}

func (a *app) cmdWatch(args []string) error {
	if len(args) > 0 {
		return usageError("watch takes no arguments")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	first := true
	err := a.c.Watch(ctx, func(raw []byte) error {
		if a.o.output != "table" {
			if err := renderRaw(a.out, a.o.output, raw); err != nil {
				return err
			}
		} else {
			var s State
			if err := json.Unmarshal(raw, &s); err != nil {
				return err
			}
			if !first {
				fmt.Fprintln(a.out)
			}
			if err := renderStateTable(a.out, a.p, &s, nil); err != nil {
				return err
			}
		}
		first = false
		if a.o.once {
			return errWatchDone
		}
		return nil
	})
	if err == errWatchDone || err == context.Canceled {
		return nil
	}
	return err
}

func (a *app) cmdPause(args []string, on bool) error {
	verb := "pause"
	if !on {
		verb = "unpause"
	}
	ctx := context.Background()
	devices, err := a.resolveDevices(ctx, args, verb)
	if err != nil {
		return err
	}
	failed := false
	for _, d := range devices {
		if err := a.c.SetPause(ctx, d, on); err != nil {
			fmt.Fprintf(a.err, "%s %s %s: %v\n", a.p.paint(cRed, "error:"), verb, d, err)
			failed = true
			continue
		}
		if on {
			fmt.Fprintf(a.out, "%s paused %s\n", d, a.p.paint(cYellow, "(20-minute countdown started)"))
		} else {
			fmt.Fprintf(a.out, "%s unpaused\n", d)
		}
	}
	if failed {
		return exitError{code: 1}
	}
	return nil
}

func (a *app) cmdEnforce(args []string, on bool) error {
	verb := "enforce"
	if !on {
		verb = "unenforce"
	}
	ctx := context.Background()
	devices, err := a.resolveDevices(ctx, args, verb)
	if err != nil {
		return err
	}
	failed := false
	for _, d := range devices {
		if err := a.c.SetEnforcement(ctx, d, on); err != nil {
			fmt.Fprintf(a.err, "%s %s %s: %v\n", a.p.paint(cRed, "error:"), verb, d, err)
			failed = true
			continue
		}
		if on {
			fmt.Fprintf(a.out, "%s enforcement %s\n", d, a.p.paint(cGreen, "ON"))
		} else {
			fmt.Fprintf(a.out, "%s enforcement %s %s\n", d, a.p.paint(cCyan, "OFF"),
				a.p.paint(cDim, "(free use, no time accrual)"))
		}
	}
	if failed {
		return exitError{code: 1}
	}
	return nil
}

func (a *app) cmdTA(args []string) error {
	if len(args) != 3 {
		return usageError("usage: kidsoutctl ta <device> <weekday|today> <±minutes>")
	}
	device, wdArg, deltaArg := args[0], args[1], args[2]
	delta, err := strconv.Atoi(deltaArg)
	if err != nil {
		return usageError(fmt.Sprintf("invalid minutes delta %q (want an integer, e.g. 30 or -15)", deltaArg))
	}
	if delta == 0 {
		return usageError("minutes delta must be non-zero")
	}
	ctx := context.Background()
	weekday, err := a.resolveWeekday(ctx, wdArg)
	if err != nil {
		return err
	}
	if err := a.c.AdjustTA(ctx, device, weekday, delta); err != nil {
		return err
	}
	sign := "+"
	if delta < 0 {
		sign = ""
	}
	fmt.Fprintf(a.out, "%s %s allowance %s\n", device, weekday,
		a.p.paint(cGreen, fmt.Sprintf("%s%dm", sign, delta)))
	return nil
}

var hhmmRE = regexp.MustCompile(`^([01]\d|2[0-3]):[0-5]\d$`)

func (a *app) cmdTF(args []string) error {
	if len(args) != 4 {
		return usageError("usage: kidsoutctl tf <device> <weekday|today> <HH:MM> <HH:MM>")
	}
	device, wdArg, start, end := args[0], args[1], args[2], args[3]
	for _, t := range []string{start, end} {
		if !hhmmRE.MatchString(t) {
			return usageError(fmt.Sprintf("invalid time %q (want HH:MM, 24h)", t))
		}
	}
	if start >= end {
		return usageError(fmt.Sprintf("tfStart %q must be before tfEnd %q (crossing midnight is not supported)", start, end))
	}
	ctx := context.Background()
	weekday, err := a.resolveWeekday(ctx, wdArg)
	if err != nil {
		return err
	}
	if err := a.c.SetTF(ctx, device, weekday, start, end); err != nil {
		return err
	}
	fmt.Fprintf(a.out, "%s %s timeframe set to %s\n", device, weekday, a.p.paint(cGreen, start+"-"+end))
	return nil
}
