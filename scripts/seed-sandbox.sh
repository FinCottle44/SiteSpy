#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy Sandbox Tenant Seed Script
#
# Seeds the hidden sandbox tenant (sandbox_construction) and its default site
# into the dev environment. Cameras can then be provisioned into the sandbox
# for staging/testing before being transferred to a customer tenant via
# POST /v1/cameras/transfer.
#
# The sandbox tenant is hidden from all non-super_admin users (the API enforces
# this via sandbox_visibility_guard). This script just creates the records so
# super_admins can start provisioning cameras immediately.
#
# Idempotent: uses conditional writes (attribute_not_exists). Re-running it
# leaves an existing sandbox tenant/site untouched.
#
# Prerequisites:
#   - AWS CLI configured with profile "sitespy-dev"
#   - The sitespy-dev stack deployed (sam deploy --config-env dev)
#
# Usage:
#   ./scripts/seed-sandbox.sh
#
# What it creates (matches constants in src/sitespy/sandbox.py):
#   DynamoDB (sitespy-dev-data):
#     - Tenant: TENANT#sandbox_construction ("Sandbox Construction")
#     - Site:   SITE#default_sandbox_site ("Default Sandbox Site")
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="sitespy-dev"
REGION="eu-west-2"
TABLE="sitespy-dev-data"

# Sandbox config — MUST match src/sitespy/sandbox.py constants
TENANT_ID="sandbox_construction"
TENANT_NAME="Sandbox Construction"
STALE_THRESHOLD_HOURS="24"
SITE_ID="default_sandbox_site"
SITE_NAME="Default Sandbox Site"
SITE_LATITUDE="-33.8688"
SITE_LONGITUDE="151.2093"
SITE_TIMEZONE="Australia/Sydney"
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  SiteSpy Sandbox Tenant Seed Script${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Sandbox tenant record (conditional — won't overwrite if it exists)
# ─────────────────────────────────────────────────────────────────────────────
echo -n "Creating sandbox tenant ($TENANT_ID)... "
if aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --condition-expression "attribute_not_exists(PK)" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"tenant_name\": {\"S\": \"$TENANT_NAME\"},
    \"stale_threshold_hours\": {\"N\": \"$STALE_THRESHOLD_HOURS\"},
    \"created_at\": {\"S\": \"$CREATED_AT\"}
  }" 2>/dev/null; then
  echo -e "${GREEN}created${NC}"
else
  echo -e "${CYAN}already exists, skipped${NC}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. Default sandbox site record (conditional)
# ─────────────────────────────────────────────────────────────────────────────
echo -n "Creating default sandbox site ($SITE_ID)... "
if aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --condition-expression "attribute_not_exists(PK) AND attribute_not_exists(SK)" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"SITE#$SITE_ID\"},
    \"site_name\": {\"S\": \"$SITE_NAME\"},
    \"latitude\": {\"N\": \"$SITE_LATITUDE\"},
    \"longitude\": {\"N\": \"$SITE_LONGITUDE\"},
    \"timezone\": {\"S\": \"$SITE_TIMEZONE\"},
    \"created_at\": {\"S\": \"$CREATED_AT\"}
  }" 2>/dev/null; then
  echo -e "${GREEN}created${NC}"
else
  echo -e "${CYAN}already exists, skipped${NC}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Sandbox seed complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Data seeded:"
echo "  • Tenant:  $TENANT_ID ($TENANT_NAME)"
echo "  • Site:    $SITE_ID ($SITE_NAME)"
echo ""
echo "Next steps:"
echo "  1. As a super_admin, provision cameras into the sandbox:"
echo "       POST /v1/sites/$SITE_ID/cameras?tenant_id=$TENANT_ID"
echo "  2. Test ingestion, then transfer to a customer tenant:"
echo "       POST /v1/cameras/transfer"
echo ""
echo -e "${CYAN}Note:${NC} the sandbox tenant is hidden from all non-super_admin users."
