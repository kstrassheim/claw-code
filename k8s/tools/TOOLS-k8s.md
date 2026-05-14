<!--
  Describes the kubectl + container-build capabilities wired into
  this image. claw-code itself runs on AKS but the docs are written
  generically — the bot is meant to be pointed at whichever
  cluster(s) the operator gives it access to.
-->

---

# Kubernetes — `kubectl` CLI + `mcp.servers.k8s` + container-build

## ⚠️ READ THIS FIRST — capability boundaries

The image ships **`kubectl`** (latest stable, pinned at runtime) plus
a thin **`mcp.servers.k8s`** wrapper. By default the pod runs under a
**dedicated ServiceAccount** (`openclaw`) with **deliberately narrow
RBAC**: a single Role (`openclaw-config-writer`) granting
`get/patch/update/create` on the `openclaw-config` ConfigMap in the
`openclaw` namespace. That is enough for the pod to bootstrap its
own runtime config and nothing else.

What this means in practice:

- `kubectl get pods -n <anything>` → `Forbidden` (the SA isn't bound
  to any read role on workloads).
- `kubectl get secret ...` → `Forbidden`, anywhere.
- Cluster-scoped reads (`kubectl get ns`, `kubectl get nodes`) →
  `Forbidden`.
- The only call that succeeds by default is the bot's own config
  bootstrap, which the init container already handles.

To broaden the bot's reach you (the human operator) have two choices:

1. **Grant additional Roles/RoleBindings** to the `openclaw` SA in
   chosen namespaces — extend `k8s/020-deployment.yaml` and apply.
2. **Mount a real kubeconfig** at `~/.kube/config` via a sealed
   Secret, so the bot authenticates as a separate principal with
   whatever RBAC you give that principal. This is the right path if
   the bot is meant to manage a *different* cluster than the one
   it's running in.

Until one of those is configured, **surface RBAC failures verbatim**
to the user — don't try to work around them.

## `mcp.servers.k8s` tool surface

| Tool                       | What it does                                                              |
|----------------------------|---------------------------------------------------------------------------|
| `k8s_contexts`             | List kubeconfig contexts visible to the pod.                              |
| `k8s_current_context`      | Active context + cluster + namespace.                                     |
| `k8s_can_i`                | `kubectl auth can-i <verb> <resource> [-n <ns>]` — answers "what am I allowed". |
| `k8s_get`                  | Typed `kubectl get` (resource kind, namespace, label selector). Strips `managedFields` automatically. |
| `k8s_logs`                 | `kubectl logs` with default `--tail=100` so context isn't burned on long-running pods. |
| `k8s_describe`             | `kubectl describe` for the focused-on resource.                           |
| `k8s_apply`                | Apply YAML from a string. The MCP rejects manifests that target
                              cluster-scoped resources you don't have permission for. |
| `k8s_run`                  | Escape hatch: raw `kubectl <args>`.                                        |

## Building container images

Two ways, pick based on what the build does.

### Option 1 — `podman` in-pod (rootless, vfs storage)

For small to medium images (under ~500MB layer transit), the
`podman` binary in your env builds images directly. Storage driver
is **vfs** because `/dev/fuse` isn't mounted into this container; vfs
is slower than overlay/fuse-overlayfs but works without privileged
mode.

```
podman login <registry-host>
podman build -t <registry-host>/<owner>/<image>:<tag> .
podman push <registry-host>/<owner>/<image>:<tag>
```

If the target registry uses a self-signed cert, add it to the
system CA store at build time or use `podman --tls-verify=false`
(only for trusted internal registries).

### Option 2 — kaniko `Job` (fast path, runs as a separate pod)

When the bot has Job-create perms in a namespace, write a `Job`
manifest that runs the `gcr.io/kaniko-project/executor` image.
Kaniko builds the Dockerfile and pushes the result to your registry
from inside its own pod, much faster than vfs-podman because kaniko
has its own efficient layer-copy implementation.

Pattern (apply with `kubectl apply -f <yaml> -n <namespace>`):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: build-<image-name>-<short-hash>
spec:
  ttlSecondsAfterFinished: 3600
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: kaniko
          image: gcr.io/kaniko-project/executor:latest
          args:
            - --dockerfile=Dockerfile
            - --context=git://github.com/<owner>/<repo>.git#refs/heads/<branch>
            - --destination=<registry-host>/<owner>/<image>:<tag>
```

For self-signed registries add `--insecure` and `--skip-tls-verify`.
The bot's own openclaw container may have the cert pre-installed,
but kaniko's separate pod does not — assume kaniko sees the registry
fresh.

Stream the build logs with:
```
kubectl logs -n <namespace> -f job/build-<image-name>-<short-hash>
```

### Image-tag conventions

For images pushed to a shared registry:

- **Namespace path** should match the consumer's scope (e.g.
  `<registry>/<owner-or-team>/<image>`). Don't push under
  unrelated paths even if RBAC allows it — surface the conflict
  to the user.
- **Tags**: use either `latest` for ephemeral scratch work or a
  git-sha / semver tag for anything you'd want to redeploy
  deterministically.

## Workflow rules

1. **Cache the namespace name** at the start of a task. The user
   often says "deploy to playground" — confirm what namespace they
   mean once and use it for the rest of the conversation. Don't
   ask repeatedly.

2. **Surface Forbidden errors verbatim.** If `kubectl ... -n X`
   returns `Forbidden`, that's the API server enforcing RBAC, not
   a bug. Tell the user "I don't have access to that namespace
   under the current ServiceAccount", don't try to work around it.

3. **Never echo Secret content.** If the user pastes secret content
   into chat ("store this token in the cluster"), prefer telling
   them to seal it themselves (kubeseal / external-secrets) rather
   than echoing it back into a manifest you create.

4. **Use `--show-managed-fields=false`** when piping `kubectl get`
   output through your model. The managedFields blob can be huge
   and eats context for no useful info.

5. **Use `kubectl logs --tail=100`** by default. Don't `kubectl
   logs` without a tail limit on a long-running pod — you'll burn
   model context on irrelevant ancient log lines.

## Quick reference

| Goal                                | Command                                                                |
|-------------------------------------|------------------------------------------------------------------------|
| Whoami / verify auth                | `kubectl auth whoami` or `kubectl auth can-i list pods -n <ns>`        |
| List pods in a namespace            | `kubectl get pods -n <ns>`                                             |
| Deploy a manifest                   | `kubectl apply -f manifest.yaml -n <ns>`                               |
| Watch a deployment roll             | `kubectl rollout status deployment/X -n <ns> --timeout=120s`           |
| Logs (tail 100)                     | `kubectl logs -n <ns> deploy/X --tail=100`                             |
| Exec into a pod                     | `kubectl exec -it -n <ns> deploy/X -- sh`                              |
| Port-forward to debug locally       | `kubectl port-forward -n <ns> svc/X 8080:80`                           |
| Clean up everything in a namespace  | `kubectl delete all,ingress,pvc --all -n <ns>`                         |
