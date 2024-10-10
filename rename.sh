#!/bin/bash

if [[ -f "rabbitmq-k8s.charm" ]]; then
    echo "Removing existing rabbitmq-k8s.charm"
    rm rabbitmq-k8s.charm
fi

for f in rabbitmq-k8s*.charm; do
    echo "Renaming $f to rabbitmq-k8s.charm"
    mv "$f" "rabbitmq-k8s.charm"
    break
done
