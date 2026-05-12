#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy Dev Seed Script
#
# Seeds the dev environment with a clean tenant, site, camera, and users.
# Run this after nuke-dev.sh to get a fresh working environment.
#
# Prerequisites:
#   - AWS CLI configured with profile "sitespy-dev"
#   - The sitespy-dev stack deployed (sam deploy --config-env dev)
#
# Usage:
#   ./scripts/seed-dev.sh
#
# What it creates:
#   DynamoDB (sitespy-dev-data):
#     - Tenant: TENANT#red_construction
#     - Site: SITE#main_site ("Red Construction - Main Site")
#     - Camera: cam_01 ("Front Gate", Axis P1455-LE)
#
#   Cognito (eu-west-2_hSZjNVtPO):
#     - Super admin: fin@cottlecc.com (SuperAdmins group)
#     - Tenant admin: admin@red.test (TenantAdmins group, tenant=red_construction)
#     - Regular user: viewer@red.test (tenant=red_construction, site_access=main_site)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="sitespy-dev"
REGION="eu-west-2"
TABLE="sitespy-dev-data"
USER_POOL_ID="eu-west-2_hSZjNVtPO"
TEMP_PASSWORD="TempPass123!"

# Tenant config
TENANT_ID="red_construction"
TENANT_NAME="Red Construction Ltd"
SITE_ID="main_site"
SITE_NAME="Red Construction - Main Site"
CAMERA_ID="cam_01"
CAMERA_NAME="Front Gate"
CAMERA_MODEL="Axis P1455-LE"
INGEST_TOKEN="tk_$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9_-' | head -c 40)"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  SiteSpy Dev Seed Script${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Tenant record
# ─────────────────────────────────────────────────────────────────────────────
echo -n "Creating tenant ($TENANT_ID)... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"tenant_name\": {\"S\": \"$TENANT_NAME\"},
    \"retention_years\": {\"N\": \"5\"},
    \"stale_threshold_hours\": {\"N\": \"24\"}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Site record
# ─────────────────────────────────────────────────────────────────────────────
echo -n "Creating site ($SITE_ID)... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"SITE#$SITE_ID\"},
    \"site_name\": {\"S\": \"$SITE_NAME\"},
    \"latitude\": {\"N\": \"51.5074\"},
    \"longitude\": {\"N\": \"-0.1278\"},
    \"timezone\": {\"S\": \"Europe/London\"}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Camera record
# ─────────────────────────────────────────────────────────────────────────────
echo -n "Creating camera ($CAMERA_ID - $CAMERA_NAME)... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"SITE#${SITE_ID}#CAM#${CAMERA_ID}\"},
    \"camera_name\": {\"S\": \"$CAMERA_NAME\"},
    \"camera_model\": {\"S\": \"$CAMERA_MODEL\"},
    \"GSI1PK\": {\"S\": \"TOKEN#$INGEST_TOKEN\"},
    \"GSI1SK\": {\"S\": \"CAMERA\"},
    \"ingest_token\": {\"S\": \"$INGEST_TOKEN\"}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Cognito users
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Creating Cognito users...${NC}"

# --- Super Admin ---
echo -n "  Creating super admin (fin@cottlecc.com)... "
SUPER_ADMIN_SUB=$(aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "fin@cottlecc.com" \
  --temporary-password "$TEMP_PASSWORD" \
  --user-attributes \
    Name=email,Value=fin@cottlecc.com \
    Name=email_verified,Value=true \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'User.Username' \
  --output text 2>/dev/null || echo "existing")
echo -e "${GREEN}done${NC}"

echo -n "  Adding to SuperAdmins group... "
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "fin@cottlecc.com" \
  --group-name "SuperAdmins" \
  --region "$REGION" \
  --profile "$PROFILE" 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# --- Tenant Admin ---
echo -n "  Creating tenant admin (admin@red.test)... "
TENANT_ADMIN_SUB=$(aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "admin@red.test" \
  --temporary-password "$TEMP_PASSWORD" \
  --user-attributes \
    Name=email,Value=admin@red.test \
    Name=email_verified,Value=true \
    Name=custom:tenant_id,Value=$TENANT_ID \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'User.Username' \
  --output text 2>/dev/null || echo "existing")
echo -e "${GREEN}done${NC}"

echo -n "  Adding to TenantAdmins group... "
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "admin@red.test" \
  --group-name "TenantAdmins" \
  --region "$REGION" \
  --profile "$PROFILE" 2>/dev/null || true
echo -e "${GREEN}done${NC}"

# --- Regular User ---
echo -n "  Creating regular user (viewer@red.test)... "
VIEWER_SUB=$(aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "viewer@red.test" \
  --temporary-password "$TEMP_PASSWORD" \
  --user-attributes \
    Name=email,Value=viewer@red.test \
    Name=email_verified,Value=true \
    Name=custom:tenant_id,Value=$TENANT_ID \
    Name=custom:site_access,Value=$SITE_ID \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'User.Username' \
  --output text 2>/dev/null || echo "existing")
echo -e "${GREEN}done${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# 5. User_Record items in DynamoDB (mirrors what POST /v1/users writes)
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Writing User_Records to DynamoDB...${NC}"

# Look up actual subs if users already existed
if [ "$SUPER_ADMIN_SUB" = "existing" ]; then
  SUPER_ADMIN_SUB=$(aws cognito-idp admin-get-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "fin@cottlecc.com" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'Username' \
    --output text 2>/dev/null)
fi
if [ "$TENANT_ADMIN_SUB" = "existing" ]; then
  TENANT_ADMIN_SUB=$(aws cognito-idp admin-get-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "admin@red.test" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'Username' \
    --output text 2>/dev/null)
fi
if [ "$VIEWER_SUB" = "existing" ]; then
  VIEWER_SUB=$(aws cognito-idp admin-get-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "viewer@red.test" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query 'Username' \
    --output text 2>/dev/null)
fi

# Super admin user record
echo -n "  Writing super admin user record... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"USER#$SUPER_ADMIN_SUB\"},
    \"sub\": {\"S\": \"$SUPER_ADMIN_SUB\"},
    \"email\": {\"S\": \"fin@cottlecc.com\"},
    \"full_name\": {\"S\": \"Fin Cottle\"},
    \"tenant_id\": {\"S\": \"$TENANT_ID\"},
    \"role\": {\"S\": \"super_admin\"},
    \"site_access\": {\"L\": []}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# Tenant admin user record
echo -n "  Writing tenant admin user record... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"USER#$TENANT_ADMIN_SUB\"},
    \"sub\": {\"S\": \"$TENANT_ADMIN_SUB\"},
    \"email\": {\"S\": \"admin@red.test\"},
    \"full_name\": {\"S\": \"Red Admin\"},
    \"tenant_id\": {\"S\": \"$TENANT_ID\"},
    \"role\": {\"S\": \"tenant_admin\"},
    \"site_access\": {\"L\": []}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# Regular user record
echo -n "  Writing regular user record... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$TENANT_ID\"},
    \"SK\": {\"S\": \"USER#$VIEWER_SUB\"},
    \"sub\": {\"S\": \"$VIEWER_SUB\"},
    \"email\": {\"S\": \"viewer@red.test\"},
    \"full_name\": {\"S\": \"Red Viewer\"},
    \"tenant_id\": {\"S\": \"$TENANT_ID\"},
    \"role\": {\"S\": \"user\"},
    \"site_access\": {\"L\": [{\"S\": \"$SITE_ID\"}]}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Seed complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Data seeded:"
echo "  • Tenant:  $TENANT_ID ($TENANT_NAME)"
echo "  • Site:    $SITE_ID ($SITE_NAME)"
echo "  • Camera:  $CAMERA_ID ($CAMERA_NAME, $CAMERA_MODEL)"
echo "  • Users:   3 User_Records in DynamoDB"
echo ""
echo "Cognito users (all temp password: $TEMP_PASSWORD):"
echo "  • fin@cottlecc.com   → SuperAdmin (sub: $SUPER_ADMIN_SUB)"
echo "  • admin@red.test     → TenantAdmin ($TENANT_ID) (sub: $TENANT_ADMIN_SUB)"
echo "  • viewer@red.test    → User ($TENANT_ID, site: $SITE_ID) (sub: $VIEWER_SUB)"
echo ""
echo -e "${CYAN}Camera ingest token:${NC}"
echo "  $INGEST_TOKEN"
echo ""
echo -e "${CYAN}Ingest URL:${NC}"
API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name sitespy-dev \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
  --output text 2>/dev/null || echo "https://<api-id>.execute-api.eu-west-2.amazonaws.com/prod")
echo "  POST ${API_ENDPOINT}/v1/ingest/${INGEST_TOKEN}"
echo ""
echo "Next steps:"
echo "  1. Deploy the latest code: sam build && sam deploy --config-env dev"
echo "  2. Log in as fin@cottlecc.com (change password on first login)"
echo "  3. Point your camera at the ingest URL above"
