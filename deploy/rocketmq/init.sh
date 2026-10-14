#!/bin/sh
set -eu

# This is an administrator job. Application services use the gRPC Proxy only.
nameserver=${MQ_ADMIN_NAMESERVER:-rocketmq-namesrv:9876}
cluster=${MQ_ADMIN_CLUSTER:-DefaultCluster}
environment=${MQ_ENVIRONMENT:-dev}
queues=${MQ_QUEUE_COUNT:-4}
retries=${MQ_RETRY_MAX_TIMES:-16}
admin=${ROCKETMQ_HOME:?ROCKETMQ_HOME is required}/bin/mqadmin

admin_retry() {
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        output=$(sh "$admin" "$@" 2>&1) || output="$output"
        printf '%s\n' "$output"
        # mqadmin may return exit code 0 even when the cluster has no brokers.
        case "$output" in
            *"create topic to "*" success."*|*"create subscription group to "*" success."*) return 0 ;;
        esac
        attempt=$((attempt + 1))
        sleep 2
    done
    return 1
}

for topic in \
    "${CONTROL_PLANE_MQ_TOPICS_TASKS:-agent-$environment-runtime-tasks}" \
    "${CONTROL_PLANE_MQ_TOPICS_CONTROL:-agent-$environment-runtime-control}" \
    "${CONTROL_PLANE_MQ_TOPICS_EVENTS:-agent-$environment-runtime-events}" \
    "${CONTROL_PLANE_MQ_TOPICS_MEMORY:-agent-$environment-memory-tasks}"
do
    admin_retry updateTopic -n "$nameserver" -c "$cluster" -t "$topic" \
        -r "$queues" -w "$queues" -a '+message.type=NORMAL'
done

for group in \
    "${CONTROL_PLANE_MQ_GROUPS_PROJECTION:-agent-$environment-control-projection}" \
    "${RUNTIME_ROCKETMQ_DISPATCH_GROUP:-agent-$environment-runtime-dispatch}" \
    "${RUNTIME_ROCKETMQ_CONTROL_GROUP:-agent-$environment-runtime-control}" \
    "${RUNTIME_ROCKETMQ_MEMORY_GROUP:-agent-$environment-memory-worker}" \
    "${RUNTIME_ROCKETMQ_MEMORY_SCHEDULER_GROUP:-agent-$environment-memory-scheduler}"
do
    admin_retry updateSubGroup -n "$nameserver" -c "$cluster" -g "$group" -r "$retries"
done
