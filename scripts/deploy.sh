#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy deployment script
#
# Usage:
#   ./scripts/deploy.sh dev          # Deploy to dev (no confirmation needed)
#   ./scripts/deploy.sh prod         # Deploy to prod (interactive confirmation required)
#
# Safety:
#   - Prod deployments require an interactive terminal (TTY)
#   - Prod deployments require you to type "deploy prod" to confirm
#   - This prevents agents, CI pipelines, or scripts from accidentally
#     deploying to production without explicit human approval
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV="${1:-}"

if [[ -z "$ENV" ]]; then
  echo "Usage: $0 <dev|prod>"
  echo ""
  echo "  dev   — Deploy to development (fast, no confirmation)"
  echo "  prod  — Deploy to production (requires interactive confirmation)"
  exit 1
fi

if [[ "$ENV" != "dev" && "$ENV" != "prod" ]]; then
  echo "ERROR: Environment must be 'dev' or 'prod', got '$ENV'"
  exit 1
fi

cd "$PROJECT_ROOT"

# ─────────────────────────────────────────────────────────────────────────────
# PROD SAFETY GATE
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$ENV" == "prod" ]]; then
  # Block non-interactive sessions (agents, piped scripts, CI without approval)
  if [[ ! -t 0 ]]; then
    echo "═══════════════════════════════════════════════════════════════════"
    echo "  ⛔  PRODUCTION DEPLOYMENT BLOCKED"
    echo ""
    echo "  Prod deployments require an interactive terminal."
    echo "  This prevents automated tools and agents from deploying to prod."
    echo ""
    echo "  Run this script directly in your terminal:"
    echo "    ./scripts/deploy.sh prod"
    echo "═══════════════════════════════════════════════════════════════════"
    exit 1
  fi

  echo "═══════════════════════════════════════════════════════════════════"
  echo "  ⚠️   PRODUCTION DEPLOYMENT"
  echo ""
  echo "  Stack:   sitespy-prod"
  echo "  Region:  eu-west-2"
  echo "  Domain:  api.sitespy.io"
  echo ""
  echo "  This will deploy to the PRODUCTION environment."
  echo "  DynamoDB has deletion protection enabled."
  echo "  S3 bucket and DynamoDB table use DeletionPolicy: Retain."
  echo "═══════════════════════════════════════════════════════════════════"
  echo ""
  read -rp "  Type 'deploy prod' to confirm: " CONFIRM

  if [[ "$CONFIRM" != "deploy prod" ]]; then
    echo ""
    echo "  Deployment cancelled."
    exit 1
  fi

  echo ""
  echo "  ✓ Confirmed. Deploying to production..."
  echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────
echo "→ Building ($ENV)..."
sam build --config-env "$ENV"

# ─────────────────────────────────────────────────────────────────────────────
# Deploy
# ─────────────────────────────────────────────────────────────────────────────
echo "→ Deploying ($ENV)..."
sam deploy --config-env "$ENV"

echo ""
echo "✓ Deployment to $ENV complete."

if [[ "$ENV" == "prod" ]]; then
  echo ""
  echo "  API available at: https://api.sitespy.io/v1/"
  echo "  (Also at the execute-api URL shown in stack outputs)"
fi
