// mq-check starts both Proxy SDK clients without sending or receiving messages.
package main

import (
	"fmt"
	"os"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/messaging"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid control-plane configuration")
		os.Exit(1)
	}
	client, err := messaging.NewRocketMQClient(cfg.MQ)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid RocketMQ Proxy configuration")
		os.Exit(1)
	}
	if err := client.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "RocketMQ Proxy SDK startup failed")
		os.Exit(1)
	}
	defer client.Close()
	fmt.Println("RocketMQ Proxy Go SDK clients started")
}
