package messaging

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

type Repository interface {
	Scopes(context.Context) ([]string, error)
	Relay(context.Context, string, func(context.Context, string, string, string, []byte) error) error
	Project(context.Context, []byte) error
}

// RunMessaging runs independent relay and projection loops until shutdown.
func RunMessaging(ctx context.Context, repository Repository, client *RocketMQClient, logger *slog.Logger) {
	var workers sync.WaitGroup
	workers.Add(2)
	go func() {
		defer workers.Done()
		for ctx.Err() == nil {
			scopes, err := repository.Scopes(ctx)
			if err != nil {
				if ctx.Err() == nil {
					logger.WarnContext(ctx, "MQ scope discovery failed")
				}
			} else {
				for _, scope := range scopes {
					if err = repository.Relay(ctx, scope, client.Publish); err != nil && ctx.Err() == nil {
						logger.WarnContext(ctx, "MQ command relay failed")
					}
				}
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
			}
		}
	}()
	go func() {
		defer workers.Done()
		for ctx.Err() == nil {
			if err := client.ReceiveEvents(ctx, repository.Project); err != nil && ctx.Err() == nil {
				logger.WarnContext(ctx, "MQ event projection failed; message remains unacknowledged", "error", err)
				select {
				case <-ctx.Done():
					return
				case <-time.After(time.Second):
				}
			}
		}
	}()
	workers.Wait()
}
