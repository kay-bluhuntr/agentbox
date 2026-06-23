#!/usr/bin/env bash
# Promote the image currently deployed in dev to production.
#
# Deployment state lives in the agentbox-gitops repo, not here. This script
# clones it, copies dev's pinned tag into prod's values, and opens a pull
# request there. The PR diff is the deployment record; ArgoCD applies it on
# merge. No kubectl, no helm, no SSH to anything.
set -euo pipefail

GITOPS_REPO="${GITOPS_REPO:-https://github.com/kay-bluhuntr/agentbox-gitops.git}"
GITOPS_SLUG="kay-bluhuntr/agentbox-gitops"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
git clone --quiet "$GITOPS_REPO" "$workdir"
cd "$workdir"

DEV_VALUES="envs/dev/values.yaml"
PROD_VALUES="envs/prod/values.yaml"

tag=$(grep -E '^\s+tag:' "$DEV_VALUES" | head -1 | sed -E 's/.*tag: *"?([^"]*)"?.*/\1/')
if [[ -z "$tag" || "$tag" == "initial" ]]; then
  echo "error: no built image tag found in $DEV_VALUES — has CI run on main yet?" >&2
  exit 1
fi

current=$(grep -E '^\s+tag:' "$PROD_VALUES" | head -1 | sed -E 's/.*tag: *"?([^"]*)"?.*/\1/')
if [[ "$current" == "$tag" ]]; then
  echo "prod is already on $tag — nothing to promote"
  exit 0
fi

branch="promote/${tag:0:7}"
git checkout -b "$branch"
sed -i.bak -E "s|^(\s+tag:).*|\1 \"$tag\"  # promoted from dev via PR — see scripts/promote.sh|" "$PROD_VALUES"
rm -f "${PROD_VALUES}.bak"
git add "$PROD_VALUES"
git commit -m "promote: ${tag:0:7} to production"
git push -u origin "$branch"

echo
echo "Pushed $branch to $GITOPS_SLUG promoting ${current:0:7} -> ${tag:0:7}."
gh pr create --repo "$GITOPS_SLUG" \
  --title "Promote ${tag:0:7} to production" \
  --body "Image verified in dev. ArgoCD will sync prod on merge." \
  --head "$branch" 2>/dev/null || {
  echo "Open the PR manually:"
  echo "  https://github.com/${GITOPS_SLUG}/compare/main...${branch}?expand=1"
}
