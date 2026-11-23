#!/usr/bin/env bash
# Redrive one Support Case Insights stage dead-letter queue back to the stage queue that feeds it.
# Run with --help for the options, the safety checks and the permissions it needs.

set -euo pipefail

readonly EXIT_OK=0
readonly EXIT_FAILED=1
readonly EXIT_USAGE=2
readonly EXIT_CANCELLED=3

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR

usage() {
  cat <<'EOF'
Usage: scripts/redrive-dlq.sh --env ENV --queue QUEUE [--dry-run | --confirm TOKEN]
                              [--rate N] [--profile NAME] [--config-dir DIR]

Moves the messages of one stage dead-letter queue of a Support Case Insights deployment back to
the stage queue that feeds it, with an SQS message move task (StartMessageMoveTask).

Options:
  --env ENV         Environment whose config/<ENV>.json names the deployment (for example dev).
  --queue QUEUE     Stage whose dead-letter queue is redriven:
                      active-accounts  CaseInsights-ActiveAccounts-<uniqueId>-DLQ
                                       -> CaseInsights-ActiveAccounts-<uniqueId>
                      annotation       CaseInsights-CaseAnnotation-<uniqueId>-DLQ
                                       -> CaseInsights-CaseAnnotation-<uniqueId>
                      summary          CaseInsights-CaseSummary-<uniqueId>-DLQ
                                       -> CaseInsights-CaseSummary-<uniqueId>
  --dry-run         Check the target and show the scope and redrive token; change nothing.
  --confirm TOKEN   Redrive token from the preview, to start without an interactive prompt.
  --rate N          Messages moved per second (1-500; default: the rate SQS chooses).
  --profile NAME    AWS named profile (default: the standard credential chain).
  --config-dir DIR  Directory holding <ENV>.json (default: the repository's config directory).
  -h, --help        Show this help.

Safety:
  The credentials must belong to the deployment's managementAccountId. Both queues are looked up
  by their deployment names in the configuration's Region, their ARNs must be in that account and
  Region, and the dead-letter queue must be the stage queue's RedrivePolicy target. The preview
  shows both queues and the approximate number of messages. Nothing moves until you type the
  redrive token shown in the preview, or pass it with --confirm; the token covers exactly this
  account, Region and queue pair. A redrive is refused while a move task is already active.

What a redrive does and does not do:
  Every message is moved unchanged, so each reference keeps its account, case, run and redrive
  identity. Committed cases are still never analysed again: a moved reference for a case that has
  a processed record is answered as already processed. A started move task only means SQS is
  moving messages back; it does not mean they were processed. Watch the dead-letter queue, the
  stage function's logs and the alarms. The workflow-failures and account-discovery-failures
  terminal queues have no source queue and are not redriven here; see docs/runbook.md. To analyse
  committed cases again, use scripts/reprocess-cases.py.

Required permissions:
  sts:GetCallerIdentity; sqs:GetQueueUrl and sqs:GetQueueAttributes on both queues;
  sqs:ListMessageMoveTasks, sqs:StartMessageMoveTask, sqs:ReceiveMessage, sqs:DeleteMessage and
  sqs:GetQueueAttributes on the dead-letter queue; sqs:SendMessage on the stage queue. The queues
  use SQS-managed encryption, so no KMS permissions are needed. Cancelling a task needs
  sqs:CancelMessageMoveTask on the dead-letter queue.

Uncertain starts:
  Only a refusal for lack of permission proves that no task started. Any other failed, timed-out
  or unreadable StartMessageMoveTask response is reported as UNKNOWN, because SQS may have
  accepted the request (the AWS CLI also retries some failures itself). Inspect the dead-letter
  queue and its move tasks with the printed commands before retrying; they use the same Region
  and --profile as the redrive.

Exit status: 0 started or nothing to redrive, 1 an AWS request failed, the task failed or its
start is unknown, 2 invalid options or target, 3 cancelled or not confirmed.
EOF
}

die_usage() {
  printf 'error: %s. Nothing was changed.\n' "$1" >&2
  exit "$EXIT_USAGE"
}

ENV_NAME=""
QUEUE=""
DRY_RUN=false
CONFIRM=""
CONFIRM_SET=false
RATE=""
PROFILE=""
CONFIG_DIR="$SCRIPT_DIR/../config"

option_value() {
  # option_value <option> <remaining argument count> <value>
  if [[ $2 -lt 2 || -z $3 || $3 == --* ]]; then
    die_usage "$1 needs a value"
  fi
}

parse_args() {
  if [[ $# -eq 0 ]]; then
    usage >&2
    exit "$EXIT_USAGE"
  fi
  while [[ $# -gt 0 ]]; do
    case $1 in
      -h | --help)
        usage
        exit "$EXIT_OK"
        ;;
      --env)
        option_value "$1" "$#" "${2-}"
        ENV_NAME=$2
        shift 2
        ;;
      --queue)
        option_value "$1" "$#" "${2-}"
        QUEUE=$2
        shift 2
        ;;
      --confirm)
        option_value "$1" "$#" "${2-}"
        CONFIRM=$2
        CONFIRM_SET=true
        shift 2
        ;;
      --rate)
        option_value "$1" "$#" "${2-}"
        RATE=$2
        shift 2
        ;;
      --profile)
        option_value "$1" "$#" "${2-}"
        PROFILE=$2
        shift 2
        ;;
      --config-dir)
        option_value "$1" "$#" "${2-}"
        CONFIG_DIR=$2
        shift 2
        ;;
      --dry-run)
        DRY_RUN=true
        shift
        ;;
      *)
        die_usage "unknown argument '$1' (see --help)"
        ;;
    esac
  done
  [[ -n $ENV_NAME ]] || die_usage "--env is required"
  [[ -n $QUEUE ]] || die_usage "--queue is required"
  [[ $ENV_NAME =~ ^[a-z][a-z0-9-]{0,31}$ ]] || die_usage "--env must be a configuration name such as dev or prod"
  if [[ -n $RATE ]] && { ! [[ $RATE =~ ^[1-9][0-9]{0,2}$ ]] || ((RATE > 500)); }; then
    die_usage "--rate must be an integer between 1 and 500"
  fi
  if $DRY_RUN && $CONFIRM_SET; then
    die_usage "--dry-run and --confirm cannot be combined"
  fi
  case $QUEUE in
    active-accounts) STAGE_PREFIX="CaseInsights-ActiveAccounts" ;;
    annotation) STAGE_PREFIX="CaseInsights-CaseAnnotation" ;;
    summary) STAGE_PREFIX="CaseInsights-CaseSummary" ;;
    workflow-failures)
      die_usage "the workflow-failures queue is a terminal queue with no source queue. To retry a recorded case, send its case reference fields to the summary queue with a new redriveId (see docs/runbook.md)"
      ;;
    account-discovery-failures | discovery-failures)
      die_usage "the account-discovery-failures queue is a terminal queue with no source queue. Fix the cause and invoke account lookup to start a fresh run (see docs/runbook.md)"
      ;;
    *) die_usage "--queue must be active-accounts, annotation or summary" ;;
  esac
}

require_tools() {
  local tool
  for tool in aws python3; do
    command -v "$tool" >/dev/null 2>&1 || die_usage "$tool is required but was not found on PATH"
  done
}

load_config() {
  local path="$CONFIG_DIR/$ENV_NAME.json" fields
  fields=$(
    python3 - "$path" "$ENV_NAME" 2>"$PY_ERR" <<'PY'
import json, re, sys

path, env = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
except FileNotFoundError:
    sys.exit(f"configuration {path} does not exist")
except (OSError, ValueError):
    sys.exit(f"configuration {path} is not readable JSON")
if not isinstance(document, dict):
    sys.exit(f"configuration {path} is not a JSON object")
unique_id = document.get("uniqueId")
region = document.get("region", "us-east-1")
management = document.get("managementAccountId")
if document.get("environment") != env:
    sys.exit(f"configuration {path} is not for environment {env}")
if not isinstance(unique_id, str) or not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", unique_id):
    sys.exit(f"configuration {path} has no valid uniqueId")
if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]", region):
    sys.exit(f"configuration {path} has no valid region")
if not isinstance(management, str) or not re.fullmatch(r"\d{12}", management):
    sys.exit(f"configuration {path} has no valid managementAccountId")
print(unique_id, region, management)
PY
  ) || die_usage "$(cat "$PY_ERR" 2>/dev/null || true)"
  read -r UNIQUE_ID REGION MANAGEMENT_ACCOUNT <<<"$fields"
  SOURCE_NAME="$STAGE_PREFIX-$UNIQUE_ID"
  DLQ_NAME="$SOURCE_NAME-DLQ"
}

AWS_ERR=""
PY_ERR=""
DLQ_URL=""

aws_json() {
  local -a command=(aws --region "$REGION" --output json)
  if [[ -n $PROFILE ]]; then
    command+=(--profile "$PROFILE")
  fi
  "${command[@]}" "$@" 2>"$AWS_ERR"
}

aws_error_code() {
  local code
  code=$(sed -n 's/.*An error occurred (\([^)]*\)).*/\1/p' "$AWS_ERR" | head -n 1)
  if [[ -z $code ]]; then
    if grep -qiE 'credentials|could not be found|token.*expired' "$AWS_ERR"; then
      code="credentials"
    else
      code="AWS CLI error"
    fi
  fi
  printf '%s' "$code"
}

aws_failed() {
  # aws_failed <what failed>: report the AWS error code only (messages can name other resources).
  local code
  code=$(aws_error_code)
  if [[ $code == credentials ]]; then
    printf 'error: AWS credentials are not usable. Nothing was changed.\n' >&2
    exit "$EXIT_USAGE"
  fi
  printf 'error: %s (%s). Nothing was changed.\n' "$1" "$code" >&2
  exit "$EXIT_FAILED"
}

json_get() {
  # json_get <field>...: print a (nested) field of the JSON document on stdin, empty when absent.
  python3 -c '
import json, sys
value = json.load(sys.stdin)
for name in sys.argv[1:]:
    value = value.get(name) if isinstance(value, dict) else None
if value is None:
    print("")
elif isinstance(value, str):
    print(value)
else:
    print(json.dumps(value))
' "$@"
}

resolve_identity() {
  local identity arn
  identity=$(aws_json sts get-caller-identity) || aws_failed "the caller identity could not be read"
  ACCOUNT=$(json_get Account <<<"$identity")
  arn=$(json_get Arn <<<"$identity")
  PARTITION=$(cut -d: -f2 <<<"$arn")
  if [[ $ACCOUNT != "$MANAGEMENT_ACCOUNT" ]]; then
    die_usage "the credentials are for account $ACCOUNT, but deployment $UNIQUE_ID runs in management account $MANAGEMENT_ACCOUNT"
  fi
}

queue_url() {
  # queue_url <name>: the URL of the deployment queue <name> in the deployment account.
  local response code
  if ! response=$(aws_json sqs get-queue-url --queue-name "$1" --queue-owner-aws-account-id "$ACCOUNT"); then
    code=$(aws_error_code)
    case $code in
      AWS.SimpleQueueService.NonExistentQueue | QueueDoesNotExist)
        die_usage "queue $1 does not exist in account $ACCOUNT, $REGION"
        ;;
    esac
    aws_failed "queue $1 could not be looked up"
  fi
  json_get QueueUrl <<<"$response"
}

queue_attributes() {
  # queue_attributes <url> <attribute>...
  local url=$1
  shift
  aws_json sqs get-queue-attributes --queue-url "$url" --attribute-names "$@" ||
    aws_failed "the attributes of $url could not be read"
}

resolve_queues() {
  local source_url dlq_url source dlq policy target
  # Each lookup runs in a subshell; its exit status carries any refusal out of this function.
  source_url=$(queue_url "$SOURCE_NAME") || exit $?
  dlq_url=$(queue_url "$DLQ_NAME") || exit $?
  DLQ_URL=$dlq_url
  source=$(queue_attributes "$source_url" QueueArn RedrivePolicy) || exit $?
  dlq=$(queue_attributes "$dlq_url" QueueArn ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible) || exit $?
  SOURCE_ARN=$(json_get Attributes QueueArn <<<"$source")
  DLQ_ARN=$(json_get Attributes QueueArn <<<"$dlq")
  local prefix="arn:$PARTITION:sqs:$REGION:$ACCOUNT"
  [[ $SOURCE_ARN == "$prefix:$SOURCE_NAME" ]] || die_usage "queue $SOURCE_NAME is not in account $ACCOUNT, $REGION"
  [[ $DLQ_ARN == "$prefix:$DLQ_NAME" ]] || die_usage "queue $DLQ_NAME is not in account $ACCOUNT, $REGION"
  policy=$(json_get Attributes RedrivePolicy <<<"$source")
  target=""
  if [[ -n $policy ]]; then
    target=$(json_get deadLetterTargetArn <<<"$policy" 2>/dev/null || true)
  fi
  [[ $target == "$DLQ_ARN" ]] || die_usage "$DLQ_NAME is not the dead-letter queue of $SOURCE_NAME (RedrivePolicy target: ${target:-none})"
  VISIBLE=$(json_get Attributes ApproximateNumberOfMessages <<<"$dlq")
  IN_FLIGHT=$(json_get Attributes ApproximateNumberOfMessagesNotVisible <<<"$dlq")
  VISIBLE=${VISIBLE:-0}
  IN_FLIGHT=${IN_FLIGHT:-0}
}

active_task() {
  # Prints "<status> <handle>" of an active move task from the dead-letter queue, if any.
  local tasks
  tasks=$(aws_json sqs list-message-move-tasks --source-arn "$DLQ_ARN" --max-results 10) ||
    aws_failed "the move tasks of $DLQ_NAME could not be listed"
  python3 -c '
import json, sys
for task in json.load(sys.stdin).get("Results", []):
    if task.get("Status") in ("RUNNING", "CANCELLING"):
        print(task.get("Status"), task.get("TaskHandle", "unknown"))
        break
' <<<"$tasks"
}

redrive_token() {
  python3 -c '
import hashlib, sys
print("rd-" + hashlib.sha256("\n".join(sys.argv[1:]).encode()).hexdigest()[:12])
' "$ACCOUNT" "$REGION" "$DLQ_ARN" "$SOURCE_ARN"
}

print_preview() {
  printf 'Redrive preview for deployment %s (env %s, account %s, %s)\n' \
    "$UNIQUE_ID" "$ENV_NAME" "$ACCOUNT" "$REGION"
  printf 'From dead-letter queue: %s (%s)\n' "$DLQ_NAME" "$DLQ_ARN"
  printf 'To stage queue:         %s (%s)\n' "$SOURCE_NAME" "$SOURCE_ARN"
  printf 'Messages in the dead-letter queue (approximate): %s visible, %s in flight\n' \
    "$VISIBLE" "$IN_FLIGHT"
  if [[ -n $RATE ]]; then
    printf 'Move rate: %s messages per second\n' "$RATE"
  else
    printf 'Move rate: chosen by SQS\n'
  fi
}

confirmed() {
  local token=$1 reply=""
  if $CONFIRM_SET; then
    if [[ $CONFIRM != "$token" ]]; then
      printf 'The confirmation token does not match this redrive (another deployment, Region or queue). Nothing was changed.\n' >&2
      return 1
    fi
    return 0
  fi
  if [[ ! -t 0 ]]; then
    printf 'Confirmation required: rerun with --confirm %s to start the redrive. Nothing was changed.\n' "$token" >&2
    return 1
  fi
  read -r -p "Type the redrive token $token to move the messages of $DLQ_NAME to $SOURCE_NAME: " reply || reply=""
  if [[ $reply != "$token" ]]; then
    printf 'Cancelled. Nothing was changed.\n' >&2
    return 1
  fi
  return 0
}

cli_options() {
  # The Region and profile of this run, shell-quoted, for the AWS CLI commands the script prints.
  printf -- '--region %q' "$REGION"
  if [[ -n $PROFILE ]]; then
    printf -- ' --profile %q' "$PROFILE"
  fi
}

print_progress_command() {
  printf 'Progress: aws sqs list-message-move-tasks --source-arn %q %s\n' "$DLQ_ARN" "$(cli_options)"
}

print_inspect_commands() {
  print_progress_command
  printf 'DLQ depth: aws sqs get-queue-attributes --queue-url %q --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible %s\n' \
    "$DLQ_URL" "$(cli_options)"
}

start_unknown() {
  # start_unknown <detail>: the start request may have been accepted without a confirmation.
  printf 'error: the move task start is UNKNOWN (%s). SQS may have accepted the request without confirming it, so messages may already be moving from %s to %s.\n' \
    "$1" "$DLQ_NAME" "$SOURCE_NAME" >&2
  printf 'Do not assume nothing moved: inspect the dead-letter queue and its move tasks before retrying, with the commands below. Start another redrive only when no task is RUNNING and the messages are still in %s.\n' \
    "$DLQ_NAME" >&2
  print_inspect_commands
  exit "$EXIT_FAILED"
}

start_task() {
  local -a rate=()
  local response handle status tasks code
  if [[ -n $RATE ]]; then
    rate=(--max-number-of-messages-per-second "$RATE")
  fi
  if ! response=$(aws_json sqs start-message-move-task --source-arn "$DLQ_ARN" \
    --destination-arn "$SOURCE_ARN" ${rate[@]+"${rate[@]}"}); then
    code=$(aws_error_code)
    case $code in
      AccessDenied | AccessDeniedException)
        # Every attempt of the request, including the CLI's own retries, carries the same
        # authorisation, so a refusal for permission means none of them was accepted.
        printf 'error: the move task was not started (%s). No messages were moved.\n' "$code" >&2
        exit "$EXIT_FAILED"
        ;;
    esac
    start_unknown "$code"
  fi
  handle=$(json_get TaskHandle <<<"$response" 2>/dev/null) || handle=""
  [[ -n $handle ]] || start_unknown "no task handle in the response"
  printf 'Started move task %s.\n' "$handle"
  if ! tasks=$(aws_json sqs list-message-move-tasks --source-arn "$DLQ_ARN" --max-results 1); then
    printf 'warning: the move task was started, but its status could not be read (%s). Do not start another; check it with the command below.\n' "$(aws_error_code)" >&2
    status="unknown"
  else
    status=$(
      python3 -c '
import json, sys
results = json.load(sys.stdin).get("Results", [])
task = results[0] if results else {}
status = task.get("Status", "unknown")
moved = task.get("ApproximateNumberOfMessagesMoved")
total = task.get("ApproximateNumberOfMessagesToMove")
reason = task.get("FailureReason")
detail = status
if moved is not None and total is not None:
    detail += f" ({moved} of about {total} messages moved)"
if reason:
    detail += f": {reason}"
print(detail)
' <<<"$tasks"
    )
    printf 'Task status: %s\n' "$status"
  fi
  printf 'Moving a message back to %s does not mean it was processed. Committed cases are answered as already processed; other messages are retried by the stage and return to %s if they fail again. Check the dead-letter queue, the stage logs and the alarms for the actual results.\n' \
    "$SOURCE_NAME" "$DLQ_NAME"
  print_progress_command
  printf 'Cancel:   aws sqs cancel-message-move-task --task-handle %q %s\n' "$handle" "$(cli_options)"
  case $status in
    FAILED* | CANCELLED* | unknown) exit "$EXIT_FAILED" ;;
  esac
  exit "$EXIT_OK"
}

main() {
  parse_args "$@"
  require_tools
  AWS_ERR=$(mktemp)
  PY_ERR=$(mktemp)
  trap 'rm -f "$AWS_ERR" "$PY_ERR"' EXIT
  load_config
  resolve_identity
  resolve_queues
  print_preview
  local active token
  active=$(active_task) || exit $?
  if [[ -n $active ]]; then
    die_usage "a move task from $DLQ_NAME is already active (${active}); wait for it to finish or cancel it first"
  fi
  if ((VISIBLE + IN_FLIGHT == 0)); then
    printf 'Nothing to redrive. No changes were made.\n'
    exit "$EXIT_OK"
  fi
  token=$(redrive_token)
  printf 'Redrive token: %s\n' "$token"
  if $DRY_RUN; then
    printf 'Dry run: no changes were made. To start the redrive, rerun with --confirm %s.\n' "$token"
    exit "$EXIT_OK"
  fi
  confirmed "$token" || exit "$EXIT_CANCELLED"
  start_task
}

main "$@"
