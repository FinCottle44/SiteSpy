#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SiteSpy — Interactive Role Change
#
# Lists users from Cognito, lets you pick one, then choose a new role.
# Updates the Cognito group membership and the DynamoDB User_Record.
#
# Usage:
#   ./scripts/change-role.sh          # defaults to prod
#   ./scripts/change-role.sh --env dev
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
echo -e "${YELLOW}  SiteSpy — Change User Role ${DIM}($ENV)${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"

# ─── Fetch users from Cognito ────────────────────────────────────────────────
echo ""
echo -e "${DIM}  Fetching users from Cognito...${NC}"

USER_JSON=$(aws cognito-idp list-users \
  --user-pool-id "$USER_POOL_ID" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --output json 2>/dev/null)

# Parse into arrays
USERNAMES=()
EMAILS=()
SUBS=()
CURRENT_GROUPS=()
LABELS=()

while IFS='|' read -r username email sub; do
  USERNAMES+=("$username")
  EMAILS+=("$email")
  SUBS+=("$sub")
done < <(echo "$USER_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for user in data.get('Users', []):
    username = user['Username']
    attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
    email = attrs.get('email', username)
    sub = attrs.get('sub', username)
    print(f'{username}|{email}|{sub}')
" 2>/dev/null)

if [ ${#USERNAMES[@]} -eq 0 ]; then
  echo -e "  ${RED}No users found in the user pool.${NC}"
  exit 0
fi

# Fetch current group for each user
echo -e "${DIM}  Fetching group memberships...${NC}"
for i in "${!USERNAMES[@]}"; do
  GROUPS_JSON=$(aws cognito-idp admin-list-groups-for-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "${USERNAMES[$i]}" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --output json 2>/dev/null)

  GROUP=$(echo "$GROUPS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
groups = [g['GroupName'] for g in data.get('Groups', [])]
if 'SuperAdmins' in groups:
    print('super_admin')
elif 'TenantAdmins' in groups:
    print('tenant_admin')
else:
    print('user')
" 2>/dev/null)

  CURRENT_GROUPS+=("$GROUP")
  LABELS+=("${EMAILS[$i]}  ${DIM}(current role: $GROUP)${NC}")
done

# ─── Select user ─────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}  Select user to change role:${NC}"
echo -e "${DIM}  ─────────────────────────────────────────${NC}"
for i in "${!LABELS[@]}"; do
  printf "  ${BOLD}%d)${NC} %b\n" $((i + 1)) "${LABELS[$i]}"
done
echo ""

COUNT=${#USERNAMES[@]}
while true; do
  read -rp "  Choose [1-$COUNT]: " selection
  if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le "$COUNT" ]; then
    IDX=$((selection - 1))
    break
  fi
  echo -e "  ${RED}Invalid choice.${NC}"
done

SELECTED_USERNAME="${USERNAMES[$IDX]}"
SELECTED_EMAIL="${EMAILS[$IDX]}"
SELECTED_SUB="${SUBS[$IDX]}"
SELECTED_CURRENT_ROLE="${CURRENT_GROUPS[$IDX]}"

echo ""
echo -e "  Selected: ${BOLD}$SELECTED_EMAIL${NC} (currently ${CYAN}$SELECTED_CURRENT_ROLE${NC})"

# ─── Select new role ─────────────────────────────────────────────────────────
prompt_choice "Select new role" \
  "user — Regular user (site-level access)" \
  "tenant_admin — Tenant admin (manages users & sites)" \
  "super_admin — Super admin (full platform access)"

NEW_ROLE="${CHOICE%% —*}"

if [ "$NEW_ROLE" = "$SELECTED_CURRENT_ROLE" ]; then
  echo ""
  echo -e "  ${DIM}User already has the '$NEW_ROLE' role. Nothing to change.${NC}"
  exit 0
fi

# ─── Tenant selection (for tenant_admin and user) ────────────────────────────
TENANT=""
if [ "$NEW_ROLE" != "super_admin" ]; then
  echo ""
  echo -e "${DIM}  Fetching tenants from DynamoDB...${NC}"

  TENANT_DATA=$(aws dynamodb scan \
    --table-name "$TABLE" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --filter-expression "begins_with(PK, :pk) AND begins_with(SK, :sk)" \
    --expression-attribute-values '{":pk":{"S":"TENANT#"}, ":sk":{"S":"TENANT#"}}' \
    --projection-expression "PK, tenant_name" \
    --output json 2>/dev/null)

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

# ─── Site selection (for user role) ──────────────────────────────────────────
SITES=""
SITE_IDS=()
if [ "$NEW_ROLE" = "user" ] && [ -n "$TENANT" ]; then
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

    SITES=$(IFS=','; echo "${SITE_IDS[*]}")
  else
    echo -e "  ${DIM}No sites found for this tenant. Skipping site assignment.${NC}"
  fi
fi

# ─── Confirmation ────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}─────────────────────────────────────────────────────────${NC}"
echo -e "${BOLD}  Review:${NC}"
echo -e "    User:      $SELECTED_EMAIL"
echo -e "    Old role:  $SELECTED_CURRENT_ROLE"
echo -e "    New role:  $NEW_ROLE"
[ -n "$TENANT" ] && echo -e "    Tenant:    $TENANT"
[ -n "$SITES" ]  && echo -e "    Sites:     $SITES"
echo -e "${YELLOW}─────────────────────────────────────────────────────────${NC}"
echo ""
read -rp "$(echo -e "  ${BOLD}Apply this change? [Y/n]:${NC} ")" CONFIRM
CONFIRM="${CONFIRM:-Y}"

if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo -e "  ${DIM}Cancelled.${NC}"
  exit 0
fi

# ─── Map roles to Cognito groups ─────────────────────────────────────────────
role_to_group() {
  case "$1" in
    super_admin)  echo "SuperAdmins" ;;
    tenant_admin) echo "TenantAdmins" ;;
    user)         echo "" ;;
  esac
}

OLD_GROUP=$(role_to_group "$SELECTED_CURRENT_ROLE")
NEW_GROUP=$(role_to_group "$NEW_ROLE")

# ─── Update Cognito group membership ────────────────────────────────────────
echo ""

# Remove from old group
if [ -n "$OLD_GROUP" ]; then
  echo -n "  Removing from group ($OLD_GROUP)... "
  aws cognito-idp admin-remove-user-from-group \
    --user-pool-id "$USER_POOL_ID" \
    --username "$SELECTED_USERNAME" \
    --group-name "$OLD_GROUP" \
    --region "$REGION" \
    --profile "$PROFILE" 2>/dev/null
  echo -e "${GREEN}done${NC}"
fi

# Add to new group
if [ -n "$NEW_GROUP" ]; then
  echo -n "  Adding to group ($NEW_GROUP)... "
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id "$USER_POOL_ID" \
    --username "$SELECTED_USERNAME" \
    --group-name "$NEW_GROUP" \
    --region "$REGION" \
    --profile "$PROFILE" 2>/dev/null
  echo -e "${GREEN}done${NC}"
fi

# ─── Update Cognito custom attributes ───────────────────────────────────────
ATTR_UPDATES=""
if [ -n "$TENANT" ]; then
  ATTR_UPDATES="Name=custom:tenant_id,Value=$TENANT"
fi
if [ -n "$SITES" ]; then
  ATTR_UPDATES="$ATTR_UPDATES Name=custom:site_access,Value=$SITES"
fi

if [ -n "$ATTR_UPDATES" ]; then
  echo -n "  Updating Cognito attributes... "
  aws cognito-idp admin-update-user-attributes \
    --user-pool-id "$USER_POOL_ID" \
    --username "$SELECTED_USERNAME" \
    --user-attributes $ATTR_UPDATES \
    --region "$REGION" \
    --profile "$PROFILE" 2>/dev/null
  echo -e "${GREEN}done${NC}"
fi

# ─── Update DynamoDB User_Record ─────────────────────────────────────────────
echo -n "  Finding User_Record in DynamoDB... "

DYNAMO_RECORDS=$(aws dynamodb scan \
  --table-name "$TABLE" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --filter-expression "(begins_with(SK, :sk1) OR begins_with(SK, :sk2)) AND begins_with(SK, :user_prefix)" \
  --expression-attribute-values "{\":sk1\":{\"S\":\"USER#$SELECTED_SUB\"}, \":sk2\":{\"S\":\"USER#$SELECTED_EMAIL\"}, \":user_prefix\":{\"S\":\"USER#\"}}" \
  --projection-expression "PK, SK" \
  --output json 2>/dev/null)

RECORD_COUNT=$(echo "$DYNAMO_RECORDS" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('Items',[])))" 2>/dev/null)

if [ "$RECORD_COUNT" -gt 0 ]; then
  echo -e "${GREEN}found $RECORD_COUNT record(s)${NC}"

  echo "$DYNAMO_RECORDS" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('Items', []):
    pk = item['PK']['S']
    sk = item['SK']['S']
    print(f'{pk}|{sk}')
" 2>/dev/null | while IFS='|' read -r pk sk; do
    echo -n "  Updating role on $pk / $sk... "

    # Build update expression
    UPDATE_EXPR="SET #r = :role"
    ATTR_NAMES='{"#r":"role"}'
    ATTR_VALUES="{\":role\":{\"S\":\"$NEW_ROLE\"}"

    if [ -n "$TENANT" ]; then
      UPDATE_EXPR="$UPDATE_EXPR, tenant_id = :tid"
      ATTR_VALUES="$ATTR_VALUES, \":tid\":{\"S\":\"$TENANT\"}"
    fi

    if [ ${#SITE_IDS[@]} -gt 0 ]; then
      SITE_ACCESS_ITEMS=""
      for site in "${SITE_IDS[@]}"; do
        SITE_ACCESS_ITEMS="${SITE_ACCESS_ITEMS}{\"S\": \"$site\"},"
      done
      SITE_ACCESS_ITEMS="${SITE_ACCESS_ITEMS%,}"
      UPDATE_EXPR="$UPDATE_EXPR, site_access = :sites"
      ATTR_VALUES="$ATTR_VALUES, \":sites\":{\"L\":[$SITE_ACCESS_ITEMS]}"
    fi

    ATTR_VALUES="$ATTR_VALUES}"

    aws dynamodb update-item \
      --table-name "$TABLE" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --key "{\"PK\":{\"S\":\"$pk\"}, \"SK\":{\"S\":\"$sk\"}}" \
      --update-expression "$UPDATE_EXPR" \
      --expression-attribute-names "$ATTR_NAMES" \
      --expression-attribute-values "$ATTR_VALUES" 2>/dev/null
    echo -e "${GREEN}done${NC}"
  done
else
  echo -e "${YELLOW}no DynamoDB record found — Cognito group updated only${NC}"
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ Role changed: $SELECTED_EMAIL${NC}"
echo -e "${GREEN}    $SELECTED_CURRENT_ROLE → $NEW_ROLE${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
