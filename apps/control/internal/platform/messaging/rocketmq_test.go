package messaging

import (
	"context"
	"errors"
	"testing"
	"time"

	rmq "github.com/apache/rocketmq-clients/golang/v5"
	v2 "github.com/apache/rocketmq-clients/golang/v5/protocol/v2"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/config"
)

func TestRocketMQRequiresProxyEndpoint(t *testing.T) {
	if _, err := NewRocketMQClient(config.MQConfig{}); err == nil {
		t.Fatal("missing Proxy endpoint must fail before constructing SDK clients")
	}
}

type recordingProducer struct {
	message *rmq.Message
}

func (p *recordingProducer) Start() error        { return nil }
func (p *recordingProducer) GracefulStop() error { return nil }

func (p *recordingProducer) Send(_ context.Context, message *rmq.Message) ([]*rmq.SendReceipt, error) {
	p.message = message
	return nil, nil
}

func TestPublishCommandUsesConfiguredTopicAndPayload(t *testing.T) {
	p := &recordingProducer{}
	c := &RocketMQClient{producer: p, commandTopic: "commands"}
	if err := c.PublishCommand(context.Background(), "command-1", []byte(`{"job_id":"job-1"}`)); err != nil {
		t.Fatal(err)
	}
	if p.message.Topic != "commands" || string(p.message.Body) != `{"job_id":"job-1"}` {
		t.Fatal("command changed during SDK adaptation")
	}
}

type recordingConsumer struct {
	messages []*rmq.MessageView
	acks     int
	err      error
}

func (c *recordingConsumer) Receive(context.Context, int32, time.Duration) ([]*rmq.MessageView, error) {
	return c.messages, c.err
}

func TestEmptyTopicIsNotAProjectionFailure(t *testing.T) {
	c := &recordingConsumer{err: &rmq.ErrRpcStatus{Code: int32(v2.Code_MESSAGE_NOT_FOUND)}}
	client := &RocketMQClient{consumer: c}
	if err := client.ReceiveEvents(context.Background(), func(context.Context, []byte) error { return nil }); err != nil {
		t.Fatal("empty topic reported as a projection error")
	}
}

func (c *recordingConsumer) Start() error        { return nil }
func (c *recordingConsumer) GracefulStop() error { return nil }

func (c *recordingConsumer) Ack(context.Context, *rmq.MessageView) error { c.acks++; return nil }

func TestEventsAreNotAckedIfProjectionFails(t *testing.T) {
	c := &recordingConsumer{messages: []*rmq.MessageView{{}}}
	client := &RocketMQClient{consumer: c}
	err := client.ReceiveEvents(context.Background(), func(context.Context, []byte) error { return errors.New("db unavailable") })
	if err == nil || c.acks != 0 {
		t.Fatal("event was acknowledged before durable projection")
	}
}

func TestPoisonEventDoesNotBlockOtherMessagesInBatch(t *testing.T) {
	c := &recordingConsumer{messages: []*rmq.MessageView{{}, {}}}
	client := &RocketMQClient{consumer: c}
	processed := 0
	err := client.ReceiveEvents(context.Background(), func(context.Context, []byte) error {
		processed++
		if processed == 1 {
			return errors.New("invalid event")
		}
		return nil
	})
	if err == nil || processed != 2 || c.acks != 1 {
		t.Fatal("poison event blocked successful sibling")
	}
}
