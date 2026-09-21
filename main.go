package main

import (
	"context"
	"embed"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"kidsout/version"
)

//go:embed web
var webFiles embed.FS

const usageText = `kidsout — parental control of screen time across devices

Usage:
  kidsout [flags]

Flags:
  -version, --version   Print version information and exit
  -h, --help            Show this help

Configuration is taken from the environment:
  KIDSOUT_LISTEN          Address/port to listen on        (default ":8080")
  KIDSOUT_DEVICES_DIR     Folder with one sub-folder per device (default "devices")
  KIDSOUT_RUNTIMESTORE    Where runtime state is persisted (default "runtimestore.yaml")

See README.md for the full administrator guide.
`

func main() {
	showVersion := flag.Bool("version", false, "print version information and exit")
	flag.Usage = func() { fmt.Fprint(flag.CommandLine.Output(), usageText) }
	flag.Parse()
	if *showVersion {
		fmt.Println(version.String("kidsout"))
		return
	}
	if flag.NArg() > 0 {
		fmt.Fprintf(os.Stderr, "kidsout: unexpected argument %q\n\n", flag.Arg(0))
		flag.Usage()
		os.Exit(2)
	}

	devicesDir := envOr("KIDSOUT_DEVICES_DIR", "devices")
	storePath := envOr("KIDSOUT_RUNTIMESTORE", "runtimestore.yaml")
	listenAddr := envOr("KIDSOUT_LISTEN", ":8080")

	log.Print(version.String("kidsout"))
	deviceNames, err := DiscoverDevices(devicesDir)
	if err != nil {
		log.Fatal(err)
	}
	if len(deviceNames) == 0 {
		log.Fatalf("no devices found in %s (each needs getState.sh, block.sh, unblock.sh)", devicesDir)
	}
	log.Printf("devices: %v", deviceNames)

	store, err := LoadStateStore(storePath, deviceNames)
	if err != nil {
		log.Fatal(err)
	}

	hub := NewSSEHub()
	srv := &Server{store: store, hub: hub, authToken: newAuthToken()}
	engine := NewEngine(devicesDir, store, srv.broadcastState)
	srv.engine = engine

	// Ctrl-C (SIGINT) or SIGTERM cancels ctx, stopping the engine and server.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go engine.Run(ctx)

	webRoot, err := fs.Sub(webFiles, "web")
	if err != nil {
		log.Fatal(err)
	}
	mux := http.NewServeMux()
	srv.Routes(mux, http.FileServer(http.FS(webRoot)))

	httpSrv := &http.Server{Addr: listenAddr, Handler: srv.basicAuth(mux)}
	go func() {
		log.Printf("listening on %s", listenAddr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}()

	<-ctx.Done()
	stop() // restore default signal handling: a second Ctrl-C force-quits
	log.Print("shutdown signal received, terminating")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
