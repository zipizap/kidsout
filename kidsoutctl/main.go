// Command kidsoutctl is a kubectl-style CLI for the kidsout HTTP API.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"regexp"
	"runtime"
	"strings"
	"time"
)

// Overridable at build time: -ldflags "-X main.version=... -X main.commit=..."
var (
	version = "1.0.0"
	commit  = "none"
)

const defaultServer = "http://localhost:8080"

const usageText = `kidsoutctl — kubectl-style CLI for the kidsout parental device blocker

Usage:
  kidsoutctl [flags] <command> [args] [flags]

Device commands:
  get [device...]                  Show device state (alias: state)
  watch                            Follow live state changes (SSE stream)
  pause     <device...> | --all    Block device(s) now (20-minute countdown)
  unpause   <device...> | --all    Cancel an active pause (alias: resume)
  enforce   <device...> | --all    Enforcement ON — rules apply again
  unenforce <device...> | --all    Enforcement OFF — free use, no time accrual
  ta <device> <weekday|today> <±minutes>
                                   Adjust recurring time allowance (delta)
  tf <device> <weekday|today> <HH:MM> <HH:MM>
                                   Set the allowed daily time-frame

Other commands:
  completion bash                  Print a bash completion script
  version                          Print version information
  help                             Show this help

Flags:
  -o table|json|yaml    Output format (default: table, colored on TTYs)
  --week                With get: show the full weekly schedule
  --all                 Apply pause/unpause/enforce/unenforce to every device
  --once                With watch: exit after the first snapshot
  --server URL          API base URL (default: $KIDSOUT_SERVER, $KO, or ` + defaultServer + `)
  --color auto|always|never
                        Colored output (default: auto; NO_COLOR is honored)
  --timeout DURATION    HTTP timeout for non-streaming requests (default: 10s)
  -v1 .. -v5            Verbosity: 1=requests 2=+timings 3=+headers 4=+bodies 5=+trace

Environment:
  KOAUTH / KIDSOUT_AUTH   Basic Auth credentials "user:pass" (required; never a flag)
  KO / KIDSOUT_SERVER     API base URL

Weekdays: sun mon tue wed thu fri sat (or "today")

Exit codes:
  0 success · 1 error · 2 usage · 3 authentication · 4 conflict (HTTP 409)

Examples:
  kidsoutctl get
  kidsoutctl get xbox -o yaml
  kidsoutctl get --week
  kidsoutctl pause --all
  kidsoutctl ta xbox today 30
  kidsoutctl ta xbox fri -15
  kidsoutctl tf tablet sat 10:00 22:00
  kidsoutctl watch
`

const bashCompletion = `# bash completion for kidsoutctl — source this file or install it under
# /etc/bash_completion.d/ (eg: kidsoutctl completion bash | sudo tee /etc/bash_completion.d/kidsoutctl)
_kidsoutctl() {
    local cur prev cmds
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    cmds="get state watch pause unpause resume enforce unenforce ta tf completion version help"
    case "$prev" in
        -o) COMPREPLY=($(compgen -W "table json yaml" -- "$cur")); return ;;
        --color) COMPREPLY=($(compgen -W "auto always never" -- "$cur")); return ;;
        completion) COMPREPLY=($(compgen -W "bash" -- "$cur")); return ;;
    esac
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$cmds" -- "$cur"))
    elif [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "-o --week --all --once --server --color --timeout -v1 -v2 -v3 -v4 -v5" -- "$cur"))
    fi
}
complete -F _kidsoutctl kidsoutctl
`

type options struct {
	server    string
	output    string
	color     string
	timeout   time.Duration
	verbosity int
	week      bool
	all       bool
	once      bool
}

// usageError renders with the usage hint and exits 2.
type usageError string

func (e usageError) Error() string { return string(e) }

// exitError requests a specific exit code without re-printing a message.
type exitError struct{ code int }

func (e exitError) Error() string { return fmt.Sprintf("exit %d", e.code) }

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// negNumRE matches negative numbers so "ta xbox fri -10" is not parsed as a flag.
var negNumRE = regexp.MustCompile(`^-\d+$`)

// splitFlags parses flags interspersed with positional args (kubectl-style).
func splitFlags(fs *flag.FlagSet, args []string) ([]string, error) {
	var pos []string
	for len(args) > 0 {
		a := args[0]
		if a == "--" {
			pos = append(pos, args[1:]...)
			break
		}
		if !strings.HasPrefix(a, "-") || negNumRE.MatchString(a) {
			pos = append(pos, a)
			args = args[1:]
			continue
		}
		if err := fs.Parse(args); err != nil {
			return nil, err
		}
		args = fs.Args()
	}
	return pos, nil
}

func colorEnabled(mode string, w io.Writer) bool {
	switch mode {
	case "always":
		return true
	case "never":
		return false
	}
	if os.Getenv("NO_COLOR") != "" {
		return false
	}
	f, ok := w.(*os.File)
	if !ok {
		return false
	}
	fi, err := f.Stat()
	return err == nil && fi.Mode()&os.ModeCharDevice != 0
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(argv []string, stdout, stderr io.Writer) int {
	o := &options{}
	fs := flag.NewFlagSet("kidsoutctl", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { fmt.Fprint(stderr, usageText) }
	fs.StringVar(&o.server, "server", envOr("KIDSOUT_SERVER", envOr("KO", defaultServer)), "API base URL")
	fs.StringVar(&o.output, "o", "table", "output format: table|json|yaml")
	fs.StringVar(&o.color, "color", "auto", "colored output: auto|always|never")
	fs.DurationVar(&o.timeout, "timeout", 10*time.Second, "HTTP timeout for non-streaming requests")
	fs.BoolVar(&o.week, "week", false, "with get: show the full weekly schedule")
	fs.BoolVar(&o.all, "all", false, "apply command to all devices")
	fs.BoolVar(&o.once, "once", false, "with watch: exit after the first snapshot")
	var v [5]bool
	for i := range v {
		fs.BoolVar(&v[i], fmt.Sprintf("v%d", i+1), false, fmt.Sprintf("verbosity level %d", i+1))
	}

	pos, err := splitFlags(fs, argv)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	for i := range v {
		if v[i] {
			o.verbosity = i + 1
		}
	}
	switch o.output {
	case "table", "json", "yaml":
	default:
		fmt.Fprintf(stderr, "error: invalid output format %q (want table|json|yaml)\n", o.output)
		return 2
	}
	if len(pos) == 0 {
		fs.Usage()
		return 2
	}
	cmd, args := pos[0], pos[1:]

	// Commands that need no server or credentials.
	switch cmd {
	case "help":
		fmt.Fprint(stdout, usageText)
		return 0
	case "version":
		fmt.Fprintf(stdout, "kidsoutctl %s (commit %s, %s %s/%s)\n",
			version, commit, runtime.Version(), runtime.GOOS, runtime.GOARCH)
		return 0
	case "completion":
		if len(args) != 1 || args[0] != "bash" {
			fmt.Fprintln(stderr, "error: usage: kidsoutctl completion bash")
			return 2
		}
		fmt.Fprint(stdout, bashCompletion)
		return 0
	}

	auth := envOr("KIDSOUT_AUTH", envOr("KOAUTH", ""))
	if auth == "" {
		fmt.Fprintln(stderr, `error: missing credentials — set KOAUTH="user:pass" (or KIDSOUT_AUTH)`)
		return 3
	}

	p := painter{enabled: colorEnabled(o.color, stdout)}
	a := &app{
		c: &Client{
			Base:      strings.TrimRight(o.server, "/"),
			Auth:      auth,
			Timeout:   o.timeout,
			Verbosity: o.verbosity,
			Log:       stderr,
		},
		o:   o,
		out: stdout,
		err: stderr,
		p:   p,
	}

	var cmdErr error
	switch cmd {
	case "get", "state":
		cmdErr = a.cmdGet(args)
	case "watch":
		cmdErr = a.cmdWatch(args)
	case "pause":
		cmdErr = a.cmdPause(args, true)
	case "unpause", "resume":
		cmdErr = a.cmdPause(args, false)
	case "enforce":
		cmdErr = a.cmdEnforce(args, true)
	case "unenforce":
		cmdErr = a.cmdEnforce(args, false)
	case "ta":
		cmdErr = a.cmdTA(args)
	case "tf":
		cmdErr = a.cmdTF(args)
	default:
		fmt.Fprintf(stderr, "error: unknown command %q — run 'kidsoutctl help'\n", cmd)
		return 2
	}
	return exitCode(cmdErr, stderr, p)
}

func exitCode(err error, stderr io.Writer, p painter) int {
	if err == nil {
		return 0
	}
	var ue usageError
	if errors.As(err, &ue) {
		fmt.Fprintf(stderr, "error: %s\n", string(ue))
		return 2
	}
	var ee exitError
	if errors.As(err, &ee) {
		return ee.code
	}
	fmt.Fprintf(stderr, "%s %v\n", p.paint(cRed, "error:"), err)
	var ae *APIError
	if errors.As(err, &ae) {
		switch ae.Status {
		case 401:
			return 3
		case 409:
			return 4
		}
	}
	return 1
}
