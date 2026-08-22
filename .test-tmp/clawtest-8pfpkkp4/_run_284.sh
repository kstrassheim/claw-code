export HOME="$PWD"
export PATH="$PWD/bin:$PATH"
export FAKE_CURL_DIR="$PWD/fixtures"
export FAKE_CURL_LOG="$PWD/curl.log"
export FAKE_FORGE_DIR="$PWD/fixtures"
export FAKE_FORGE_LOG="$PWD/forge.log"
export FAKE_OPENCLAW_CALLS="$PWD/openclaw-calls.jsonl"
export PYTHONPATH="$PWD/bin"
set -u
AGENT_THINKING=''
BOT_LOGIN='bot'
GITHUB_TOKEN='token'
ISSUE_BODY=''
ISSUE_CLOSED_AS=''
ISSUE_LABELS_JSON='[]'
ISSUE_NUM='7'
ISSUE_STATE='open'
ISSUE_TITLE='a task'
REPO='o/r'
FORGE=(forge-cli --repo "$REPO")
CR_NOUN="pull request"
AWAITING_REVIEW_MARKER="$PWD/awaiting-review"
AWAITING_HUMAN_MARKER="$PWD/awaiting-human"
REVIEW_WAIT_TTL="${REVIEW_WAIT_TTL:-7200}"
APPROVAL_ASKED_FILE="$PWD/approval-asked"
MERGE_REFUSED_FILE="$PWD/merge-refused"
APPROVAL_GRANTED_FILE="$PWD/approval-granted"
SYNC_FP_FILE="$PWD/sync-fp"
SYNC_RETRY_FILE="$PWD/sync-retries"
SYNC_RETRY_CAP="${SYNC_RETRY_CAP:-4}"
REVIEW_FP_FILE="$PWD/review-fp"
REVIEW_RETRY_FILE="$PWD/review-retries"
HUMAN_REVIEW_FP_FILE="$PWD/human-review-fp"
HUMAN_REVIEW_RETRY_FILE="$PWD/human-review-retries"
HUMAN_REVIEW_ESCALATED_FILE="$PWD/human-review-escalated"
REVIEW_ESCALATED_FILE="$PWD/review-escalated"
REVIEW_RETRY_CAP="${REVIEW_RETRY_CAP:-4}"
CI_RETRY_FILE="$PWD/ci-red-retries"
CI_RED_ESCALATED_FILE="$PWD/ci-red-escalated"
CI_RED_RETRY_CAP="${CI_RED_RETRY_CAP:-4}"
DEFAULT_BRANCH=main
repo_owner_login() { echo "${REPO%%/*}"; }
source "$PWD/api.sh"
source "$PWD/status.sh"
source "$PWD/facts.sh"
source "$PWD/review.sh"
source "$PWD/approval.sh"
source "$PWD/merge.sh"
source "$PWD/escalate.sh"
RUN_OUTCOME=""
if maybe_merge_green_pr 7; then echo MERGED; else echo NOT_MERGED; fi

