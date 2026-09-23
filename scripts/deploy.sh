#!/usr/bin/env bash
# deploy.sh — Deploy the Weather App CDK stack to a target environment.
#
# Usage:
#   ./scripts/deploy.sh [dev|prod]
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - CDK CLI installed  (`npm install -g aws-cdk`)
#   - Python virtual-env activated with infrastructure/requirements.txt installed
#   - CDK already bootstrapped in target account/region (`cdk bootstrap`)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENV=${1:-dev}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infrastructure"

# ---------------------------------------------------------------------------
# Validate environment argument
# ---------------------------------------------------------------------------
if [[ "${ENV}" != "dev" && "${ENV}" != "prod" ]]; then
  echo "ERROR: Unknown environment '${ENV}'. Valid values: dev, prod" >&2
  exit 1
fi

echo "==> Target environment: ${ENV}"

# ---------------------------------------------------------------------------
# Validate required environment variables
# ---------------------------------------------------------------------------
: "${AWS_REGION:?AWS_REGION must be set}"
: "${CDK_DEFAULT_ACCOUNT:?CDK_DEFAULT_ACCOUNT must be set}"

echo "==> AWS account : ${CDK_DEFAULT_ACCOUNT}"
echo "==> AWS region  : ${AWS_REGION}"

# ---------------------------------------------------------------------------
# Confirm production deployments interactively
# ---------------------------------------------------------------------------
if [[ "${ENV}" == "prod" ]]; then
  read -r -p "You are about to deploy to PRODUCTION. Continue? [y/N] " confirm
  if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    echo "Aborted."
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Install CDK dependencies
# ---------------------------------------------------------------------------
echo "==> Installing infrastructure dependencies…"
pip install -r "${INFRA_DIR}/requirements.txt" -q

# ---------------------------------------------------------------------------
# Synthesise to validate the stack before deploying
# ---------------------------------------------------------------------------
echo "==> Synthesising CDK stack…"
(cd "${INFRA_DIR}" && cdk synth --context env="${ENV}" --quiet)

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
echo "==> Deploying CDK stack (env=${ENV})…"

if [[ "${ENV}" == "dev" ]]; then
  (cd "${INFRA_DIR}" && cdk deploy \
    --context env="${ENV}" \
    --require-approval never \
    --outputs-file "${REPO_ROOT}/cdk-outputs-${ENV}.json")
else
  (cd "${INFRA_DIR}" && cdk deploy \
    --context env="${ENV}" \
    --outputs-file "${REPO_ROOT}/cdk-outputs-${ENV}.json")
fi

echo "==> Deployment complete. Outputs written to cdk-outputs-${ENV}.json"
