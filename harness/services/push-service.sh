#!/usr/bin/env bash
set -euo pipefail

# Load local .env if present
if [[ -f ".env" ]]; then
    set -a
    source ".env"
    set +a
fi

usage() {
cat <<EOF
Usage:
  $0 --service=SERVICE --repository=REPOSITORY [--version=VERSION] [--latest]
Examples:
  $0 --service=services/feedback-discord --repository=feedback-discord --version=0.7.0
  $0 --service=services/feedback-discord --repository=feedback-discord --version=0.7.0 --latest
Options:
  --service=SERVICE       Local service directory containing Dockerfile and README.md (required)
  --repository=REPOSITORY Docker Hub repository name (required)
  --version=VERSION       Image version
  --latest                Also tag and push :latest
  --help                  Show this help
EOF
exit 1
}

SERVICE=""
REPOSITORY=""
VERSION=""
LATEST=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service=*)
            SERVICE="${1#*=}"
            ;;
        --repository=*)
            REPOSITORY="${1#*=}"
            ;;
        --version=*)
            VERSION="${1#*=}"
            ;;
        --latest)
            LATEST=true
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "! unknown argument: $1"
            usage
            ;;
    esac
    shift
done

if [[ -z "${SERVICE}" ]]; then
    echo "! --service is required"
    usage
fi

if [[ -z "${REPOSITORY}" ]]; then
    echo "! --repository is required"
    usage
fi

if [[ -z "${VERSION}" ]]; then
    echo "! --version is required"
    usage
fi

IMAGE="${DOCKERHUB_NAMESPACE}/${REPOSITORY}"
VERSION_TAG="${IMAGE}:${VERSION}"
LATEST_TAG="${IMAGE}:latest"

# ------------------------------------------------------------
# Validate
# ------------------------------------------------------------

if [[ ! -f "${SERVICE}/Dockerfile" ]]; then
    echo "! dockerfile not found: ${SERVICE}/Dockerfile"
    exit 1
fi

if [[ ! -f "${SERVICE}/README.md" ]]; then
    echo "! readme.md not found: ${SERVICE}/README.md"
    exit 1
fi

for command in docker curl jq; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "! ${command} is required"
        exit 1
    fi
done

# ------------------------------------------------------------
# Build
# ------------------------------------------------------------

echo ". building ${VERSION_TAG}"

if [[ "${LATEST}" == true ]]; then
    docker build -f "${SERVICE}/Dockerfile" -t "${VERSION_TAG}" -t "${LATEST_TAG}" "${SERVICE}"
else
    docker build -f "${SERVICE}/Dockerfile" -t "${VERSION_TAG}" "${SERVICE}"
fi

# ------------------------------------------------------------
# Push version
# ------------------------------------------------------------

echo ". pushing ${VERSION_TAG}"
docker push "${VERSION_TAG}"

# ------------------------------------------------------------
# Push latest
# ------------------------------------------------------------

if [[ "${LATEST}" == true ]]; then
    echo ". pushing ${LATEST_TAG}"
    docker push "${LATEST_TAG}"
fi

# ------------------------------------------------------------
# Docker Hub overview
# ------------------------------------------------------------

if [[ -z "${DOCKERHUB_NAMESPACE:-}" ]]; then
    echo "~ dockerhub namespace is not set"
    echo "~ skipping docker hub overview update"
    exit 0
fi

if [[ -z "${DOCKERHUB_USERNAME:-}" ]]; then
    echo "~ dockerhub username is not set"
    echo "~ skipping docker hub overview update"
    exit 0
fi

if [[ -z "${DOCKERHUB_TOKEN:-}" ]]; then
    echo "~ dockerhub token is not set"
    echo "~ skipping docker hub overview update"
    exit 0
fi

echo ". authenticating with docker hub api"

TOKEN_RESPONSE="$(
    curl -fsS \
        -H "Content-Type: application/json" \
        -X POST \
        -d "$(jq -n \
            --arg identifier "${DOCKERHUB_USERNAME}" \
            --arg secret "${DOCKERHUB_TOKEN}" \
            '{
                identifier: $identifier,
                secret: $secret
            }')" \
        https://hub.docker.com/v2/auth/token
)"

JWT="$(echo "${TOKEN_RESPONSE}" | jq -r '.access_token')"

if [[ -z "${JWT}" || "${JWT}" == "null" ]]; then
    echo "! docker hub authentication failed"
    exit 1
fi

echo ". updating docker hub overview"

curl -fsS \
    -X PATCH \
    "https://hub.docker.com/v2/namespaces/${DOCKERHUB_NAMESPACE}/repositories/${REPOSITORY}" \
    -H "Authorization: Bearer ${JWT}" \
    -H "Content-Type: application/json" \
    --data "$(jq -n \
        --rawfile description "${SERVICE}/README.md" \
        '{ full_description: $description }')" \
    >/dev/null

echo ". image: ${VERSION_TAG}"
echo ". published successfully 🎉"
