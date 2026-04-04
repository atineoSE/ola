#!/usr/bin/env bash
# Build (and optionally push) the ola template image.
#
# Usage:
#   docker/build.sh              # build locally
#   docker/build.sh --push       # build and push to registry
#
# Override the image name via OLA_SBX_IMAGE:
#   OLA_SBX_IMAGE=myregistry.io/team/ola:v2 docker/build.sh --push
set -euo pipefail

IMAGE="${OLA_SBX_IMAGE:-ola/ola:latest}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

push_flag=""
if [[ "${1:-}" == "--push" ]]; then
  push_flag="--push"
fi

echo "Building $IMAGE ..."
docker build \
  --no-cache \
  -f "$SCRIPT_DIR/Dockerfile" \
  -t "$IMAGE" \
  $push_flag \
  "$PROJECT_DIR"

echo "Done: $IMAGE"

if [ -z "$push_flag" ]; then
  echo "Run with --push to push to the registry."
fi
