#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy — Add Environment Variable to samconfig.toml
#
# Appends a CloudFormation parameter override to the parameter_overrides line
# in samconfig.toml for one or both environments. The variable must also have
# a matching Parameter defined in template.yaml for SAM to accept it.
#
# Usage:
#   ./scripts/add_env_variable.sh <ParamName> <Value> [--env dev|prod|both]
#
# Examples:
#   ./scripts/add_env_variable.sh OpenWeatherApiKey abc123
#   ./scripts/add_env_variable.sh OpenWeatherApiKey abc123 --env dev
#   ./scripts/add_env_variable.sh OpenWeatherApiKey abc123 --env both
#
# Default: adds to both dev and prod.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ─── Args ────────────────────────────────────────────────────────────────────
if [ $# -lt 2 ]; then
  echo -e "${RED}Usage: $0 <ParamName> <Value> [--env dev|prod|both]${NC}"
  echo ""
  echo "  ParamName  — CloudFormation parameter name (must exist in template.yaml)"
  echo "  Value      — The value to set"
  echo "  --env      — Target environment: dev, prod, or both (default: both)"
  exit 1
fi

PARAM_NAME="$1"
PARAM_VALUE="$2"
shift 2

TARGET_ENV="both"
while [[ $# -gt 0 ]]; do
  case $1 in
    --env) TARGET_ENV="$2"; shift 2 ;;
    *)     echo -e "${RED}Unknown option: $1${NC}"; exit 1 ;;
  esac
done

if [[ "$TARGET_ENV" != "dev" && "$TARGET_ENV" != "prod" && "$TARGET_ENV" != "both" ]]; then
  echo -e "${RED}--env must be one of: dev, prod, both${NC}"
  exit 1
fi

# ─── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMCONFIG="$SCRIPT_DIR/../samconfig.toml"

if [ ! -f "$SAMCONFIG" ]; then
  echo -e "${RED}samconfig.toml not found at $SAMCONFIG${NC}"
  exit 1
fi

# ─── Helper: add param to a specific env section ─────────────────────────────
add_param_to_env() {
  local env="$1"
  local section="\\[${env}\\.deploy\\.parameters\\]"

  # Check if the parameter already exists in this env's parameter_overrides
  local current_line
  current_line=$(grep -A 20 "^\[${env}\.deploy\.parameters\]" "$SAMCONFIG" | grep "^parameter_overrides" | head -1)

  if [ -z "$current_line" ]; then
    echo -e "  ${YELLOW}Warning: No parameter_overrides found for [$env.deploy.parameters]${NC}"
    return
  fi

  # Check if param already present
  if echo "$current_line" | grep -q "${PARAM_NAME}="; then
    # Replace existing value
    # Match ParamName=<anything up to next space or end of quote>
    local escaped_name
    escaped_name=$(printf '%s' "$PARAM_NAME" | sed 's/[.[\*^$()+?{|\\]/\\&/g')
    sed -i '' "s/${escaped_name}=[^ \"]*/${PARAM_NAME}=${PARAM_VALUE}/g" "$SAMCONFIG"
    echo -e "  ${env}: ${YELLOW}Updated${NC} ${PARAM_NAME}=${PARAM_VALUE}"
  else
    # Append to existing parameter_overrides (before the closing quote)
    sed -i '' "/^\[${env}\.deploy\.parameters\]/,/^$\|^\[/ s|parameter_overrides = \"\(.*\)\"|parameter_overrides = \"\1 ${PARAM_NAME}=${PARAM_VALUE}\"|" "$SAMCONFIG"
    echo -e "  ${env}: ${GREEN}Added${NC} ${PARAM_NAME}=${PARAM_VALUE}"
  fi
}

# ─── Execute ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Adding parameter override to samconfig.toml${NC}"
echo "  Parameter: $PARAM_NAME"
echo "  Value:     $PARAM_VALUE"
echo ""

if [[ "$TARGET_ENV" == "dev" || "$TARGET_ENV" == "both" ]]; then
  add_param_to_env "dev"
fi

if [[ "$TARGET_ENV" == "prod" || "$TARGET_ENV" == "both" ]]; then
  add_param_to_env "prod"
fi

echo ""
echo -e "${GREEN}Done!${NC} Remember to deploy for changes to take effect:"
if [[ "$TARGET_ENV" == "dev" || "$TARGET_ENV" == "both" ]]; then
  echo "  sam build && sam deploy --config-env dev"
fi
if [[ "$TARGET_ENV" == "prod" || "$TARGET_ENV" == "both" ]]; then
  echo "  sam build && sam deploy --config-env prod"
fi
echo ""
