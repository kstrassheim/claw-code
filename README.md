# claw-code

Autonomous coding agent powered by OpenClaw, deployed on AKS.

## Architecture

```
claw-code/
├── main.tf                  # Terraform: AKS cluster, ACR, PV storage, Sealed Secrets, NetworkPolicies
├── variables.tf             # Terraform variables
├── terraform.tfvars          # Dev environment values
├── builder/
│   ├── Dockerfile                  # Custom openclaw image (all coding features, no astrology)
│   ├── heartbeat-issue-tick.py     # Issue-watcher planner (see Autonomous issue watcher)
│   ├── cron-issue-spawn.sh         # Issue-watcher Job-spawner (see Autonomous issue watcher)
│   ├── k8s-mcp/                    # kubectl MCP
│   ├── azure-mcp/                  # az CLI MCP
│   ├── aws-mcp/                    # AWS CLI MCP
│   ├── gcp-mcp/                    # gcloud MCP
│   ├── alicloud-mcp/               # aliyun CLI MCP
│   ├── debug-mcp/                  # Node CDP debugger MCP
│   └── entra-totp/                 # TOTP generator for Entra MFA
├── k8s/
│   ├── 000-namespace-and-config.yaml  # Namespace
│   ├── 005-pvc.yaml                   # PVC for openclaw workspace
│   ├── 010-openclaw-config.yaml       # Config template + render-config script + RBAC
│   ├── 020-deployment.yaml            # Deployment + ServiceAccount + Role/Binding
│   ├── 030-service.yaml               # ClusterIP service
│   ├── 040-networkpolicy.yaml         # default-deny + allow-dns
│   ├── 050-issue-watcher.yaml         # Issue-watcher CronJob + RBAC + chat skill
│   └── tools/                         # Per-tool TOOLS-*.md (assembled into openclaw-tools-md ConfigMap by deploy.yml)
└── .github/workflows/
    ├── validate.yml      # On PR to main: check secrets, terraform plan, docker build (no push)
    ├── deploy.yml        # On push to main: terraform apply, build+push image, kubectl apply, rollout
    ├── seal-secrets.yml  # Read GH secrets, kubectl apply the Secret directly to the cluster (filename is historical — no kubeseal involved any more)
    └── codeql.yml        # JS security scan
```

Secrets are not stored in the repo — they come from the `seal-secrets`
workflow which reads GitHub Actions secrets (per the `dev` environment)
and applies a plain Kubernetes Secret straight to the cluster via
`kubectl apply -f -` (the manifest never touches disk or git).

---

## Prerequisites

### Azure (one-time human setup)

1. **Resource Group** `claw-code` must exist:
   ```bash
   az group create --name claw-code --location westeurope
   ```

2. **User-Assigned Managed Identity** for GitHub Actions OIDC:
   ```bash
   az identity create --name github-claw-code --resource-group claw-code
   ```
   Note the `clientId` for `AZURE_CLIENT_ID`.

3. **Federated Credentials** on the identity:
   - Azure Portal → Entra ID → App registrations → find your MI → Certificates & secrets → Federated credentials
   - Add credential:
     - **Trusted entity**: GitHub Actions
     - **Organization**: `kstrassheim`
     - **Repository**: `claw-code`
     - **Environment**: `dev` (or `All environments`)
     - **Audience**: `api://AzureADTokenExchange`

4. **Role assignments** on the identity:
   ```bash
   MI_PRINCIPAL_ID=$(az identity show --name github-claw-code --resource-group claw-code --query principalId -o tsv)
   RG_ID=$(az group show --name claw-code --query id -o tsv)

   az role assignment create --role "Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
   az role assignment create --role "Storage Account Contributor" --assignee-object-id $MI_PRINCIPAL_ID --scope $RG_ID
   ```

5. **Terraform state** already exists:
   - Account: `mytofustates`
   - Container: `claw-code`
   - Key: `dev.tfstate`

### GitHub Secrets

Set under `Settings → Environments → dev → Environment secrets` (the
deploy/validate workflows scope to `environment: dev`, so repo-level
secrets won't resolve).

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Client ID of the deploy-managed identity |
| `AZURE_TENANT_ID` | Yes | Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Subscription ID |
| `MISTRAL_API_KEY` | One of these two | Mistral API key — see below |
| `MINIMAX_API_KEY` | One of these two | MiniMax API key — see below |
| `BOT_GITHUB_TOKEN` | Yes | Bot account PAT for autonomous git/github work |
| `TELEGRAM_BOT_TOKEN` | Optional | Telegram bot token if the channel is enabled |

> Note on `BOT_GITHUB_TOKEN`: GitHub Actions reserves `secrets.GITHUB_TOKEN`
> for the auto-generated per-workflow token. We use `BOT_GITHUB_TOKEN`
> for the long-lived bot PAT and seal it into the cluster under the
> `GITHUB_TOKEN` k8s secret key that openclaw expects.

**At least one of `MISTRAL_API_KEY` / `MINIMAX_API_KEY` is required.**
Both the `validate` and `deploy` workflows have a `check-llm-secrets`
job that fails when neither is set — PR won't merge, deploy won't run.

### Getting the LLM API keys

#### Mistral (Experimental tier)

1. Go to [console.mistral.ai](https://console.mistral.ai/) and sign up
   (email or Google/GitHub OAuth).
2. The free **Experimental** plan is selected by default — it includes
   access to all production models (Mistral Large, Medium 3.5, Pixtral,
   Codestral) at experimental rate limits. No credit card needed for the
   tier; you can upgrade to a paid plan later if you hit the limits.
3. Navigate to `Workspace → API Keys` (or
   [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys/)).
4. Click `Create new key`, give it a name (e.g. `claw-code`), and copy
   the token — it's only shown once.
5. Paste into `MISTRAL_API_KEY` under the `dev` GitHub environment.

#### MiniMax (Starter tier)

1. Go to [platform.minimax.io](https://platform.minimax.io/) and sign up
   (email or Google).
2. The **Starter** plan is the default free tier — gives you access to
   `MiniMax-M2.7` (the reasoning/coding model claw-code uses) with a
   monthly token quota. No credit card required.
3. Navigate to `Account → API Keys` and click `Create API Key`.
4. Copy the token (only shown once) and paste it into
   `MINIMAX_API_KEY` under the `dev` GitHub environment.

Whichever one(s) you set will be reflected in the openclaw config at the
next pod restart — see the **LLMs** section below for the routing
matrix.

---

## Stage 1: Terraform (Infrastructure)

### What gets deployed

| Resource | Purpose |
|---|---|
| AKS cluster (1× Standard_D2pds_v5, arm64, 2 vCPU / 8 GiB) | K8s cluster |
| Azure Container Registry (`clwcodecodevdev1`, Basic SKU) | Hosts the custom openclaw image |
| Azure Storage Account (`clwcodecodev`) | Backs the openclaw workspace PVC |
| Default-deny NetworkPolicies + DNS/HTTPS allow rules | Pure `kubernetes_network_policy_v1`, no controller required |
| Role assignments: AcrPull on the deploy MI + AKS kubelet identity | Required so both CI and the cluster can read the registry |

### Terraform workflow

- **PR to `main`**: `validate.yml` runs `terraform init/validate/plan` and posts a summary; nothing is applied.
- **Merge to `main`**: `deploy.yml` runs `terraform apply -auto-approve` as its first job, then builds + pushes the image, then `kubectl apply -f k8s/`, then rolls the openclaw deployment.

### Terraform variables

| Variable | Default | Description |
|---|---|---|
| `env` | `dev` | Environment name |
| `cluster_name` | `claw-code-aks` | AKS cluster name |
| `location` | `westeurope` | Azure region |
| `node_size` | `Standard_D2pds_v5` | arm64 VM (2 vCPU, 8 GiB) — matches the `linux/arm64` image build target |
| `node_count` | `1` | Number of nodes |
| `storage_account_name` | `clwcodecodev` | Globally unique storage account name |
| `namespace` | `openclaw` | K8s namespace |
| `aks_admin_group_name` | `claw-code-aks-admin` | Display name of the Entra group granted `Azure Kubernetes Service RBAC Cluster Admin` on the cluster scope |
| `unique_suffix` | `dev1` | Appended to globally-unique resource names (ACR, storage account) |

---

## Stage 2: Custom Docker Image

### What's included

✅ VSCode (code-server)
✅ Azure CLI + azure-mcp
✅ AWS CLI v2 + aws-mcp
✅ Google Cloud CLI + gcp-mcp
✅ Alibaba Cloud CLI + alicloud-mcp
✅ kubectl + k8s-mcp
✅ Terraform CLI + terraform-mcp-server
✅ GitHub CLI + github-mcp-server
✅ Chromium (browser automation for Entra login)
✅ podman (rootless, vfs storage)
✅ code-server (full VSCode-in-the-browser)
✅ debug-mcp (Node CDP debugger)

### Building the image

The normal path is **CI-driven** — `deploy.yml`'s `build-and-push-image`
job builds and pushes on every merge to `main`:

1. Mirror `ghcr.io/openclaw/openclaw:<VERSIONS/OPENCLAW_UPSTREAM>` into
   our ACR as `openclaw/openclaw-base:<tag>` (idempotent via
   `az acr import`).
2. `docker buildx build --platform linux/arm64` against `builder/`.
3. Push `:latest` and `:<short_sha>` to
   `clwcodecodevdev1.azurecr.io/openclaw/claw-code`.

The Deployment references `:latest` with `imagePullPolicy: Always`, so
the post-push `kubectl rollout restart` step is what actually picks up
new code.

To build locally for testing (arm64 host or buildx multi-platform):

```bash
cd builder/
docker buildx build --platform linux/arm64 \
  --build-arg BASE_IMAGE=clwcodecodevdev1.azurecr.io/openclaw/openclaw-base:$(grep '^OPENCLAW_UPSTREAM=' ../VERSIONS | cut -d= -f2) \
  -t clwcodecodevdev1.azurecr.io/openclaw/claw-code:dev .
```

`az acr login --name clwcodecodevdev1` first if you want to push to ACR
from your workstation.

---

## LLMs

The canonical config lives in [k8s/010-openclaw-config.yaml](k8s/010-openclaw-config.yaml)
as a single template (`openclaw-config-template` ConfigMap). At pod start an
init container `render-config` decides which providers stay in based on which
API keys are present in `openclaw-secrets`. **Both keys are optional, but at
least one must be set** — if neither is, the init container fails and the pod
won't start (loud-fail beats a silent misconfig).

Resulting routing based on which keys are set:

| `MISTRAL_API_KEY` | `MINIMAX_API_KEY` | Primary chat | Image model | Notes |
|---|---|---|---|---|
| ✓ | ✓ | MiniMax M2.7 (Mistral Large as fallback) | Mistral Large 3 (Medium 3.5 fallback) | MiniMax wins chat priority; vision goes to Mistral because M2.7 is text/reasoning-only |
| ✓ | ✗ | Mistral Large | Mistral Large 3 (Medium 3.5 fallback) | MiniMax block + registry entries stripped |
| ✗ | ✓ | MiniMax M2.7 | MiniMax M2.7 (best-effort, may fail) | Mistral stripped. M2.7 likely doesn't accept image content — vision-using chats will probably error. Add `MISTRAL_API_KEY` to fix. |
| ✗ | ✗ | — | — | `check-llm-secrets` job fails the PR / deploy; init container would also fail-fast |

> Why is the image model on Mistral when both are set? MiniMax M2.7's
> public product positioning is "Self-Improvement / Reasoning" — text
> only. We could not find authoritative docs confirming vision support
> on the `api.minimax.io/anthropic` endpoint. Mistral Large 3 is
> explicitly multimodal, so it carries vision unless it's not configured.

How the render works (see `010-openclaw-config.yaml` and the
`render-config` init container in `020-deployment.yaml`):

1. **`fix-perms`** init container runs as root, `chown`s and `chmod`s
   the openclaw PVC so the unprivileged `node` user (uid 1000) can
   write to it.
2. **`render-config`** init container reads the template from the
   `openclaw-config-template` ConfigMap, then uses `node` (jq isn't in
   the base image yet) to strip provider blocks whose API key env var
   is empty/unset. The rendered config is **merged with the runtime
   state** that openclaw itself persists (gateway auth token, paired
   command owners, channel state) and written to
   `/home/node/.openclaw/openclaw.json` on the PVC.
3. Main container starts and reads its config from that file. We do
   **not** rely on `envFrom` of the rendered ConfigMap — `envFrom` skips
   keys with dots (`openclaw.json`), so that path silently no-op'd.
4. RBAC is the tightly-scoped `openclaw-config-writer` Role on the
   `openclaw` ServiceAccount — get/patch/update/create on only the
   `openclaw-config` ConfigMap. The ConfigMap is still updated by the
   init script for debuggability (`kubectl get cm openclaw-config -o yaml`).

To toggle a provider on or off: add or remove the matching
`MISTRAL_API_KEY` / `MINIMAX_API_KEY` GitHub Actions secret. The
`seal-secrets` workflow seals it into `openclaw-secrets`, the next pod
restart re-runs the render, and the config tracks the new state.

---

## Autonomous issue watcher

The cluster runs a `*/5 * * * *` CronJob in `claw-code` that
auto-fixes any GitHub issue assigned to the bot account. Each fixer
is an `openclaw agent --local` Node.js **subprocess spawned inside
the running claw-code pod**, not a separate Pod — so it inherits
the main pod's network, secrets, MCP servers, plugin registry, and
config by construction.

```
       CronJob issue-watcher           (own pod, every 5 min)
              |
       cron-issue-spawn (bash)
              |
       heartbeat-issue-tick (python)
       |                       \
GET /issues?filter=assigned     `kubectl exec claw-code-pod -- ls .fixer-locks/`
       \                       /
        \                     /
         decide toSpawn list  ←  cap at 1 active fixer per repo
                  |
        for each toSpawn entry:
        kubectl exec claw-code-pod -- nohup fixer-runner repo n url title &
                  |    (subprocess inside the claw-code container)
                  v
       fixer-runner:
         mkdir lock at ~/.openclaw/.fixer-locks/<owner>__<name>/
         clone-or-update ~/.openclaw/projects/<owner>/<name>/
         git checkout -b issue-<n>-fix
         openclaw agent --local --message "Fix issue …"
            → commit → push → open PR
         trap: rm -rf lock on exit
```

- **Concurrency ledger**: lock directories at
  `~/.openclaw/.fixer-locks/<owner>__<name>/` inside the claw-code
  pod. `mkdir` is atomic on local filesystems — the first runner
  that asks wins, everyone else exits fast. **Max 1 fixer per
  repo**, because the shared on-disk checkout can't be safely
  raced. Issues queued for a busy repo wait for the next tick.
- **Shared persistent checkout**: each repo has one working tree
  under `~/.openclaw/projects/<owner>/<name>/` on the claw-code
  PVC. Survives pod restarts, so the agent benefits from a warm
  `.git`, cached `node_modules`, etc.
- **TTL**: each fixer subprocess is bounded by the agent's
  `--timeout 3500` flag (~58 min). Stale locks older than 1h
  (planner-checked on every tick) are ignored, so a crashed fixer
  doesn't permanently hold a repo.
- **Coding agent**: same Node.js runtime as the chat bot, same
  rendered `~/.openclaw/openclaw.json` (MiniMax M2.7 primary,
  Mistral Large fallback), same MCP servers and skills.

The watcher CronJob, its service account, RBAC (the cron pod needs
`pods/exec` on the claw-code deployment's pods), and the chat-skill
ConfigMap are all in
[`k8s/050-issue-watcher.yaml`](k8s/050-issue-watcher.yaml).

### Controlling it from chat

The same manifest ships an `issue-watcher` skill (mounted at
`~/.openclaw/workspace/skills/issue-watcher/SKILL.md` via subPath
ConfigMap). The bot picks the skill up at session start and
recognises plain-text triggers:

| You type | What runs |
|---|---|
| `watcher status` | `kubectl get cronjob issue-watcher -o jsonpath=…` |
| `watcher start` | `kubectl patch cronjob issue-watcher … suspend:false` |
| `watcher stop`  | `kubectl patch … suspend:true` AND `pkill -f 'openclaw agent --local'` AND `rm -rf $HOME/.openclaw/.fixer-locks/*` |
| `watcher list`  | `ls $HOME/.openclaw/.fixer-locks/` (one line per active repo) |
| `watcher logs <repo>#<n>` | `tail $HOME/.openclaw/fixer-logs/<owner>_<name>-<n>.log` |
| `watcher kill`  | the second half of `stop` only — terminates in-flight fixers without suspending the CronJob |

`watcher stop` deliberately kills in-flight subprocesses too —
partial work is discarded, because the user's intent on "stop" is
"stop coding work right now", not "finish what's in progress".

`spec.suspend` is *deliberately absent* from the CronJob manifest
(K8s defaults it to `false`). The deploy workflow re-applies the
manifest on every push to `main`, so if we set `suspend: false` in
git, every deploy would override a chat-driven `watcher stop`. With
the field unmanaged, runtime patches survive.

### Disabling permanently

Suspend the CronJob via `watcher stop` (or `kubectl patch ... suspend:true`)
and don't unsuspend it. To drop it entirely, delete
`050-issue-watcher.yaml` from `k8s/` and merge — the next deploy will
`kubectl apply -f k8s/` without it, but the existing CronJob + RBAC
resources will linger (no garbage collection without ArgoCD). Clean
those up manually with `kubectl delete -f` against an old copy of the
file or by resource name.

### Editing the chat skill

The skill body lives inline in `k8s/050-issue-watcher.yaml`'s ConfigMap.
Edit, push, the next deploy re-applies the ConfigMap. Then
`kubectl rollout restart deployment/claw-code -n claw-code` once so
the subPath mount picks up the new content (subPath ConfigMap mounts
don't reload live).

---

## Network Policies

Two pre-configured NetworkPolicies in `k8s/040-networkpolicy.yaml`:

- `default-deny-all`: blocks all ingress/egress unless explicitly allowed
- `allow-dns`: allows egress to kube-system DNS (UDP/TCP port 53)

Add allow-rules as needed for your workloads.

---

## Deployment

K8s manifests in `k8s/` are applied to the cluster by `deploy.yml`'s
`deploy-to-aks` job on every push to `main`. The workflow:

1. Logs into Azure via OIDC.
2. Assembles the per-tool `k8s/tools/TOOLS-*.md` files into the
   `openclaw-tools-md` ConfigMap.
3. Fetches admin kubeconfig with `az aks get-credentials --admin`.
4. Runs `kubectl apply -f k8s/`.
5. `kubectl rollout restart deployment/openclaw -n openclaw` so the
   pod picks up the freshly-pushed `:latest` image and any ConfigMap
   changes.

No in-cluster GitOps controller — the GitHub Actions runner is the
deploy actor.

Secrets are handled separately by `seal-secrets.yml`: it reads GitHub
Actions secrets and `kubectl apply`s a plain Kubernetes Secret directly
to the cluster. The manifest is piped from `kubectl create -o yaml`
into `kubectl apply -f -` and never written to disk or committed — so
there is no encryption round-trip and no Sealed Secrets controller in
the cluster (unlike the claw-code-local sibling, which commits the
sealed YAML to git and needs the controller to decrypt it).

---

## Contact

For questions, tag @kstrassheim in the issue comments.
