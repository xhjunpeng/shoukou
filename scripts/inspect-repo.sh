#!/usr/bin/env bash
# Read-only Git/PR/Actions inventory for the shoukou skill.
set -u

base=""
if [ "${1:-}" = "--base" ]; then
  base="${2:-}"
fi

if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  printf 'error: not inside a Git worktree\n' >&2
  exit 2
fi

cd "$root"

if [ -z "$base" ]; then
  base="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
fi

if [ -z "$base" ]; then
  for candidate in main master; do
    if git show-ref --verify --quiet "refs/heads/$candidate" || git show-ref --verify --quiet "refs/remotes/origin/$candidate"; then
      base="$candidate"
      break
    fi
  done
fi

repository=""
merged_pr_heads=""
if command -v gh >/dev/null 2>&1; then
  remote_url="$(git remote get-url origin 2>/dev/null || true)"
  repository="$(printf '%s' "$remote_url" | sed -E 's#\.git$##; s#^(git@github.com:|https://github.com/|ssh://git@github.com/)##')"
  if ! printf '%s' "$repository" | grep -Eq '^[^/[:space:]]+/[^/[:space:]]+$'; then
    repository=""
  elif [ -n "$base" ]; then
    merged_pr_heads="$(gh pr list --repo "$repository" --state merged --limit 100 --json headRefName,baseRefName \
      --jq '.[] | [.headRefName, .baseRefName] | @tsv' 2>/dev/null \
      | awk -F '\t' -v target="$base" '$2 == target { print $1 }')"
  fi
fi

printf 'repository=%s\n' "$root"
printf 'head=%s\n' "$(git rev-parse --short HEAD)"
printf 'branch=%s\n' "$(git branch --show-current)"
printf 'base=%s\n' "$base"

policy_ref="$base"
local_policy_version=""
remote_policy_version=""
if [ -n "$base" ] && git cat-file -e "$base:AGENTS.md" 2>/dev/null; then
  local_policy_version="$(git show "$base:AGENTS.md" | sed -n 's/.*closeout-policy: \([^ >]*\).*/\1/p' | head -n 1)"
fi
if [ -n "$base" ] && git cat-file -e "origin/$base:AGENTS.md" 2>/dev/null; then
  remote_policy_version="$(git show "origin/$base:AGENTS.md" | sed -n 's/.*closeout-policy: \([^ >]*\).*/\1/p' | head -n 1)"
fi
if [ -n "$local_policy_version" ]; then
  policy_ref="$base"
  policy_version="$local_policy_version"
elif [ -n "$remote_policy_version" ]; then
  policy_ref="origin/$base"
  policy_version="$remote_policy_version"
else
  policy_ref="origin/$base"
  policy_version=""
fi
printf 'policy_ref=%s\n' "$policy_ref"
printf 'policy_version=%s\n' "${policy_version:-unversioned}"
printf '\n[status]\n'
git status --short --branch

printf '\n[worktrees]\n'
git worktree list --porcelain

printf '\n[local-branches]\n'
if [ -n "$base" ] && git rev-parse --verify --quiet "$base" >/dev/null; then
  git for-each-ref --format='%(refname:short)|%(upstream:short)|%(objectname:short)|%(committerdate:short)' refs/heads \
    | while IFS='|' read -r branch upstream commit date; do
        if [ "$branch" = "$base" ]; then
          relation='base'
        elif git merge-base --is-ancestor "$branch" "$base"; then
          relation='merged-into-base'
        elif printf '%s\n' "$merged_pr_heads" | grep -Fqx -- "$branch"; then
          relation='merged-via-pr'
        else
          relation='not-merged-into-base'
        fi
        branch_policy=""
        if git cat-file -e "$branch:AGENTS.md" 2>/dev/null; then
          branch_policy="$(git show "$branch:AGENTS.md" | sed -n 's/.*closeout-policy: \([^ >]*\).*/\1/p' | head -n 1)"
        fi
        if [ -z "$policy_version" ] || [ "$branch_policy" = "$policy_version" ]; then
          policy_state='current-or-unversioned'
        elif [ -z "$branch_policy" ]; then
          policy_state='legacy-unmarked'
        else
          policy_state="legacy-$branch_policy"
        fi
        printf '%s|%s|%s|%s|%s|%s\n' "$branch" "$relation" "$upstream" "$commit" "$date" "$policy_state"
      done
else
  git for-each-ref --format='%(refname:short)|base-unknown|%(upstream:short)|%(objectname:short)|%(committerdate:short)' refs/heads
fi

printf '\n[remote]\n'
git remote -v

if [ -n "$repository" ]; then
    printf '\n[pull-requests]\n'
    gh pr list --repo "$repository" --state all --limit 100 --json number,state,isDraft,headRefName,baseRefName,mergedAt,url \
      --jq '.[] | [.number, .state, .headRefName, .baseRefName, (.mergedAt // ""), .url] | @tsv' 2>/dev/null || printf 'unavailable\n'
    printf '\n[actions-permission]\n'
    gh api "repos/$repository/actions/permissions" --jq '.enabled' 2>/dev/null || printf 'unavailable\n'
    if [ -n "$base" ]; then
      printf '\n[branch-protection]\n'
      gh api "repos/$repository/branches/$base" --jq '.protected' 2>/dev/null || printf 'unavailable\n'
    fi
fi
