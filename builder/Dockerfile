# Custom openclaw image for claw-code coding agent.
# Based on the k8s-openclaw builder/Dockerfile but WITHOUT:
#   - JHora / PyJHora / pyswisseph (astrology)
#   - Gmail MCP
#   - Ollama / Whisper / Kokoro TTS / OpenWebUI
#   - Second instance (olga)
#   - gdrive / knowledge corpus
#
# Differences from k8s-openclaw builder:
#   - No Python astrology packages
#   - No ollama / whisper / kokoro
#   - No open-webui
#   - No gdrive / knowledge mounts
#   - Only Mistral Large default (optional MiniMax via secrets)
#
# Args come from .github/workflows/build.yml build step.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# Strip openclaw skills that expand the agent's capabilities at runtime:
#   - mcporter      — dynamic MCP-server gateway (loads tools over HTTP/stdio,
#                     bypasses our static MCP allowlist)
#   - skill-creator — guides the agent to write new skill files
# Skills live at /app/skills/ in the upstream openclaw image.
USER root
RUN rm -rf /app/skills/mcporter /app/skills/skill-creator

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
 && apt-get purge -y --auto-remove apt-transport-https lsb-release gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/bin/az --version | head -3

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

# Chromium
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium fonts-liberation \
      libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
      libgbm1 libasound2 libxshmfence1 \
 && rm -rf /var/lib/apt/lists/* \
 && chromium --version

# kubectl + podman
USER root
RUN ARCH=$(dpkg --print-architecture) \
 && KUBECTL_VERSION="$(curl -sL https://dl.k8s.io/release/stable.txt)" \
 && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && /usr/local/bin/kubectl version --client=true --output=yaml | head -3 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      podman buildah fuse-overlayfs uidmap slirp4netns \
 && rm -rf /var/lib/apt/lists/* \
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

# Issue-watcher scripts invoked by the issue-watcher CronJob.
#   heartbeat-issue-tick — read-only planner: emits JSON of repos +
#                          assigned issues + active Jobs per repo.
#   cron-issue-spawn     — runs the planner and `kubectl apply`s a
#                          Job per toSpawn entry (one Job = one
#                          `openclaw agent --local` invocation that
#                          fixes the issue end-to-end).
USER root
COPY --chmod=0755 heartbeat-issue-tick.py /usr/local/bin/heartbeat-issue-tick
COPY --chmod=0755 cron-issue-spawn.sh    /usr/local/bin/cron-issue-spawn

USER node