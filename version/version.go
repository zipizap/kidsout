// Package version holds the build identity shared by the kidsout server and
// the kidsoutctl client. Both binaries live in the same module, so a release
// bumps Version here once and both report it.
//
// Version is the source of truth and is bumped by hand together with
// CHANGELOG.md. Commit is stamped at build time by go_build.sh:
//
//	-ldflags "-X kidsout/version.Commit=$(git rev-parse --short HEAD)"
//
// Version can be overridden the same way (-X kidsout/version.Version=...),
// which is handy for pre-release or CI builds.
package version

import (
	"fmt"
	"runtime"
)

var (
	// Version is the semantic version of this source tree.
	Version = "1.0.0"
	// Commit is the short git hash the binary was built from ("none" if unknown).
	Commit = "none"
)

// String renders the one-line banner printed by --version / `version`,
// e.g. "kidsout 1.0.0 (commit 83544b0, go1.25.5 linux/amd64)".
func String(app string) string {
	return fmt.Sprintf("%s %s (commit %s, %s %s/%s)",
		app, Version, Commit, runtime.Version(), runtime.GOOS, runtime.GOARCH)
}

// Info is the JSON shape served on GET /api/version.
type Info struct {
	App       string `json:"app"`
	Version   string `json:"version"`
	Commit    string `json:"commit"`
	GoVersion string `json:"goVersion"`
	OS        string `json:"os"`
	Arch      string `json:"arch"`
}

// Get returns the Info for the given application name.
func Get(app string) Info {
	return Info{
		App:       app,
		Version:   Version,
		Commit:    Commit,
		GoVersion: runtime.Version(),
		OS:        runtime.GOOS,
		Arch:      runtime.GOARCH,
	}
}
