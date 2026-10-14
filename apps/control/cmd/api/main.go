package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/sakuya/novel-agent/apps/control/internal/app"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/config"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/logger"
)

func main() {
	os.Exit(run())
}

func run() int {
	log := logger.New(os.Stderr)
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	cfg, err := config.Load()
	if err != nil {
		log.Error("invalid control-plane configuration")
		return 1
	}
	application, err := app.New(ctx, cfg, log)
	if err != nil {
		log.Error("control-plane initialization failed", "reason", err.Error())
		return 1
	}
	if err := application.Run(ctx); err != nil {
		log.Error("control-plane server failed")
		return 1
	}
	return 0
}
