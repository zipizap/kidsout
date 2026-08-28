package main

import (
	"context"
	"embed"
	"errors"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

//go:embed web
var webFiles embed.FS

func main() {
	devicesDir := envOr("KIDSOUT_DEVICES_DIR", "devices")
	storePath := envOr("KIDSOUT_RUNTIMESTORE", "runtimestore.yaml")
	listenAddr := envOr("KIDSOUT_LISTEN", ":8080")

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
