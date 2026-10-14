package messaging

import (
	"context"
	"errors"
	"time"

	rmq "github.com/apache/rocketmq-clients/golang/v5"
	"github.com/apache/rocketmq-clients/golang/v5/credentials"
	v2 "github.com/apache/rocketmq-clients/golang/v5/protocol/v2"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
)

// RocketMQClient connects to the gRPC Proxy, never to NameServer.
type RocketMQClient struct {
	producer     proxyProducer
	consumer     proxyConsumer
	commandTopic string
	controlTopic string
}

type proxyProducer interface {
	Send(context.Context, *rmq.Message) ([]*rmq.SendReceipt, error)
	Start() error
	GracefulStop() error
}

type proxyConsumer interface {
	Receive(context.Context, int32, time.Duration) ([]*rmq.MessageView, error)
	Ack(context.Context, *rmq.MessageView) error
	Start() error
	GracefulStop() error
}

func NewRocketMQClient(cfg config.MQConfig) (*RocketMQClient, error) {
	if cfg.Endpoint == "" {
		return nil, errors.New("RocketMQ Proxy endpoint is required")
	}
	// The upstream Go SDK configures TLS process-wide; use one TLS policy per process.
	rmq.EnableSsl = cfg.TLSEnabled
	sdkConfig := &rmq.Config{
		Endpoint: cfg.Endpoint, NameSpace: cfg.Namespace, ConsumerGroup: cfg.Groups.Projection,
		Credentials: &credentials.SessionCredentials{AccessKey: cfg.AccessKey, AccessSecret: cfg.SecretKey},
	}
	producer, err := rmq.NewProducer(sdkConfig, rmq.WithTopics(cfg.Topics.Tasks, cfg.Topics.Control))
	if err != nil {
		return nil, err
	}
	consumer, err := rmq.NewSimpleConsumer(sdkConfig,
		rmq.WithSimpleAwaitDuration(5*time.Second),
		rmq.WithSimpleSubscriptionExpressions(map[string]*rmq.FilterExpression{cfg.Topics.Events: rmq.SUB_ALL}),
	)
	if err != nil {
		_ = producer.GracefulStop()
		return nil, err
	}
	return &RocketMQClient{producer: producer, consumer: consumer, commandTopic: cfg.Topics.Tasks, controlTopic: cfg.Topics.Control}, nil
}

func (c *RocketMQClient) Start() error {
	if err := c.producer.Start(); err != nil {
		return err
	}
	if err := c.consumer.Start(); err != nil {
		_ = c.Close()
		return err
	}
	return nil
}

func (c *RocketMQClient) Close() error {
	return errors.Join(c.consumer.GracefulStop(), c.producer.GracefulStop())
}

func (c *RocketMQClient) PublishCommand(ctx context.Context, commandID string, payload []byte) error {
	message := &rmq.Message{Topic: c.commandTopic, Body: payload}
	message.SetKeys(commandID)
	_, err := c.producer.Send(ctx, message)
	return err
}

func (c *RocketMQClient) Publish(ctx context.Context, topic, key, tag string, payload []byte) error {
	if topic != c.commandTopic && topic != c.controlTopic {
		return errors.New("unexpected command topic")
	}
	message := &rmq.Message{Topic: topic, Body: payload}
	message.SetKeys(key)
	message.SetTag(tag)
	_, err := c.producer.Send(ctx, message)
	return err
}

// ProjectEvent must persist its inbox/projection before returning success.
func (c *RocketMQClient) ReceiveEvents(ctx context.Context, projectEvent func(context.Context, []byte) error) error {
	messages, err := c.consumer.Receive(ctx, 16, 60*time.Second)
	if err != nil {
		if status, ok := rmq.AsErrRpcStatus(err); ok && status.GetCode() == int32(v2.Code_MESSAGE_NOT_FOUND) {
			return nil
		}
		return err
	}
	var failures []error
	for _, message := range messages {
		if err := projectEvent(ctx, message.GetBody()); err != nil {
			failures = append(failures, err)
			continue
		}
		if err := c.consumer.Ack(ctx, message); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}
