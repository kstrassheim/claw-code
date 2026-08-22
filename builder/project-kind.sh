# shellcheck shell=bash
# project-kind: what does a TARGET project actually deploy? Sourced (not
# executed) by the three runners, same pattern as
# /usr/local/bin/project-instructions.
#
#   web        a site to browse           frontend/ or backend/ (or: nothing
#                                         else matched — the safe fallback)
#   automation PowerShell runbooks        runbooks/ or automation*.tf
#   dwh        source DBs + Data Factory  synapse*.tf / datafactory*.tf /
#              + Synapse                  sql_init_*.sql
#   k8s        manifests deployed to a    k8s/  (and NO cluster IaC — the
#              cluster the project does         cluster belongs to somebody
#              NOT provision                    else: k3s, a homelab, a
#                                               managed cluster)
#   aks        ...and the project DOES    k8s/ AND aks.tf|kubernetes.tf,
#              provision it, on Azure     at the root or one level down
#   aksbot     ...and that workload is    the same as aks, AND the manifests
#              an openclaw bot            deploy an openclaw instance
#
# WHY k8s AND aks ARE SEPARATE
# ----------------------------
# `k8s` used to MEAN Azure — it required `aks.tf|kubernetes.tf`, so a project
# that only ships manifests matched nothing and fell through to `web`.
# k8s-ultimate-web-stack was detected as a web application: no cluster, no
# namespaces, no deployments, no events, no restarts, while it is nothing but
# those things.
#
# Deploying INTO a cluster and PROVISIONING one are different facts. The
# workload questions — what is running, why did it restart, what does this
# CronJob do — are identical on a k3s box and on AKS, and are gated on
# `has_cluster_kind`. Only the questions that need Azure itself — the cluster
# IaC, `az aks`, a node pool — are gated on `has_azure_cluster_kind`.
# Requiring Terraform to notice Kubernetes asked the wrong question: the
# manifests are what make it Kubernetes.
#
# A SET, NOT A SINGLE VALUE
# -------------------------
# Real projects combine these: a warehouse with a small web front end, a
# runbook repo that also ships a portal. This used to be a first-match-wins
# if/elif chain in each runner, which silently dropped every part but one — a
# DWH + website was classified `dwh`, the site was never opened, and the
# tester prompt went as far as asserting the project HAD no website while its
# URL sat in the pipeline trace. So detection returns EVERY kind that matches
# and the callers compose one prompt section per kind.
#
# The order is fixed — automation, dwh, k8s|aksbot, web — and callers keep it
# when they compose. It is the order the parts should be TESTED in: the runbook
# and data side first, the site last, because the site usually reads whatever
# the pipelines produced (and is often served from the cluster), so a failure
# there is explained by what came before.
#
# k8s VERSUS aksbot
# -----------------
# These two are mutually exclusive: `aksbot` is `k8s` upgraded. Cluster plus
# manifests gets you `k8s`, whose protocol is generic — connect, then pods,
# deployments, cronjobs, events, restarts. `aksbot` adds the stages that only
# make sense against an openclaw bot: its scheduled tick fired, its last run
# succeeded on its own terms, no stale lock directory, and the effect it was
# supposed to have actually landed in the database.
#
# The upgrade is decided by WHAT IS IN THE MANIFESTS, not by what is absent
# from the repo root. The first version used "no frontend/" as the test, on
# the theory that a repo with a frontend is a website rather than a bot. That
# proxy fails in both directions: a website deployed to AKS got no cluster
# check at all, and a bot repo that also ships a small UI would have had the
# bot stages run as though it were a plain site. Reading the manifests answers
# the actual question — is the thing in this cluster an openclaw instance? —
# and costs one grep (or, for the tester, nothing extra: the k8s kind already
# forces the checkout it then greps).
#
# The two detectors below must agree: the tester reads the repo tree over the
# API — `GET /repos/{owner}/{repo}/contents?ref={sha}`, one root entry per
# line, at the COMMIT UNDER TEST — because it often has no checkout, while the
# reviewer and solver read the checkout they already hold. Same patterns, two
# sources; the tree is passed in, so this file never makes a request of its
# own and stays testable without a token. The API side matches
# case-insensitively and the filesystem side does not — a project spelling it
# `Runbooks/` is detected by the tester and not by the other two, which is the
# pre-existing behaviour of both and not worth diverging further over.

# The set. Callers read it; nothing outside this file should assign it.
PROJECT_KINDS=""

_pk_add() {
  case " $PROJECT_KINDS " in
    *" $1 "*) ;;
    *) PROJECT_KINDS="${PROJECT_KINDS:+$PROJECT_KINDS }$1" ;;
  esac
}

# has_kind <kind> — true when the project is (also) that kind.
has_kind() {
  case " $PROJECT_KINDS " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# has_nonweb_kind — true when the project deploys something that is not a site.
# Gates everything that only makes sense against Azure resources: the source
# checkout, the scope rule, the Entra auth rule.
has_nonweb_kind() {
  has_kind automation || has_kind dwh || has_kind k8s || has_kind aks \
    || has_kind aksbot
}

# has_cluster_kind — this project deploys into a Kubernetes cluster, whoever
# provisioned it. `k8s` is a cluster the project does not own; `aks` and
# `aksbot` are ones it does. Everything gated on this is about WORKLOADS —
# namespaces, deployments, cronjobs, events, restarts — which are the same
# facts on a k3s box as on AKS.
has_cluster_kind() {
  has_kind k8s || has_kind aks || has_kind aksbot
}

# has_azure_cluster_kind — the project PROVISIONS its cluster on Azure. Gates
# the parts that are genuinely Azure-only: the cluster IaC, `az aks` commands,
# a node pool. A local cluster has none of them.
has_azure_cluster_kind() {
  has_kind aks || has_kind aksbot
}

# kind_count — how many kinds matched. 1 means "compose exactly what a
# single-kind project used to get"; >1 means the multi-part framing.
kind_count() {
  # shellcheck disable=SC2086
  set -- $PROJECT_KINDS
  echo "$#"
}

# kind_title <kind> — SHOUTED short name, for a part heading.
kind_title() {
  case "$1" in
    automation) echo "AZURE AUTOMATION" ;;
    dwh)        echo "DWH (source DBs + Data Factory + Synapse)" ;;
    k8s)        echo "KUBERNETES WORKLOAD" ;;
    aks)        echo "AKS WORKLOAD" ;;
    aksbot)     echo "AKS WORKLOAD (an openclaw bot)" ;;
    web)        echo "WEB APPLICATION" ;;
    *)          echo "$1" ;;
  esac
}

# kind_label <kind> — prose name, for a sentence describing the project.
kind_label() {
  case "$1" in
    automation) echo "Azure Automation runbooks" ;;
    dwh)        echo "a data warehouse (source databases, Data Factory pipelines, a Synapse SQL pool)" ;;
    k8s)        echo "a workload deployed to a Kubernetes cluster the project does not provision (manifests only)" ;;
    aks)        echo "a workload running in the project's own AKS cluster" ;;
    aksbot)     echo "an openclaw bot running in the project's own AKS cluster" ;;
    web)        echo "a web application with a site to browse" ;;
    *)          echo "$1" ;;
  esac
}

# kinds_english — "a data warehouse (...) AND a web application with a site to
# browse". Used in the one sentence that tells the agent what it is looking at.
kinds_english() {
  _pk_out=""; _pk_n=0
  for _pk_k in $PROJECT_KINDS; do _pk_n=$((_pk_n + 1)); done
  _pk_i=0
  for _pk_k in $PROJECT_KINDS; do
    _pk_i=$((_pk_i + 1))
    _pk_this="$(kind_label "$_pk_k")"
    if [ "$_pk_i" = 1 ]; then
      _pk_out="$_pk_this"
    elif [ "$_pk_i" = "$_pk_n" ]; then
      _pk_out="$_pk_out AND $_pk_this"
    else
      _pk_out="$_pk_out, $_pk_this"
    fi
  done
  echo "$_pk_out"
}

# ---------------------------------------------------------------------------
# Detection — from a repo tree listing (one root entry per line)
# ---------------------------------------------------------------------------
_pk_tree_has() { printf '%s\n' "$_PK_TREE" | grep -qiE "$1"; }

# A root listing cannot say whether the workload is a bot — that is in the
# manifests. So this always yields the generic `k8s`; the caller upgrades it
# with refine_aks_kind once it has a checkout.
detect_project_kinds_from_tree() {
  _PK_TREE="$1"
  PROJECT_KINDS=""
  if _pk_tree_has '^runbooks$|^automation.*\.tf$'; then _pk_add automation; fi
  if _pk_tree_has '^(synapse|datafactory).*\.tf$|^sql_init_.*\.sql$'; then _pk_add dwh; fi
  # KUBERNETES IS NOT AZURE.
  #
  # This used to demand `aks.tf|kubernetes.tf` AND `k8s/`, so a repository that
  # deploys manifests to a cluster somebody else runs — a k3s box, a homelab,
  # any managed cluster not provisioned from this repo — matched nothing and
  # fell through to the `web` default. k8s-ultimate-web-stack detected as
  # `web`: no cluster, no namespaces, no deployments, no events, no restarts,
  # while it is nothing BUT those things.
  #
  # The manifests are what make it a Kubernetes project. Provisioning the
  # cluster is a SEPARATE fact, and that is what `aks` now says.
  if _pk_tree_has '^k8s$'; then
    if _pk_tree_has '^(aks|kubernetes)\.tf$'; then _pk_add aks; else _pk_add k8s; fi
  fi
  if _pk_tree_has '^frontend$|^backend$'; then _pk_add web; fi
  # Nothing recognised — keep the browser test rather than silently testing
  # nothing. An unrecognised project loses no coverage it used to have.
  [ -n "$PROJECT_KINDS" ] || PROJECT_KINDS=web
  return 0
}

# ---------------------------------------------------------------------------
# Detection — from a checkout
# ---------------------------------------------------------------------------
# True when ANY argument exists. The obvious `ls a* b* c* >/dev/null 2>&1` is
# wrong and was the previous implementation in two of the runners: ls exits
# non-zero if ANY operand is missing, so it means "all three patterns match",
# not "any of them does". A project with datafactory.tf and no synapse*.tf was
# therefore never detected as dwh by the reviewer or the solver, while the
# tester (grep over the tree, real OR) said it was.
_pk_any_exists() {
  for _pk_g in "$@"; do
    [ -e "$_pk_g" ] && return 0
  done
  return 1
}

detect_project_kinds_from_dir() {
  _pk_dir="$1"
  PROJECT_KINDS=""
  if [ -d "$_pk_dir/runbooks" ] || _pk_any_exists "$_pk_dir"/automation*.tf; then
    _pk_add automation
  fi
  if _pk_any_exists "$_pk_dir"/synapse*.tf "$_pk_dir"/datafactory*.tf \
                    "$_pk_dir"/sql_init_*.sql; then
    _pk_add dwh
  fi
  # See detect_project_kinds_from_tree. Manifests make it Kubernetes; Azure
  # cluster IaC makes it AKS. The IaC files are searched below the root as
  # well, because a repository that keeps them in `terraform/` is provisioning
  # a cluster just as much as one that keeps them at the top.
  if [ -d "$_pk_dir/k8s" ]; then
    if _pk_any_exists "$_pk_dir"/aks.tf "$_pk_dir"/kubernetes.tf \
                      "$_pk_dir"/*/aks.tf "$_pk_dir"/*/kubernetes.tf; then
      _pk_add aks
    else
      _pk_add k8s
    fi
  fi
  if [ -d "$_pk_dir/frontend" ] || [ -d "$_pk_dir/backend" ]; then
    _pk_add web
  fi
  [ -n "$PROJECT_KINDS" ] || PROJECT_KINDS=web
  # The manifests are right here, so answer the bot question immediately.
  refine_aks_kind "$_pk_dir"
  return 0
}

# ---------------------------------------------------------------------------
# k8s -> aksbot, from the manifests
# ---------------------------------------------------------------------------
# k8s_workload_is_bot <dir> — does what this repo deploys to its cluster
# include an openclaw instance? The bot ships as a `claw-*` deployment with an
# `openclaw.json` ConfigMap and a PVC-mounted ~/.openclaw, so its name is all
# over its own manifests and appears in nobody else's.
k8s_workload_is_bot() {
  [ -d "$1/k8s" ] || return 1
  grep -rqiE 'openclaw|claw-[a-z0-9]' "$1/k8s" 2>/dev/null
}

# refine_aks_kind <dir> — upgrade a generic k8s kind to aksbot when the
# manifests show a bot. Safe to call when the project has no cluster, when the
# directory does not exist, or twice. A caller with no checkout simply skips
# it and keeps the generic protocol, which is the right degradation: the bot
# stages need the repo anyway to resolve what they are looking at.
refine_aks_kind() {
  # `aksbot` is `aks` upgraded, never `k8s` upgraded: a bot in a cluster the
  # project does not provision is still not an AKS workload.
  has_kind aks || return 0
  k8s_workload_is_bot "$1" || return 0
  _pk_new=""
  for _pk_k in $PROJECT_KINDS; do
    [ "$_pk_k" = "aks" ] && _pk_k=aksbot
    _pk_new="${_pk_new:+$_pk_new }$_pk_k"
  done
  PROJECT_KINDS="$_pk_new"
  return 0
}


# ===========================================================================
# ANNOTATIONS — narrower facts than a kind
# ===========================================================================
# A KIND answers "what IS this project", and saying it re-frames everything:
# more than one kind switches the reviewer out of its single-part protocol and
# into "this project is MORE THAN ONE THING", with a section per part.
#
# An ANNOTATION answers a narrower question and must NOT re-frame anything.
# It is deliberately weaker: it adds a fact, it never changes the shape of the
# prompt around it.
#
# The distinction is not academic. 14 of the 15 repositories this bot works on
# contain `.tf` files. Making that a kind would fire the multi-part preamble on
# nearly all of them — and a framing that always fires stops being read, which
# costs the signal on the three repositories that genuinely ARE two things.
#
# A SET, not a scalar: more annotations will follow. Within a family the values
# are mutually exclusive (a checkout is managed by one binary, not two), which
# is enforced by the detector returning at its first match rather than by
# anything here.
PROJECT_ANNOTATIONS=""

_pa_add() {
  case " $PROJECT_ANNOTATIONS " in
    *" $1 "*) ;;
    *) PROJECT_ANNOTATIONS="${PROJECT_ANNOTATIONS:+$PROJECT_ANNOTATIONS }$1" ;;
  esac
}

# has_annotation <name> — true when the project carries that fact.
has_annotation() {
  case " $PROJECT_ANNOTATIONS " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# annotation_count — how many matched. Deliberately NOT consulted by any
# framing decision; it exists for logging and for tests.
annotation_count() {
  # shellcheck disable=SC2086
  set -- $PROJECT_ANNOTATIONS
  echo "$#"
}

# annotation_title <name> — short heading for one annotation.
annotation_title() {
  case "$1" in
    iac-terraform) echo "Infrastructure tool: Terraform" ;;
    iac-tofu)      echo "Infrastructure tool: OpenTofu" ;;
    iac-unknown)   echo "Infrastructure tool: UNDETERMINED" ;;
    *)             echo "$1" ;;
  esac
}

# annotation_body <name> — what the agent must actually do about it.
annotation_body() {
  case "$1" in
    iac-terraform)
      cat <<'PA_EOF'
This checkout is managed by **Terraform**. Use `terraform`. Do NOT run `tofu`.

Both binaries are installed and both read the same `.tf` files, so the wrong
one runs happily and does damage quietly: it rewrites the provider addresses
in `.terraform.lock.hcl` (and can rewrite them in state), which is a migration
nobody asked for, in a commit about something else.

If you believe the project should move to OpenTofu, say so on the issue and
stop. Switching tools is its own decision and its own change.
PA_EOF
      ;;
    iac-tofu)
      cat <<'PA_EOF'
This checkout is managed by **OpenTofu**. Use `tofu`. Do NOT run `terraform`.

Both binaries are installed and both read the same `.tf` files, so the wrong
one runs happily and does damage quietly: it rewrites the provider addresses
in `.terraform.lock.hcl` (and can rewrite them in state), which is a migration
nobody asked for, in a commit about something else.

If you believe the project should move to Terraform, say so on the issue and
stop. Switching tools is its own decision and its own change.
PA_EOF
      ;;
    iac-unknown)
      cat <<'PA_EOF'
This checkout contains `.tf` files, and NOTHING here says which binary owns
them: there is no `.terraform.lock.hcl` to read a registry host out of, and no
pipeline that names `terraform` or `tofu`.

**Run neither.** Guessing has a wrong answer that silently rewrites the
lockfile and possibly state. If the work needs an init/plan/apply, say on the
issue that the tool could not be determined and ask which one to use.

Editing `.tf` source without running either binary is fine.
PA_EOF
      ;;
  esac
}

# project_annotations_block — the full prompt section, or "" when there is
# nothing to say. Flat headings, no preamble: this annotates, it does not
# re-frame.
project_annotations_block() {
  [ -n "$PROJECT_ANNOTATIONS" ] || return 0
  echo "## What is known about this repository"
  echo
  for _pa_a in $PROJECT_ANNOTATIONS; do
    echo "### $(annotation_title "$_pa_a")"
    echo
    annotation_body "$_pa_a"
    echo
  done
}

# project_annotations_reminder — ONE line, for a prompt that is re-sent on
# later turns. The solver states its full prompt on turn 1 only and then polls;
# by turn six the block above is the oldest thing in the session and is the
# first to be summarised away. A rule whose wrong answer rewrites state cannot
# rely on being remembered.
project_annotations_reminder() {
  [ -n "$PROJECT_ANNOTATIONS" ] || return 0
  for _pa_a in $PROJECT_ANNOTATIONS; do
    case "$_pa_a" in
      iac-terraform) echo "Reminder: this repository is Terraform-managed — use \`terraform\`, never \`tofu\`." ;;
      iac-tofu)      echo "Reminder: this repository is OpenTofu-managed — use \`tofu\`, never \`terraform\`." ;;
      iac-unknown)   echo "Reminder: the infrastructure tool for this repository is UNDETERMINED — run neither \`terraform\` nor \`tofu\`; ask." ;;
    esac
  done
}


# detect_project_annotations_from_dir <dir> — fill PROJECT_ANNOTATIONS.
#
# EVIDENCE BEFORE INTENT. The order below is deliberate:
#
#   1. `.terraform.lock.hcl` — written BY whichever binary ran, and the two
#      write different registry hosts into it. Verified on this image:
#        tofu init      -> provider "registry.opentofu.org/hashicorp/null"
#        terraform init -> provider "registry.terraform.io/hashicorp/null"
#      Same filename, so the file's PRESENCE says nothing; its contents say
#      everything.
#   2. The pipeline — what CI installs and invokes. Needed because a lockfile
#      is not always committed: k8s-ultimate-web-stack has terraform/*.tf, no
#      lockfile at all, and `hashicorp/setup-terraform` in its workflow.
#   3. Neither -> `iac-unknown`, which tells the agent to run nothing. A repo
#      with .tf files and no signal is the dangerous case, so it gets an
#      annotation rather than silence.
#
# A repository with no `.tf` at all gets no annotation: there is no question.
detect_project_annotations_from_dir() {
  _pa_dir="$1"
  PROJECT_ANNOTATIONS=""
  [ -d "$_pa_dir" ] || return 0

  # Any IaC source at all? .terraform/ is a build artifact, not source.
  if ! find "$_pa_dir" -maxdepth 3 -name '*.tf' -not -path '*/.terraform/*' \
       2>/dev/null | head -1 | grep -q .; then
    return 0
  fi

  # 1. the lockfile's registry host
  _pa_locks="$(find "$_pa_dir" -maxdepth 3 -name '.terraform.lock.hcl' \
                 2>/dev/null | head -5)"
  if [ -n "$_pa_locks" ]; then
    # shellcheck disable=SC2086
    if grep -qF 'registry.opentofu.org' $_pa_locks 2>/dev/null; then
      _pa_add iac-tofu; return 0
    fi
    # shellcheck disable=SC2086
    if grep -qF 'registry.terraform.io' $_pa_locks 2>/dev/null; then
      _pa_add iac-terraform; return 0
    fi
  fi

  # 2. what the pipeline installs or runs
  _pa_ci=""
  [ -d "$_pa_dir/.github/workflows" ] && _pa_ci="$_pa_dir/.github/workflows"
  [ -f "$_pa_dir/.gitlab-ci.yml" ] && _pa_ci="$_pa_ci $_pa_dir/.gitlab-ci.yml"
  if [ -n "$_pa_ci" ]; then
    # shellcheck disable=SC2086
    if grep -rqE 'opentofu/setup-opentofu|(^|[^-[:alnum:]])tofu[[:space:]]+(init|plan|apply|validate|fmt)' \
         $_pa_ci 2>/dev/null; then
      _pa_add iac-tofu; return 0
    fi
    # shellcheck disable=SC2086
    if grep -rqE 'hashicorp/setup-terraform|(^|[^-[:alnum:]])terraform[[:space:]]+(init|plan|apply|validate|fmt)' \
         $_pa_ci 2>/dev/null; then
      _pa_add iac-terraform; return 0
    fi
  fi

  # 3. .tf files, nothing that says which tool owns them
  _pa_add iac-unknown
  return 0
}
