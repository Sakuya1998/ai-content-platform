package app

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"
)

type backgroundTask struct {
	name string
	run  func(context.Context)
}

type Application struct {
	logger     *slog.Logger
	handler    http.Handler
	address    string
	background []backgroundTask
	cleanup    []func()
	closeOnce  sync.Once
}

func (a *Application) Run(ctx context.Context) error {
	listener, err := net.Listen("tcp", a.address)
	if err != nil {
		a.Close()
		return errors.New("cannot listen on configured HTTP address")
	}
	return a.Serve(ctx, listener)
}

func (a *Application) Serve(ctx context.Context, listener net.Listener) error {
	ctx, cancel := context.WithCancel(ctx)
	var workers sync.WaitGroup
	for _, task := range a.background {
		workers.Go(func() { task.run(ctx) })
	}
	server := &http.Server{Handler: a.handler, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second}
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-ctx.Done()
		shutdown, stop := context.WithTimeout(context.Background(), 10*time.Second)
		defer stop()
		if err := server.Shutdown(shutdown); err != nil {
			a.logger.Warn("HTTP shutdown timed out")
			_ = server.Close()
		}
	}()
	a.logger.Info("control plane listening", "address", listener.Addr().String())
	err := server.Serve(listener)
	cancel()
	<-shutdownDone
	workers.Wait()
	a.Close()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (a *Application) Close() {
	a.closeOnce.Do(func() {
		for i := len(a.cleanup) - 1; i >= 0; i-- {
			a.cleanup[i]()
		}
	})
}

func periodic(logger *slog.Logger, name string, interval time.Duration, run func(context.Context) error) backgroundTask {
	return backgroundTask{name: name, run: func(ctx context.Context) {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for ctx.Err() == nil {
			if err := run(ctx); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "background recovery failed", "task", name)
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}}
}
