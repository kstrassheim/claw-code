# The openclaw gateway's bearer token.
#
# openclaw refuses to start without one:
#
#     Refusing to bind gateway to lan without auth.  (exit 78)
#
# It treats gateway.auth as runtime state — generated on first run, then
# carried forward from the PVC by render-config. Neither happens on a fresh
# PVC, so a newly provisioned cluster could never start a pod. Generating it
# here makes it infrastructure instead: created once, held in state, and
# identical on every deploy and every replacement PVC.
#
# The state is encrypted (aes_gcm, key from Key Vault), so the token is at
# rest under the same protection as everything else in it.
resource "random_password" "gateway_token" {
  length = 64
  # Hex-ish alphabet only. The value travels through a shell as a
  # --from-literal, and a token containing quotes or backslashes is a quoting
  # bug waiting for the worst moment.
  special = false
  upper   = false
}

output "gateway_token" {
  value     = random_password.gateway_token.result
  sensitive = true
}
