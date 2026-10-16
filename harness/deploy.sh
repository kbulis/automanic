#!/usr/bin/env bash
set -euo pipefail

pushd ../harness/services/ && ./push-service.sh --service=./feedback/discord --repository=feedback-discord --version=0.7.0 --latest && popd
pushd ../harness/services/ && ./push-service.sh --service=./shell/bash --repository=shell-bash --version=0.7.0 --latest && popd
pushd ../harness/services/ && ./push-service.sh --service=./analyze/graph --repository=analyze-graph --version=0.7.0 --latest && popd
pushd ../harness/services/ && ./push-service.sh --service=./orchestrator/claudeapi --repository=orchestrator-claudeapi --version=0.7.0 --latest && popd
pushd ../harness/services/ && ./push-service.sh --service=./orchestrator/claudecode --repository=orchestrator-claudecode --version=0.7.0 --latest && popd