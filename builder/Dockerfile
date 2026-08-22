# The coding agent's image: upstream openclaw plus the tools it works with.
#
# WHAT IS DELIBERATELY NOT HERE
# No local model runtime, no speech or audio stack, no personal-data
# integrations and no document corpus. This image exists to read code, run
# tests and drive a browser; everything it carries is something one of the
# three subsystems — solver, tester, reviewer — actually invokes. Anything
# else is attack surface on a container that executes model output.
#
# Every version is pinned in the VERSIONS file at the repository root and
# arrives as a --build-arg, so what shipped is answerable from git alone.
# The build args come from the image-build step in .github/workflows/.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# Strip the bundled skills the agent could otherwise load at runtime.
#
# ALLOWLIST, NOT DENYLIST. The previous form named the two skills known to be
# dangerous (mcporter, skill-creator) and kept everything else. That is the
# wrong way round: an upstream release that adds a fifty-first skill ships it
# to the agent silently, and nobody finds out until it uses one. The list of
# what may survive lives in BUNDLED_SKILLS_ALLOWED — currently empty, because
# the bot's own skills arrive as reviewed ConfigMap mounts under the workspace
# and nothing upstream ships needs to be kept.
#
# THE ASSERTION IS THE POINT. `rm -rf` on a path upstream has moved exits 0
# and removes nothing. That failure is silent and total: every bundled skill
# ships and the build stays green. So after removing, this enumerates what is
# actually loadable in the image and fails the build on anything the allowlist
# does not name. The SKILLS-LOCKED-ASSERT sentinel is what CI greps for, so
# deleting this block is itself a CI failure rather than a quiet loss.
#
# SKILLS DO NOT ALL LIVE IN /app/skills. They also ship inside extensions, at
# /app/extensions/<ext>/skills/<name>/, and again in the built mirror under
# /app/dist/. An earlier version of this block removed only /app/skills/*/ and
# reported every one of them stripped — while four skills carried on shipping
# from the other two locations. The assertion is what caught it.
#
# So the removal and the assertion are now driven by THE SAME `find`. A skill
# root is any directory `<...>/skills/<name>/` holding a SKILL.md, wherever it
# is, and the two passes cannot disagree about what counts as one because
# neither has its own opinion. node_modules is excluded: a dependency shipping
# a SKILL.md is not something that gets loaded as a skill, and matching there
# produced noise rather than findings.
USER root
COPY BUNDLED_SKILLS_ALLOWED /usr/local/share/BUNDLED_SKILLS_ALLOWED
COPY --chmod=0755 list-loadable-skills.sh /usr/local/bin/list-loadable-skills
RUN set -eu; \
    sed 's/#.*//' /usr/local/share/BUNDLED_SKILLS_ALLOWED | awk 'NF' | sort -u > /tmp/allowed; \
    find /app -name SKILL.md -not -path '*/node_modules/*' 2>/dev/null \
      | grep '/skills/' \
      | sed -E 's#(.*/skills/[^/]+)/.*#\1#' | sort -u > /tmp/roots || true; \
    while IFS= read -r root; do \
      [ -n "$root" ] || continue; \
      [ -d "$root" ] || continue; \
      name="$(basename "$root")"; \
      if grep -qxF "$name" /tmp/allowed; then \
        echo "keep   $name  ($root)"; \
      else \
        echo "strip  $name  ($root)"; \
        rm -rf "$root"; \
      fi; \
    done < /tmp/roots; \
    echo "SKILLS-LOCKED-ASSERT: verifying nothing unlisted survived"; \
    /usr/local/bin/list-loadable-skills > /tmp/remaining; \
    if [ -s /tmp/remaining ] && grep -vxF -f /tmp/allowed /tmp/remaining > /tmp/extra; then \
      echo "FATAL: bundled skills survived the strip that the allowlist does not name:" >&2; \
      cat /tmp/extra >&2; \
      exit 1; \
    fi; \
    rm -f /tmp/allowed /tmp/roots /tmp/remaining /tmp/extra; \
    echo "SKILLS-LOCKED-ASSERT: ok"

# git-lfs. Target repositories keep large binary assets under LFS, and without
# the client a clone hands the agent ~130-byte pointer files instead of the
# content. That does not fail loudly: it looks like corrupt data, and any
# commit touching such a file overwrites the pointer with whatever the agent
# thought it was editing, quietly destroying the object for everyone.
#
# It has to be in the IMAGE, not installed at runtime. The agent runs as uid
# 1000 and cannot write to /usr/local/bin, so a runtime install lands in some
# directory that is not on PATH and `git lfs` still reports "not a git command".
#
# `git lfs install --system` writes the clean/smudge/filter-process entries
# into /etc/gitconfig, so every repository the agent clones is covered without
# a per-repo or per-user step it has to remember. The final `git config
# --system --get` is the verification: it fails the BUILD if the filters did
# not actually land, rather than leaving the first LFS clone to discover it.
ARG GIT_LFS_VERSION
USER root
RUN set -eu; \
    ARCH="$(dpkg --print-architecture)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /tmp/git-lfs.tar.gz \
      "https://github.com/git-lfs/git-lfs/releases/download/v${GIT_LFS_VERSION}/git-lfs-linux-${ARCH}-v${GIT_LFS_VERSION}.tar.gz"; \
    tar -xzf /tmp/git-lfs.tar.gz -C /tmp; \
    install -m 0755 "/tmp/git-lfs-${GIT_LFS_VERSION}/git-lfs" /usr/local/bin/git-lfs; \
    rm -rf /tmp/git-lfs.tar.gz "/tmp/git-lfs-${GIT_LFS_VERSION}"; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    git lfs install --system; \
    git lfs version; \
    git config --system --get filter.lfs.process

# gh CLI + GitHub MCP server, installed for autonomous git/github work.
# Image is arm64-only (AKS uses ARM); upstream release archives use `arm64`.
ARG GH_CLI_VERSION
ARG GITHUB_MCP_SERVER_VERSION
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates jq \
 && curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_arm64.tar.gz" \
 && tar -xzf /tmp/gh.tar.gz -C /tmp \
 && mv "/tmp/gh_${GH_CLI_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_CLI_VERSION}_linux_arm64" \
 && curl -fsSL -o /tmp/github-mcp.tar.gz \
      "https://github.com/github/github-mcp-server/releases/download/v${GITHUB_MCP_SERVER_VERSION}/github-mcp-server_Linux_arm64.tar.gz" \
 && tar -xzf /tmp/github-mcp.tar.gz -C /tmp \
 && mv /tmp/github-mcp-server /usr/local/bin/github-mcp-server \
 && rm -rf /tmp/github-mcp.tar.gz \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/gh --version

# tea CLI + Gitea MCP server, the Gitea half of the forge layer's tooling.
# Same shape as the gh block above; arm64 only, same as the image. `tea`
# publishes a bare binary rather than an archive, the MCP a tar.gz.
ARG TEA_VERSION
ARG GITEA_MCP_VERSION
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /usr/local/bin/tea \
      "https://gitea.com/gitea/tea/releases/download/v${TEA_VERSION}/tea-${TEA_VERSION}-linux-arm64" \
 && chmod 0755 /usr/local/bin/tea \
 && curl -fsSL -o /tmp/gitea-mcp.tar.gz \
      "https://gitea.com/gitea/gitea-mcp/releases/download/v${GITEA_MCP_VERSION}/gitea-mcp_Linux_arm64.tar.gz" \
 && tar -xzf /tmp/gitea-mcp.tar.gz -C /tmp \
 && mv /tmp/gitea-mcp /usr/local/bin/gitea-mcp \
 && chmod 0755 /usr/local/bin/gitea-mcp \
 && rm -rf /tmp/gitea-mcp.tar.gz \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/tea --version

# Azure DevOps: the `az devops` CLI extension and the official MCP server.
# The extension rides on the azure-cli installed below rather than being its
# own binary, so it has no version pin of its own. The MCP is npm and is
# vendored at build time rather than resolved by npx at run time — a pod that
# reaches the network on first use is a pod that fails when it cannot.
ARG AZDO_MCP_VERSION
USER root
RUN npm install -g "@azure-devops/mcp@${AZDO_MCP_VERSION}" \
 && npm cache clean --force

# Baked-in .gitconfig: credential helper reads $GITHUB_TOKEN at git-runtime
RUN install -d -o 1000 -g 1000 /home/node \
 && cat > /home/node/.gitconfig <<'GITCONFIG'
[credential "https://github.com"]
	helper = "!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f"
GITCONFIG
RUN chown 1000:1000 /home/node/.gitconfig

# Entra / Azure auth tooling
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl apt-transport-https lsb-release gnupg \
 && mkdir -p /etc/apt/keyrings \
 && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
 && AZ_REPO="$(lsb_release -cs)" \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $AZ_REPO main" \
      > /etc/apt/sources.list.d/azure-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends azure-cli python3-pyotp \
 && az extension add --name azure-devops --only-show-errors \
 && apt-get purge -y --auto-remove apt-transport-https lsb-release gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/bin/az --version | head -3

# MongoDB driver for the planning store (builder/planning_store.py).
#
# The only pip package installed into the image's SYSTEM python — the security
# scanners further down get their own virtualenvs — and it is here rather than
# vendored because the alternative is speaking the MongoDB wire protocol by
# hand.
#
# --break-system-packages: the base is Debian and its python3 is
# externally-managed (PEP 668), so pip refuses to install into it without
# this. That is the correct override here — the image IS the environment,
# there is no venv for a bash runner to activate, and every module is imported
# by bare name from /usr/local/bin.
#
# planning_store imports it LAZILY, inside the functions that reach the
# server. If this layer ever fails or is removed, planning documents still
# spool to the workspace volume and flush once a driver exists; nothing that
# solves an issue breaks.
#
# The default mirrors PYMONGO_VERSION in the root VERSIONS file and must be
# kept equal to it. It exists so the image still builds when the build is
# invoked without --build-arg: an empty ARG here would expand to `pymongo==`,
# which pip rejects, and the whole image would fail on a missing variable
# rather than on anything to do with the driver.
#
# python3-pip is installed HERE rather than assumed. The base image ships a
# python3 interpreter but no pip, so a bare `pip install` exits 127 — "command
# not found", which reads as a broken package name rather than a missing tool.
# The scanner layer further down needs pip and venv too; apt is idempotent, so
# installing it in both places costs nothing and neither layer depends on the
# other having run first.
#
# Invoked as `python3 -m pip`, not `pip`: Debian's packaging has moved which of
# pip / pip3 / neither ends up on PATH more than once, and the module form is
# the spelling that has always worked.
ARG PYMONGO_VERSION=4.17.0
USER root
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    python3 -m pip install --no-cache-dir --break-system-packages \
      "pymongo==${PYMONGO_VERSION}"; \
    python3 -c "import pymongo; print('pymongo', pymongo.__version__)"

# k8s-mcp + azure-mcp
COPY --chown=1000:1000 k8s-mcp /opt/k8s-mcp
COPY --chown=1000:1000 azure-mcp /opt/azure-mcp
RUN cd /opt/k8s-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x k8s-mcp.mjs \
 && cd /opt/azure-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x azure-mcp.mjs

# AWS CLI v2 + gcloud + aliyun
ARG AWS_CLI_VERSION
ARG GCLOUD_VERSION
ARG ALIYUN_CLI_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && AWS_ARCH=$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64") \
 && GCP_ARCH=$([ "$ARCH" = "arm64" ] && echo "arm" || echo "x86_64") \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip \
 && curl -fsSL -o /tmp/awscliv2.zip \
      "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWS_CLI_VERSION}.zip" \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install --update \
 && rm -rf /tmp/awscliv2.zip /tmp/aws \
 && curl -fsSL -o /tmp/gcloud.tgz \
      "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${GCP_ARCH}.tar.gz" \
 && tar -xzf /tmp/gcloud.tgz -C /opt \
 && rm /tmp/gcloud.tgz \
 && /opt/google-cloud-sdk/install.sh --quiet --usage-reporting=false --command-completion=false --path-update=false >/dev/null \
 && ln -sf /opt/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud \
 && ln -sf /opt/google-cloud-sdk/bin/gsutil /usr/local/bin/gsutil \
 && curl -fsSL -o /tmp/aliyun.tgz \
      "https://github.com/aliyun/aliyun-cli/releases/download/v${ALIYUN_CLI_VERSION}/aliyun-cli-linux-${ALIYUN_CLI_VERSION}-${ARCH}.tgz" \
 && tar -xzf /tmp/aliyun.tgz -C /usr/local/bin aliyun \
 && chmod 0755 /usr/local/bin/aliyun \
 && rm /tmp/aliyun.tgz \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/aws --version | head -1 \
 && /usr/local/bin/gcloud --version | head -1 \
 && /usr/local/bin/aliyun --version | head -1

# aws-mcp + gcp-mcp + alicloud-mcp
COPY --chown=1000:1000 aws-mcp /opt/aws-mcp
COPY --chown=1000:1000 gcp-mcp /opt/gcp-mcp
COPY --chown=1000:1000 alicloud-mcp /opt/alicloud-mcp
RUN cd /opt/aws-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x aws-mcp.mjs \
 && cd /opt/gcp-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x gcp-mcp.mjs \
 && cd /opt/alicloud-mcp \
 && npm install --omit=dev --no-audit --no-fund --loglevel=error \
 && chmod +x alicloud-mcp.mjs

# Terraform CLI + terraform-mcp-server
ARG TERRAFORM_VERSION
ARG TERRAFORM_MCP_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip \
 && curl -fsSL -o /tmp/terraform.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip" \
 && unzip -q /tmp/terraform.zip -d /usr/local/bin \
 && chmod 0755 /usr/local/bin/terraform \
 && rm /tmp/terraform.zip \
 && curl -fsSL -o /tmp/tf-mcp.zip \
      "https://releases.hashicorp.com/terraform-mcp-server/${TERRAFORM_MCP_VERSION}/terraform-mcp-server_${TERRAFORM_MCP_VERSION}_linux_${ARCH}.zip" \
 && unzip -q /tmp/tf-mcp.zip -d /usr/local/bin terraform-mcp-server \
 && chmod 0755 /usr/local/bin/terraform-mcp-server \
 && rm /tmp/tf-mcp.zip \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/* \
 && terraform version | head -1 \
 && terraform-mcp-server --version 2>&1 | head -1 || true

# OpenTofu CLI + its registry MCP.
#
# Alongside terraform, not instead of it. A project's state file and provider
# lockfile are written by whichever binary produced them, and running the other
# one against them is at best a rewrite and at worst a state migration nobody
# asked for — so the bot carries both and uses whichever the repository has
# committed to.
#
# The MCP is an npm package rather than a release binary. It is pinned exactly
# and installed at build time: `npx` would fetch it on first use, from a pod
# whose egress is restricted, at the moment the agent is trying to do
# something else.
ARG OPENTOFU_VERSION
ARG OPENTOFU_MCP_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip \
 && curl -fsSL -o /tmp/tofu.zip \
      "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_${ARCH}.zip" \
 && unzip -q -o /tmp/tofu.zip -d /tmp/tofu \
 && install -m 0755 /tmp/tofu/tofu /usr/local/bin/tofu \
 && rm -rf /tmp/tofu.zip /tmp/tofu \
 && npm install -g --no-audit --no-fund --loglevel=error \
      "@opentofu/opentofu-mcp-server@${OPENTOFU_MCP_VERSION}" \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/* \
 && tofu version | head -1 \
 && command -v opentofu-mcp-server

# Chromium. libnss3-tools provides `certutil`, used at pod start to trust the
# internal CA in Chromium's NSS store (see render-config.sh) so the tester can
# open internal HTTPS deploy URLs without ERR_CERT_AUTHORITY_INVALID.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium fonts-liberation \
      libnss3 libnss3-tools libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
      libgbm1 libasound2 libxshmfence1 \
 && rm -rf /var/lib/apt/lists/* \
 && chromium --version

# kubectl + kubelogin + podman
#
# kubelogin is the credential plugin kubectl EXECS when a kubeconfig comes from
# `az aks get-credentials` against an Entra-integrated AKS cluster — which is
# what builder/aks-login produces. kubectl on its own cannot authenticate to
# such a cluster at all: the first call dies with
# `exec: "kubelogin": executable file not found in $PATH`. Talking to this
# pod's OWN cluster goes through the in-cluster ServiceAccount and does not
# need it; reaching any OTHER cluster as the bot's Entra identity does.
ARG KUBELOGIN_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && KUBECTL_VERSION="$(curl -sL https://dl.k8s.io/release/stable.txt)" \
 && curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && /usr/local/bin/kubectl version --client=true --output=yaml | head -3 \
 && apt-get update \
 # unzip is BUILD-ONLY here and purged again below, exactly as the aws-cli and
 # terraform layers do — it is deliberately not in the shipped image, so it
 # cannot be assumed present from an earlier layer.
 && apt-get install -y --no-install-recommends unzip \
 && curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /tmp/kubelogin.zip \
      "https://github.com/Azure/kubelogin/releases/download/v${KUBELOGIN_VERSION}/kubelogin-linux-${ARCH}.zip" \
 # The per-platform zip still nests the binary under bin/<os>_<arch>/.
 && unzip -q -j /tmp/kubelogin.zip "bin/linux_${ARCH}/kubelogin" -d /usr/local/bin \
 && chmod 0755 /usr/local/bin/kubelogin \
 && rm -f /tmp/kubelogin.zip \
 && apt-get purge -y --auto-remove unzip \
 && apt-get install -y --no-install-recommends \
      podman buildah fuse-overlayfs uidmap slirp4netns \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/local/bin/kubelogin --version \
 && podman --version
RUN install -d -o 1000 -g 1000 /home/node/.config/containers \
 && cat > /home/node/.config/containers/storage.conf <<'STORAGE'
[storage]
driver = "vfs"
runroot = "/home/node/.local/share/containers/run"
graphroot = "/home/node/.local/share/containers/storage"
STORAGE
RUN cat > /home/node/.config/containers/containers.conf <<'CONTAINERS'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
runtime = "crun"
CONTAINERS
RUN chown -R 1000:1000 /home/node/.config

# code-server
ARG CODE_SERVER_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && curl -fsSL -o /tmp/code-server.deb \
      "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_${ARCH}.deb" \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/code-server.deb \
 && rm /tmp/code-server.deb \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o 1000 -g 1000 /home/node/.config/code-server \
 && install -d -o 1000 -g 1000 /home/node/.local/share/code-server \
 && code-server --version | head -1

# debug-mcp
COPY debug-mcp /opt/debug-mcp
RUN chown -R 1000:1000 /opt/debug-mcp \
 && cd /opt/debug-mcp \
 && su node -c "cd /opt/debug-mcp && npm install --omit=dev --no-audit --no-fund --loglevel=error" \
 && chmod +x /opt/debug-mcp/debug-mcp.mjs

# Code scanners behind mcp.servers.security and mcp.servers.semgrep — see
# k8s/tools/TOOLS-security.md for the tool surface these have to provide:
#   - semgrep + semgrep-mcp (the official MCP server) — multi-language SAST
#   - bandit (Python SAST) + pip-audit (Python dependency CVEs)
#   - gitleaks (secrets in a working tree AND in git history)
#   - pwsh 7 + PSScriptAnalyzer (PowerShell lint incl. the security rules)
# npm audit, the sixth tool, is already available via the base image's node.
#
# TWO VIRTUALENVS, NOT ONE, AND NOT THE SYSTEM PYTHON. Debian's python3 is
# externally managed (PEP 668), so nothing here belongs in it — and semgrep and
# semgrep-mcp cannot share an environment with each other either: they pin
# INCOMPATIBLE exact versions of the `mcp` package (semgrep wants mcp==1.23.3,
# semgrep-mcp wants mcp==1.12.2), so a single venv cannot resolve. semgrep-mcp
# therefore gets /opt/semgrep-venv to itself — the exact path
# k8s/010-openclaw-config.yaml starts it from — and it reaches the semgrep CLI
# as a subprocess resolved from PATH, which the symlink below provides.
ARG SEMGREP_VERSION
ARG SEMGREP_MCP_VERSION
ARG BANDIT_VERSION
ARG PIP_AUDIT_VERSION
ARG GITLEAKS_VERSION
ARG PWSH_VERSION
ARG PSSCRIPTANALYZER_VERSION
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3-venv python3-pip \
 # libicu is versioned per Debian release (libicu72 on bookworm, ...) and both
 # pwsh and the .NET SDK below hard-require it — resolve whatever this base
 # ships instead of hardcoding a soname that breaks on the next base bump.
 && apt-get install -y --no-install-recommends \
      "$(apt-cache search --names-only '^libicu[0-9]+$' | awk '{print $1}' | sort -V | tail -1)" \
 && python3 -m venv /opt/security-venv \
 && /opt/security-venv/bin/pip install --no-cache-dir \
      "semgrep==${SEMGREP_VERSION}" \
      "bandit==${BANDIT_VERSION}" \
      "pip-audit==${PIP_AUDIT_VERSION}" \
 && python3 -m venv /opt/semgrep-venv \
 && /opt/semgrep-venv/bin/pip install --no-cache-dir \
      "semgrep-mcp==${SEMGREP_MCP_VERSION}" \
 # security-mcp shells out to these by bare name, and TOOLS-security.md
 # documents them as CLI fallbacks, so they must resolve on PATH.
 && ln -sf /opt/security-venv/bin/semgrep    /usr/local/bin/semgrep \
 && ln -sf /opt/security-venv/bin/bandit     /usr/local/bin/bandit \
 && ln -sf /opt/security-venv/bin/pip-audit  /usr/local/bin/pip-audit \
 && curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /tmp/gitleaks.tar.gz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${ARCH}.tar.gz" \
 && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
 && chmod 0755 /usr/local/bin/gitleaks \
 && rm /tmp/gitleaks.tar.gz \
 && mkdir -p /opt/microsoft/powershell/7 \
 && curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /tmp/pwsh.tar.gz \
      "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-${ARCH}.tar.gz" \
 && tar -xzf /tmp/pwsh.tar.gz -C /opt/microsoft/powershell/7 \
 && chmod 0755 /opt/microsoft/powershell/7/pwsh \
 && ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh \
 && rm /tmp/pwsh.tar.gz \
 && mkdir -p /usr/local/share/powershell/Modules \
 # Bootstrap the NuGet package provider EXPLICITLY. Save-Module otherwise
 # prompts for it on first use, and a prompt in a -NonInteractive shell is an
 # error, not a default-yes. -Scope AllUsers so the provider is in the image
 # for the Az/Pester layer below rather than in some transient HOME.
 && pwsh -NoProfile -NonInteractive -Command \
      "Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Scope AllUsers -Force | Out-Null; \
       Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
       Save-Module -Name PSScriptAnalyzer -RequiredVersion ${PSSCRIPTANALYZER_VERSION} -Path /usr/local/share/powershell/Modules" \
 && rm -rf /var/lib/apt/lists/* \
 # Verify here, so a broken scanner fails the BUILD instead of the first tool
 # call of the first review that needed it.
 && /usr/local/bin/semgrep --version \
 && test -x /opt/semgrep-venv/bin/semgrep-mcp \
 # No pipes in these: `cmd | head` reports HEAD's exit status, so a scanner
 # that failed to install would still pass the guard.
 && /usr/local/bin/bandit --version \
 && /usr/local/bin/pip-audit --version \
 && /usr/local/bin/gitleaks version \
 && pwsh -NoProfile -NonInteractive -Command "(Get-Module -ListAvailable PSScriptAnalyzer).Version.ToString()"

# Azure data-platform tooling — see k8s/tools/TOOLS-powershell.md:
#   - Pester — the PowerShell test framework. The solver is required to make
#     a runbook's unit tests pass locally before it opens a pull request.
#   - Az.Accounts / Az.Automation / Az.DataFactory / Az.Synapse — ONLY these
#     four, never the Az meta-module, which is several hundred MB of provider
#     surface nothing here touches.
#   - msodbcsql18 + sqlcmd — so the AGENT can apply .sql files and run ad-hoc
#     queries in its own shell.
# Everything authenticates as the bot's Entra identity through the shared
# ~/.azure token cache; nothing here accepts a connection string or key.
#
# The Microsoft signing key was already dearmoured into /etc/apt/keyrings by
# the Entra layer above, and gnupg was purged with it — so this reuses that
# keyring rather than resurrecting gnupg to write a second copy. The `test -s`
# turns a missing keyring into an obvious failure on this line instead of an
# unsigned-repository error twenty lines later.
ARG PESTER_VERSION
ARG AZ_ACCOUNTS_VERSION
ARG AZ_AUTOMATION_VERSION
ARG AZ_DATAFACTORY_VERSION
ARG AZ_SYNAPSE_VERSION
USER root
RUN . /etc/os-release \
 && test -s /etc/apt/keyrings/microsoft.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/${VERSION_ID}/prod ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
      msodbcsql18 mssql-tools18 unixodbc \
 && ln -sf /opt/mssql-tools18/bin/sqlcmd /usr/local/bin/sqlcmd \
 && ln -sf /opt/mssql-tools18/bin/bcp    /usr/local/bin/bcp \
 && pwsh -NoProfile -NonInteractive -Command \
      "Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Scope AllUsers -Force | Out-Null; \
       Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
       Save-Module -Name Pester         -RequiredVersion ${PESTER_VERSION}         -Path /usr/local/share/powershell/Modules; \
       Save-Module -Name Az.Accounts    -RequiredVersion ${AZ_ACCOUNTS_VERSION}    -Path /usr/local/share/powershell/Modules; \
       Save-Module -Name Az.Automation  -RequiredVersion ${AZ_AUTOMATION_VERSION}  -Path /usr/local/share/powershell/Modules; \
       Save-Module -Name Az.DataFactory -RequiredVersion ${AZ_DATAFACTORY_VERSION} -Path /usr/local/share/powershell/Modules; \
       Save-Module -Name Az.Synapse     -RequiredVersion ${AZ_SYNAPSE_VERSION}     -Path /usr/local/share/powershell/Modules" \
 && rm -rf /var/lib/apt/lists/* \
 # Fail the build loudly rather than ship an image whose agent silently cannot
 # reach a database or load the modules its runbook tests import.
 && sqlcmd -? >/dev/null 2>&1 \
 && pwsh -NoProfile -NonInteractive -Command \
      "@('Pester','Az.Accounts','Az.Automation','Az.DataFactory','Az.Synapse') | ForEach-Object { \
         if (-not (Get-Module -ListAvailable \$_)) { throw \"missing module: \$_\" }; \
         '{0} {1}' -f \$_, (Get-Module -ListAvailable \$_).Version[0] }"

# .NET SDK + SqlPackage — SSDT/dacpac database projects.
#
# A project whose schema lives in a `.sqlproj` (MSBuild.Sdk.SqlProj) is gated
# in CI by `dotnet build …sqlproj` followed by a `sqlpackage /a:Publish` onto a
# throwaway SQL Server. The issue solver has to be able to run that same gate
# BEFORE it pushes — otherwise it edits schema blind and every mistake costs a
# full pipeline round-trip. sqlpackage is a .NET global tool, so it needs the
# SDK anyway.
#
# Installed via Microsoft's dotnet-install.sh, NOT apt: packages.microsoft.com
# publishes no dotnet-sdk-8.0 for arm64 (the only arch this image is built
# for), so an apt install fails outright. The script is arch-agnostic and takes
# a CHANNEL, which is what a project's global.json needs — a `rollForward:
# latestFeature` pin on 8.0.x stays inside 8.0 and will NOT accept a 10.0 SDK.
#
# The .NET ICU dependency is already satisfied by the libicu the scanner layer
# above installs version-agnostically.
#
# sqlpackage goes to a shared tool root rather than ~/.dotnet/tools so it
# survives the PVC-mounted home the pod runs with.
ARG DOTNET_SDK_VERSION
ARG SQLPACKAGE_VERSION
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_TOOLS=/opt/dotnet-tools \
    PATH=/usr/share/dotnet:/opt/dotnet-tools:$PATH
USER root
RUN curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /tmp/dotnet-install.sh "https://dot.net/v1/dotnet-install.sh" \
 && chmod +x /tmp/dotnet-install.sh \
 && /tmp/dotnet-install.sh --channel "${DOTNET_SDK_VERSION}" \
      --install-dir "$DOTNET_ROOT" --no-path \
 && rm -f /tmp/dotnet-install.sh \
 && chmod -R a+rX "$DOTNET_ROOT" \
 && dotnet --version \
 && dotnet tool install --tool-path "$DOTNET_TOOLS" \
      microsoft.sqlpackage --version "${SQLPACKAGE_VERSION}" \
 && chmod -R a+rX "$DOTNET_TOOLS" \
 # Fail the build rather than ship an image whose solver silently cannot
 # validate a schema change.
 && sqlpackage /version
# Debian's /etc/profile OVERWRITES PATH outright ("/usr/local/bin:/usr/bin:
# /bin:...") before it sources /etc/profile.d, so a LOGIN shell loses the two
# directories set by ENV PATH above and `dotnet` / `sqlpackage` become
# "command not found". Every other tool in this image lives in /usr/local/bin
# and survives that reset, which is exactly why the gap is easy to miss — it
# hits only these two, and only in a login shell.
#
# The drop-in is sourced AFTER the reset (profile.d is the last thing
# /etc/profile does), so it wins. Idempotent, so re-sourcing a profile does not
# stack duplicate entries onto PATH.
RUN printf '%s\n' \
      '# Re-add the .NET paths that /etc/profile drops (see builder/Dockerfile).' \
      'case ":$PATH:" in' \
      '  *":/usr/share/dotnet:"*) ;;' \
      '  *) PATH="/usr/share/dotnet:/opt/dotnet-tools:$PATH" ;;' \
      'esac' \
      'export PATH' \
      > /etc/profile.d/10-dotnet.sh \
 && chmod 0644 /etc/profile.d/10-dotnet.sh \
 # Prove the fix in the shell that was actually broken.
 && bash -lc 'command -v dotnet && command -v sqlpackage'

# security-mcp — the in-house wrapper exposing the scanners above as MCP tools.
# Started by k8s/010-openclaw-config.yaml as /opt/security-mcp/security-mcp.mjs.
COPY security-mcp /opt/security-mcp
RUN chown -R 1000:1000 /opt/security-mcp \
 && su node -c "cd /opt/security-mcp && npm install --omit=dev --no-audit --no-fund --loglevel=error" \
 && chmod +x /opt/security-mcp/security-mcp.mjs

# Live DAST scanners behind mcp.servers.pentest — see TOOLS-pentest.md:
#   - nuclei     — template-based web DAST (CVEs, exposures, misconfig,
#                  default creds, security headers). The community templates
#                  are baked in at /opt/nuclei-templates so a scan is
#                  reproducible and needs no fetch at run time.
#   - testssl.sh — TLS/SSL posture of the deployed endpoint (a bash script;
#                  it uses the system openssl already present).
#
# The binaries are installed OUTSIDE $PATH (nuclei in /opt/pentest-bin,
# testssl in /opt/testssl) and are deliberately NOT symlinked into
# /usr/local/bin. Only pentest-mcp invokes them, by absolute path — so no
# agent's exec shell can run `nuclei` / `testssl.sh` by name, and the pen-test
# capability is reachable ONLY through the (tester-gated, fail-closed) MCP.
# Moving either onto PATH would silently hand every agent a live scanner.
ARG NUCLEI_VERSION
ARG NUCLEI_TEMPLATES_VERSION
ARG TESTSSL_VERSION
USER root
# NOTE: this RUN must NOT end in `|| true`. `A && B && … && Z || true` parses
# as `(A && … && Z) || true`, so one trailing `|| true` swallows a failure
# ANYWHERE in the chain — which is precisely how a template-less scanner ships
# unnoticed and then reports a clean scan it never performed. The templates are
# baked at a PINNED tag rather than fetched with `nuclei -update-templates`,
# because that resolves the version through a GitHub API call which can
# silently no-op in CI (exit 0, download nothing). The guards below are hard
# and unmasked for the same reason.
#
# THE TEMPLATES AND testssl COME FROM `git clone`, NOT FROM A TARBALL URL.
# `/archive/refs/tags/<tag>.tar.gz` is generated on demand, and that generation
# is throttled per source IP — it returns 429 even to an authenticated request,
# and it does so independently of the ordinary API rate limit. On a runner that
# shares an egress address with anything else busy, this layer therefore fails
# with `curl (22) … 429` while every release-asset download in the same build
# succeeds, which reads like a bad pin and is not one.
#
# Retrying does not help: `--retry-delay` OVERRIDES the server's Retry-After,
# so the five retries all land inside the same throttle window. Cloning a
# single pinned tag at depth 1 goes through a different backend that is not
# rate-limited this way, costs about the same bytes, and is just as
# deterministic — the tag is still the thing being pinned.
RUN ARCH=$(dpkg --print-architecture) \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip bsdmainutils dnsutils \
 && mkdir -p /opt/pentest-bin \
 && curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 \
      -o /tmp/nuclei.zip \
      "https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VERSION}/nuclei_${NUCLEI_VERSION}_linux_${ARCH}.zip" \
 && unzip -q /tmp/nuclei.zip -d /tmp/nuclei \
 && mv /tmp/nuclei/nuclei /opt/pentest-bin/nuclei \
 && chmod 0755 /opt/pentest-bin/nuclei \
 && rm -rf /tmp/nuclei.zip /tmp/nuclei \
 && git -c advice.detachedHead=false clone --depth 1 --quiet \
      --branch "v${NUCLEI_TEMPLATES_VERSION}" \
      https://github.com/projectdiscovery/nuclei-templates.git /opt/nuclei-templates \
 && rm -rf /opt/nuclei-templates/.git \
 && chown -R 1000:1000 /opt/nuclei-templates /opt/pentest-bin \
 && git -c advice.detachedHead=false clone --depth 1 --quiet \
      --branch "v${TESTSSL_VERSION}" \
      https://github.com/testssl/testssl.sh.git /opt/testssl \
 && rm -rf /opt/testssl/.git \
 && chmod 0755 /opt/testssl/testssl.sh \
 # Hard guards (no `|| true`): fail the build if either binary is missing or
 # the template tree holds no template at all.
 && test -x /opt/pentest-bin/nuclei \
 && test -d /opt/nuclei-templates \
 && test -n "$(find /opt/nuclei-templates -name '*.yaml' -print -quit)" \
 && test -x /opt/testssl/testssl.sh \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/*
# Informational only — isolated in its own layer so a version print can never
# mask a failure in the install RUN above.
RUN /opt/pentest-bin/nuclei -version 2>&1 | head -2 || true; \
    /opt/testssl/testssl.sh --version 2>&1 | head -2 || true

# pentest-mcp — the in-house wrapper: nuclei + testssl.sh as MCP tools, with
# the host allowlist enforced in code. Started by k8s/010-openclaw-config.yaml
# as /opt/pentest-mcp/pentest-mcp.mjs.
COPY pentest-mcp /opt/pentest-mcp
RUN chown -R 1000:1000 /opt/pentest-mcp \
 && su node -c "cd /opt/pentest-mcp && npm install --omit=dev --no-audit --no-fund --loglevel=error" \
 && chmod +x /opt/pentest-mcp/pentest-mcp.mjs
ENV NUCLEI_TEMPLATES_DIR=/opt/nuclei-templates

# Issue-watcher scripts invoked by the issue-watcher CronJob.
#   heartbeat-issue-tick — read-only planner: emits JSON of repos +
#                          assigned issues + active Jobs per repo.
#   cron-issue-spawn     — runs the planner and, for each toSpawn entry,
#                          `kubectl exec`s into THIS pod and backgrounds a
#                          fixer-runner subprocess there. It does not create
#                          Jobs and does not start a second agent pod: every
#                          run happens inside the openclaw container, which
#                          is why the two concurrency slots are contended
#                          within one pod rather than across pods.
#                          (It did apply a Job per entry when it was written
#                          in May; the script changed and this comment did
#                          not, which reads as though the CronJobs need the
#                          agent image. They do not — see builder/cron/.)
# ---------------------------------------------------------------------------
# LAST OF THE INSTALLS, ON PURPOSE.
#
# Docker invalidates every layer after the first one that changes, so anything
# whose version moves often belongs BELOW the things that are slow to rebuild.
# This block first went in around the chromium layer, which put it above the
# security venvs (semgrep, bandit, pip-audit), the .NET SDK and nuclei — so
# bumping a mermaid version rebuilt all of them. It only needs to be after
# chromium, and here it is.
# ---------------------------------------------------------------------------
# mermaid-cli, for `mermaid-render`.
#
# WHY IT WAS MISSING AND WHY THAT WAS INVISIBLE
# `mermaid-render` and its theme file have shipped in this image for months
# (see the COPYs further down), and the planning skill documents piping charts
# through it. But `mmdc` was never installed, so every call ended at the
# wrapper's own guard — "mmdc is not installed in this image" — and diagram
# output silently degraded to ASCII. The tooling was complete apart from the
# one package it drives.
#
# NO SECOND BROWSER. mermaid renders in a headless browser, and puppeteer
# downloads its own Chromium by default — a second ~150MB browser in an image
# that already installs one directly above. PUPPETEER_SKIP_DOWNLOAD stops
# that, and PUPPETEER_EXECUTABLE_PATH points it at the system binary. Both are
# set as ENV rather than only at install time: mmdc reads them at RUN time
# too, and an install-only value would work in the build and fail in the pod.
ENV PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# The puppeteer config mermaid-render has always passed and this image has
# never had. Chromium refuses to run as root with its sandbox on, and in a
# container there is no user namespace to sandbox into — so the renderer named
# this file, and `mmdc` failed on the missing path. Two absences, one symptom:
# charts silently fell back to ASCII.
#
# --disable-dev-shm-usage as well: the default /dev/shm in a container is 64MB
# and chromium crashes rendering anything large, which looks like a broken
# diagram rather than a full tmpfs.
RUN printf '%s\n' \
      '{"args":["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"]}' \
      > /etc/puppeteer-no-sandbox.json \
 && chmod 0644 /etc/puppeteer-no-sandbox.json

ARG MERMAID_CLI_VERSION
RUN npm install -g --no-audit --no-fund --loglevel=error \
      "@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}" \
 && command -v mmdc \
 # Render for real, with the SAME arguments mermaid-render uses. Checking that
 # the binary unpacked proves nothing: this failed at render time, on the
 # sandbox, with the package installed perfectly.
 #
 # A writable HOME is not optional — chromium spawns chrome_crashpad_handler
 # before anything else and it derives its database from the home directory.
 # See the long note in mermaid-render, which learned this the expensive way.
 && mkdir -p /tmp/smoke-home \
 && printf 'graph TD\n  A[build] --> B[check]\n' > /tmp/smoke.mmd \
 && { HOME=/tmp/smoke-home XDG_CONFIG_HOME=/tmp/smoke-home/.config \
      XDG_CACHE_HOME=/tmp/smoke-home/.cache \
      mmdc --input /tmp/smoke.mmd --output /tmp/smoke.svg \
        --puppeteerConfigFile /etc/puppeteer-no-sandbox.json \
      || { echo "=== mmdc smoke render failed ==="; exit 1; }; } \
 && test -s /tmp/smoke.svg \
 && head -c 60 /tmp/smoke.svg && echo \
 && rm -rf /tmp/smoke.mmd /tmp/smoke.svg /tmp/smoke-home

# ---------------------------------------------------------------------------
# The tools documentation, BAKED IN.
#
# These describe every capability wired into the bot, and the agent is told to
# treat anything absent from them as not wired. They used to exist only as a
# ConfigMap assembled by an ArgoCD pre-sync hook — so if that ConfigMap failed
# to apply, the docs were not stale, they were GONE, and the agent would
# conclude it had no tools at all.
#
# Now they ship both ways: the parts and an assembled TOOLS.md are in the
# image, and the ConfigMap overlays the parts at runtime. A missing mount
# degrades to an older document rather than to nothing.
#
# Assembled here from ORDER rather than from a glob: the sequence is part of
# the document (the header file first, capabilities after) and a shell glob
# would silently reorder it the day someone adds TOOLS-a-something.md.
COPY --chmod=0644 tools-md/ /usr/local/share/claw-tools/
RUN set -eu; \
    cd /usr/local/share/claw-tools; \
    grep -v '^#' ORDER | grep -v '^[[:space:]]*$' > /tmp/order.txt; \
    while IFS= read -r f; do \
      [ -f "$f" ] || { echo "ORDER names a file that does not exist: $f"; exit 1; }; \
    done < /tmp/order.txt; \
    # shellcheck disable=SC2046
    cat $(tr '\n' ' ' < /tmp/order.txt) > /usr/local/share/claw-tools/TOOLS.assembled.md; \
    rm -f /tmp/order.txt; \
    echo "assembled TOOLS.md: $(wc -c < /usr/local/share/claw-tools/TOOLS.assembled.md) bytes"

USER root
COPY --chmod=0755 heartbeat-issue-tick.py /usr/local/bin/heartbeat-issue-tick
COPY --chmod=0755 cron-issue-spawn.sh    /usr/local/bin/cron-issue-spawn
COPY --chmod=0755 fixer-runner.sh        /usr/local/bin/fixer-runner
# Tester subsystem — parallel to the issue-fixer. Same pattern: a
# CronJob runs cron-tester-spawn, which calls tester-tick to list
# repos with new default-branch HEAD, then kubectl-exec's a
# tester-runner subprocess inside the openclaw pod. See
# k8s/051-tester.yaml.
COPY --chmod=0755 tester-tick.py                /usr/local/bin/tester-tick
COPY --chmod=0755 cron-tester-spawn.sh          /usr/local/bin/cron-tester-spawn
COPY --chmod=0755 tester-runner.sh              /usr/local/bin/tester-runner
COPY --chmod=0755 tester-upload-screenshots.py  /usr/local/bin/tester-upload-screenshots

# Reviewer subsystem — the third planner/runner pair, same shape again. See
# k8s/052-reviewer.yaml.
COPY --chmod=0755 reviewer-tick.py         /usr/local/bin/reviewer-tick
COPY --chmod=0755 cron-reviewer-spawn.sh   /usr/local/bin/cron-reviewer-spawn
COPY --chmod=0755 reviewer-runner.sh       /usr/local/bin/reviewer-runner

# Estimation — sizes a story on the cheap planning model one tick before the
# solver picks it up, so the solver can route by size.
COPY --chmod=0755 estimate-runner.sh       /usr/local/bin/estimate-runner
COPY --chmod=0755 forge-cli                /usr/local/bin/forge-cli

# Shared Python modules. Installed FLAT and extension-less alongside the
# scripts because that is how they are imported at runtime — by bare name,
# from /usr/local/bin, which is on sys.path for a script living there. The
# test suite imports them the same way so it tests this arrangement and not
# another one.
#
# A module that is merged but not listed here is deployed and then absent:
# the code that imports it raises ImportError on every tick, in a CronJob log
# nobody reads. `test_shipped_modules` exists to fail the build instead.
# The code-host seam. Every planner and every runner asks its questions
# through this, so a build without it is a build where nothing can talk to
# anything.
COPY --chmod=0644 forge.py              /usr/local/bin/forge.py
COPY --chmod=0644 forge_github.py       /usr/local/bin/forge_github.py
COPY --chmod=0644 forge_gitlab.py       /usr/local/bin/forge_gitlab.py
COPY --chmod=0644 forge_gitea.py        /usr/local/bin/forge_gitea.py
COPY --chmod=0644 forge_azdo.py         /usr/local/bin/forge_azdo.py
COPY --chmod=0644 issue_status.py       /usr/local/bin/issue_status.py
COPY --chmod=0644 issue_priority.py     /usr/local/bin/issue_priority.py
COPY --chmod=0644 story_points.py       /usr/local/bin/story_points.py
COPY --chmod=0644 story_estimate.py     /usr/local/bin/story_estimate.py
COPY --chmod=0644 approval_release.py   /usr/local/bin/approval_release.py
COPY --chmod=0644 lexical_guard.py      /usr/local/bin/lexical_guard.py
COPY --chmod=0644 review_subject.py     /usr/local/bin/review_subject.py
COPY --chmod=0644 security_reports.py   /usr/local/bin/security_reports.py
COPY --chmod=0644 queue_state.py        /usr/local/bin/queue_state.py
COPY --chmod=0644 delivering_pr.py      /usr/local/bin/delivering_pr.py
COPY --chmod=0644 project_allowlist.py  /usr/local/bin/project_allowlist.py
COPY --chmod=0644 planning_docs.py      /usr/local/bin/planning_docs.py
COPY --chmod=0644 planning_store.py     /usr/local/bin/planning_store.py
COPY --chmod=0644 sprint_current.py     /usr/local/bin/sprint_current.py
COPY --chmod=0644 sprint_schedule.py    /usr/local/bin/sprint_schedule.py

# Runtime controls. The `.sh` halves are SOURCED by the runners and must sit
# next to the CLI that shares their name — agent-models sources
# agent-models.sh from its own directory, exactly as the tests arrange it.
COPY --chmod=0755 agent-limits          /usr/local/bin/agent-limits
COPY --chmod=0644 agent-limits.sh       /usr/local/bin/agent-limits.sh
COPY --chmod=0755 agent-models          /usr/local/bin/agent-models
COPY --chmod=0644 agent-models.sh       /usr/local/bin/agent-models.sh
COPY --chmod=0755 agent-thinking        /usr/local/bin/agent-thinking
COPY --chmod=0644 agent-thinking.sh     /usr/local/bin/agent-thinking.sh
COPY --chmod=0644 agent-slot.sh         /usr/local/bin/agent-slot.sh
COPY --chmod=0755 security-level        /usr/local/bin/security-level
COPY --chmod=0755 llm-quota             /usr/local/bin/llm-quota

# Permission boundary. Nothing the bot does autonomously touches a repository
# outside this list.
COPY --chmod=0755 project-allow          /usr/local/bin/project-allow
COPY --chmod=0644 project-kind.sh        /usr/local/bin/project-kind.sh
COPY --chmod=0644 project-instructions.sh /usr/local/bin/project-instructions.sh

# Planning / reporting surface.
COPY --chmod=0755 planning              /usr/local/bin/planning
COPY --chmod=0755 planning-record       /usr/local/bin/planning-record
COPY --chmod=0755 planning-story        /usr/local/bin/planning-story
COPY --chmod=0755 record-deliveries     /usr/local/bin/record-deliveries

# Notification and rendering helpers.
COPY --chmod=0755 telegram-notify       /usr/local/bin/telegram-notify
COPY --chmod=0755 mermaid-render        /usr/local/bin/mermaid-render
COPY --chmod=0644 mermaid-theme.json    /usr/local/share/mermaid-theme.json

# The skills lockdown, run as root by the fix-perms init container.
COPY --chmod=0755 lock-skill-dirs.sh    /usr/local/bin/lock-skill-dirs

# Timezone: Europe/Zurich. Without this, libc time + Node Date default
# to UTC, so chat timestamps, heartbeat log lines, and the runtime
# context the LLM sees are off by 1-2h vs the user's locale. The
# `Europe/Zurich` IANA name handles CET ↔ CEST automatically.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/Europe/Zurich /etc/localtime \
 && echo "Europe/Zurich" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Zurich

USER node