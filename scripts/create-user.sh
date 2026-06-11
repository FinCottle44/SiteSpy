#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy — Interactive User Creation (bypass SES)
#
# Walks you through creating a Cognito user with menus for tenant, site, and
# role selection — pulling live data from DynamoDB and Cognito.
#
# The email invitation is suppressed. You share the temp credentials manually.
# The user sets their own password on first login.
#
# Usage:
#   ./scripts/create-user.sh          # defaults to prod
#   ./scripts/create-user.sh --env dev
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ─── Parse args ──────────────────────────────────────────────────────────────
ENV="prod"
while [[ $# -gt 0 ]]; do
  case $1 in
    --env) ENV="$2"; shift 2 ;;
    *)     echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ─── Config per environment ──────────────────────────────────────────────────
if [ "$ENV" = "prod" ]; then
  PROFILE="sitespy-dev"
  USER_POOL_ID="eu-west-2_XXwnO0sjc"
  TABLE="sitespy-prod-data"
elif [ "$ENV" = "dev" ]; then
  PROFILE="sitespy-dev"
  USER_POOL_ID="eu-west-2_hSZjNVtPO"
  TABLE="sitespy-dev-data"
else
  echo "Error: --env must be 'dev' or 'prod'"
  exit 1
fi

REGION="eu-west-2"

# ─── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Helpers ─────────────────────────────────────────────────────────────────
prompt_choice() {
  # Usage: prompt_choice "Header" "option1" "option2" ...
  # Sets CHOICE to the selected value (1-indexed)
  local header="$1"; shift
  local options=("$@")
  local count=${#options[@]}

  echo ""
  echo -e "${CYAN}${header}${NC}"
  echo -e "${DIM}─────────────────────────────────────────${NC}"
  for i in "${!options[@]}"; do
    printf "  ${BOLD}%d)${NC} %s\n" $((i + 1)) "${options[$i]}"
  done
  echo ""

  while true; do
    read -rp "  Choose [1-$count]: " selection
    if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le "$count" ]; then
      CHOICE="${options[$((selection - 1))]}"
      return
    fi
    echo -e "  ${RED}Invalid choice. Enter a number between 1 and $count.${NC}"
  done
}

prompt_multi_choice() {
  # Usage: prompt_multi_choice "Header" "option1" "option2" ...
  # Sets CHOICES array to selected values (comma-separated input)
  local header="$1"; shift
  local options=("$@")
  local count=${#options[@]}

  echo ""
  echo -e "${CYAN}${header}${NC}"
  echo -e "${DIM}─────────────────────────────────────────${NC}"
  for i in "${!options[@]}"; do
    printf "  ${BOLD}%d)${NC} %s\n" $((i + 1)) "${options[$i]}"
  done
  echo ""
  echo -e "  ${DIM}Enter numbers separated by commas, or 'all' for all, or 'none' to skip${NC}"

  while true; do
    read -rp "  Choose: " selection

    if [ "$selection" = "none" ]; then
      CHOICES=()
      return
    fi

    if [ "$selection" = "all" ]; then
      CHOICES=("${options[@]}")
      return
    fi

    # Parse comma-separated numbers
    IFS=',' read -ra nums <<< "$selection"
    CHOICES=()
    local valid=true
    for num in "${nums[@]}"; do
      num=$(echo "$num" | tr -d ' ')
      if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -ge 1 ] && [ "$num" -le "$count" ]; then
        CHOICES+=("${options[$((num - 1))]}")
      else
        valid=false
        break
      fi
    done

    if [ "$valid" = true ] && [ ${#CHOICES[@]} -gt 0 ]; then
      return
    fi
    echo -e "  ${RED}Invalid input. Use numbers 1-$count separated by commas.${NC}"
  done
}

# ─── Banner ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  SiteSpy — Create User ${DIM}($ENV)${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"

# ─── 1. Email ────────────────────────────────────────────────────────────────
echo ""
read -rp "$(echo -e "${CYAN}Email address:${NC} ")" EMAIL

if [ -z "$EMAIL" ]; then
  echo -e "${RED}Error: email is required${NC}"
  exit 1
fi

# ─── 2. Full name ───────────────────────────────────────────────────────────
DEFAULT_NAME="${EMAIL%%@*}"
read -rp "$(echo -e "${CYAN}Full name${NC} ${DIM}[$DEFAULT_NAME]:${NC} ")" FULL_NAME
FULL_NAME="${FULL_NAME:-$DEFAULT_NAME}"

# ─── 3. Role ────────────────────────────────────────────────────────────────
prompt_choice "Select role" "user — Regular user (site-level access)" "tenant_admin — Tenant admin (manages users & sites)" "super_admin — Super admin (full platform access)"

# Extract role key from the choice
ROLE="${CHOICE%% —*}"

# ─── 4. Tenant ───────────────────────────────────────────────────────────────
TENANT=""
if [ "$ROLE" != "super_admin" ]; then
  echo ""
  echo -e "${DIM}  Fetching tenants from DynamoDB...${NC}"

  # Query all TENANT# records
  TENANT_DATA=$(aws dynamodb scan \
    --table-name "$TABLE" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --filter-expression "begins_with(PK, :pk) AND begins_with(SK, :sk)" \
    --expression-attribute-values '{":pk":{"S":"TENANT#"}, ":sk":{"S":"TENANT#"}}' \
    --projection-expression "PK, tenant_name" \
    --output json 2>/dev/null)

  # Parse into arrays
  TENANT_IDS=()
  TENANT_LABELS=()
  while IFS= read -r line; do
    tid=$(echo "$line" | cut -d'|' -f1)
    tname=$(echo "$line" | cut -d'|' -f2)
    TENANT_IDS+=("$tid")
    TENANT_LABELS+=("$tid — $tname")
  done < <(echo "$TENANT_DATA" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('Items', []):
    pk = item['PK']['S'].replace('TENANT#', '')
    name = item.get('tenant_name', {}).get('S', '(unnamed)')
    print(f'{pk}|{name}')
" 2>/dev/null)

  if [ ${#TENANT_IDS[@]} -eq 0 ]; then
    echo -e "${RED}  No tenants found in $TABLE. Create a tenant first.${NC}"
    exit 1
  fi

  prompt_choice "Select tenant" "${TENANT_LABELS[@]}"
  TENANT="${CHOICE%% —*}"
fi

# ─── 5. Sites (for regular users) ───────────────────────────────────────────
SITES=""
SITE_IDS=()
if [ "$ROLE" = "user" ] && [ -n "$TENANT" ]; then
  echo ""
  echo -e "${DIM}  Fetching sites for tenant '$TENANT'...${NC}"

  SITE_DATA=$(aws dynamodb query \
    --table-name "$TABLE" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --key-condition-expression "PK = :pk AND begins_with(SK, :sk)" \
    --expression-attribute-values "{\":pk\":{\"S\":\"TENANT#$TENANT\"}, \":sk\":{\"S\":\"SITE#\"}}" \
    --projection-expression "SK, site_name" \
    --output json 2>/dev/null)

  SITE_IDS_ALL=()
  SITE_LABELS=()
  while IFS= read -r line; do
    sid=$(echo "$line" | cut -d'|' -f1)
    sname=$(echo "$line" | cut -d'|' -f2)
    # Skip camera sub-records (SITE#x#CAM#y)
    if [[ "$sid" != *"#CAM#"* ]]; then
      SITE_IDS_ALL+=("$sid")
      SITE_LABELS+=("$sid — $sname")
    fi
  done < <(echo "$SITE_DATA" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('Items', []):
    sk = item['SK']['S'].replace('SITE#', '')
    name = item.get('site_name', {}).get('S', '(unnamed)')
    print(f'{sk}|{name}')
" 2>/dev/null)

  if [ ${#SITE_IDS_ALL[@]} -gt 0 ]; then
    prompt_multi_choice "Select sites this user can access" "${SITE_LABELS[@]}"

    for choice in "${CHOICES[@]}"; do
      SITE_IDS+=("${choice%% —*}")
    done

    # Build comma-separated string
    SITES=$(IFS=','; echo "${SITE_IDS[*]}")
  else
    echo -e "  ${DIM}No sites found for this tenant. Skipping site assignment.${NC}"
  fi
fi

# ─── Confirmation ────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}─────────────────────────────────────────────────────────${NC}"
echo -e "${BOLD}  Review:${NC}"
echo -e "    Email:   $EMAIL"
echo -e "    Name:    $FULL_NAME"
echo -e "    Role:    $ROLE"
[ -n "$TENANT" ] && echo -e "    Tenant:  $TENANT"
[ -n "$SITES" ]  && echo -e "    Sites:   $SITES"
echo -e "${YELLOW}─────────────────────────────────────────────────────────${NC}"
echo ""
read -rp "$(echo -e "  ${BOLD}Create this user? [Y/n]:${NC} ")" CONFIRM
CONFIRM="${CONFIRM:-Y}"

if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo -e "  ${DIM}Cancelled.${NC}"
  exit 0
fi

# ─── Generate temp password ─────────────────────────────────────────────────
TEMP_PASSWORD="$(openssl rand -base64 16 | tr -dc 'A-Za-z0-9' | head -c 12)$(openssl rand -base64 4 | tr -dc '0-9' | head -c 2)!A"

# ─── Map role to Cognito group ───────────────────────────────────────────────
case "$ROLE" in
  super_admin)  COGNITO_GROUP="SuperAdmins" ;;
  tenant_admin) COGNITO_GROUP="TenantAdmins" ;;
  user)         COGNITO_GROUP="" ;;
esac

# ─── Build user attributes ───────────────────────────────────────────────────
USER_ATTRS="Name=email,Value=$EMAIL Name=email_verified,Value=true"

if [ -n "$TENANT" ]; then
  USER_ATTRS="$USER_ATTRS Name=custom:tenant_id,Value=$TENANT"
fi

if [ -n "$SITES" ]; then
  USER_ATTRS="$USER_ATTRS Name=custom:site_access,Value=$SITES"
fi

# ─── Create ──────────────────────────────────────────────────────────────────
echo ""
echo -n "  Creating Cognito user... "
SUB=$(aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --temporary-password "$TEMP_PASSWORD" \
  --message-action SUPPRESS \
  --user-attributes $USER_ATTRS \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'User.Username' \
  --output text 2>&1)

if [ $? -ne 0 ]; then
  echo -e "${RED}failed${NC}"
  echo "  $SUB"
  exit 1
fi
echo -e "${GREEN}done${NC}"

# Add to group
if [ -n "$COGNITO_GROUP" ]; then
  echo -n "  Adding to group ($COGNITO_GROUP)... "
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id "$USER_POOL_ID" \
    --username "$EMAIL" \
    --group-name "$COGNITO_GROUP" \
    --region "$REGION" \
    --profile "$PROFILE" 2>/dev/null
  echo -e "${GREEN}done${NC}"
fi

# Write DynamoDB record
RECORD_TENANT="${TENANT:-global}"
SITE_ACCESS_ITEMS=""
if [ ${#SITE_IDS[@]} -gt 0 ]; then
  for site in "${SITE_IDS[@]}"; do
    SITE_ACCESS_ITEMS="${SITE_ACCESS_ITEMS}{\"S\": \"$site\"},"
  done
  SITE_ACCESS_ITEMS="${SITE_ACCESS_ITEMS%,}"
fi

echo -n "  Writing User_Record to DynamoDB... "
aws dynamodb put-item \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --item "{
    \"PK\": {\"S\": \"TENANT#$RECORD_TENANT\"},
    \"SK\": {\"S\": \"USER#$SUB\"},
    \"sub\": {\"S\": \"$SUB\"},
    \"email\": {\"S\": \"$EMAIL\"},
    \"full_name\": {\"S\": \"$FULL_NAME\"},
    \"tenant_id\": {\"S\": \"$RECORD_TENANT\"},
    \"role\": {\"S\": \"$ROLE\"},
    \"site_access\": {\"L\": [$SITE_ACCESS_ITEMS]}
  }" 2>/dev/null
echo -e "${GREEN}done${NC}"

# ─── Output credentials ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ User created!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}Send this to the user:${NC}"
echo ""
echo -e "  ┌─────────────────────────────────────────────────────────┐"
echo -e "  │                                                         │"
echo -e "  │  Login URL: ${CYAN}https://app.sitespy.io${NC}                     │"
echo -e "  │  Email:     ${CYAN}$EMAIL${NC}"
echo -e "  │  Password:  ${CYAN}$TEMP_PASSWORD${NC}"
echo -e "  │                                                         │"
echo -e "  │  You'll be asked to set a new password on first login.  │"
echo -e "  │                                                         │"
echo -e "  └─────────────────────────────────────────────────────────┘"
echo ""
echo -e "  ${DIM}Cognito sub: $SUB${NC}"
echo ""
