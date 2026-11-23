# Prompts and classification

This document describes how the analysis stages use Amazon Bedrock: the prompt templates and their placeholders, how the model is called, how summaries and classifications are validated and corrected, the RCA and lifecycle taxonomies, how prompt and model provenance is recorded, and how to change and test prompts safely. It covers the case analysis pipeline only; the two MCP tools that call Bedrock (`analyze_with_bedrock` and `analyze_case_summaries`) are described in [mcp-server-deployment.md](mcp-server-deployment.md#tools).

## How a case is analysed

The `CaseAnalysis-<uniqueId>` state machine runs three Bedrock stages in order for each case, then commits the processed record. Each stage sends one prompt and allows at most one corrective follow-up in the same conversation.

| Stage | Template | Input | Output | After one failed correction |
| --- | --- | --- | --- | --- |
| Summary | `summary-template.txt` | The rendered case conversation | `Case_Summary`, 600 to 650 characters | The stage fails (`SummaryValidationError`) and the workflow records a failure; nothing is padded or truncated |
| RCA | `rca-template.txt` | The validated summary | `RCA_Category`, `RCA_Reason` | `Unclassified` with an explanatory reason |
| Lifecycle | `lifecycle-template.txt` | The validated summary | `Lifecycle_Category`, `Lifecycle_Reason` | `Unclassified` with an explanatory reason |

The case conversation is rendered by `src/lambda/common/conversation.py`. Messages are labelled `AWS Support` or `Customer`; email addresses of the authors are not included. A rendered conversation longer than 375,000 characters (the 400,000-character prompt limit minus a 25,000-character allowance for the template) is not truncated: case annotation stores it in full, sends a `ConversationTooLarge` record to the workflow failure queue instead of queueing a summary, writes a `terminal-failure.json` marker with a digest of the conversation and emits the `OversizedConversations` metric. The case is reported again only when its conversation changes; it is never retried automatically while unchanged. See [QueueMessages-WorkflowFailures](runbook.md#queuemessages-workflowfailures).

## Templates

The templates live in `src/templates/` and are deployed in the templates layer `case-insights-templates-<uniqueId>`, which Lambda mounts at `/opt/templates`. `src/lambda/common/templates.py` loads them from the directory passed by the caller, then `CASE_INSIGHTS_TEMPLATES_DIR`, then `/opt/templates`.

| File | Placeholders | Used for |
| --- | --- | --- |
| `summary-template.txt` | `{case_annotation}` | First summary prompt |
| `summary-correction-template.txt` | `{issues}` | Corrective follow-up listing each failed summary rule as a bullet |
| `rca-template.txt` | `{case_summary}`, `{categories}` | First RCA prompt |
| `lifecycle-template.txt` | `{case_summary}`, `{categories}` | First lifecycle prompt |
| `classification-correction-template.txt` | `{problem}`, `{categories}`, `{category_key}`, `{reason_key}` | Corrective follow-up for either classifier. `{problem}` is a fixed hint for the failed rule and never contains model output |

Rules enforced by `common/templates.py`:

- A placeholder is `{` followed by lower-case letters or underscores and `}`. Each file must use exactly the placeholders listed above; a missing, extra or misspelt placeholder raises `TemplateError` and the stage fails. Literal JSON such as `{"RCA_Category": "..."}` is not a placeholder, because it starts with a quotation mark.
- Rendering is a single pass, so placeholder-like text inside case content, such as `{categories}` in a customer message, is inserted as written and never expanded.
- Category lists are rendered from `src/lambda/common/taxonomy.py` as `- Label: definition` lines. Template files never repeat category names or definitions.

The prompts treat case content as untrusted data. The summary prompt places the conversation between `<case_annotation>` tags and the classification prompts place the summary between `<case_summary>` tags, each with the instruction to use the content only as material and to ignore any instructions it contains. The summary prompt also tells the model not to include names, email addresses, phone numbers, account credentials or other personal data. These instructions reduce, but cannot eliminate, the effect of adversarial case text; the validation described below limits what a reply can contain.

## Model invocation

`src/lambda/common/bedrock.py` calls `InvokeModel` with the Anthropic Messages format:

| Setting | Value |
| --- | --- |
| Model | `bedrockModelId`: `global.anthropic.claude-haiku-4-5-20251001-v1:0` (default) or `global.anthropic.claude-sonnet-4-5-20250929-v1:0`. No other model and no fallback model is used. |
| `anthropic_version` | `bedrock-2023-05-31` |
| `max_tokens` | `bedrockMaxTokens`, 256 to 4096 (default 1000) |
| `temperature` | `0.0` |
| System prompt | None for the pipeline stages |
| Prompt size limit | 400,000 characters; a larger prompt raises `PromptTooLargeError` and is not sent |
| Response size limit | 1 MiB |
| Client | `read_timeout` 120 seconds, SDK retries off (`max_attempts` 1) |
| Retries | Up to 4 attempts for throttling and transient errors, exponential backoff from 1 second up to 16 seconds |
| Concurrency | At most `bedrockMaxConcurrency` invocations at once across the stages |

Before every retry the stage checks whether the case already has a processed record or a reusable checkpoint for the stage and stops if so. An invocation whose outcome was uncertain (for example a timeout after the request was sent) can still be repeated, so a case may occasionally be sent to Bedrock more than once; there is no exactly-once guarantee. Once a processed record is committed, no further automatic inference happens for that case.

Errors are mapped to `BedrockThrottlingError`, `BedrockTransientError`, `BedrockInvocationError` and `BedrockResponseError` (with the subtypes `MalformedResponse`, `EmptyResponse` and `ResponseTooLarge`). Prompts, replies and AWS error messages are never logged; logs and metrics carry only error classes and counts.

The `global.` inference profiles can route requests to any AWS commercial Region worldwide. Data stays within AWS and the results are stored only in your account. Anthropic models may require the first-time use form and an AWS Marketplace subscription, which IAM permissions cannot complete; see [installation.md](installation.md). At the time of writing, the [Claude Sonnet 4.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-5.html) lists the model as Legacy with an end-of-life date of 8 April 2027.

## Summary validation

`src/lambda/common/summary.py` normalises the reply (Unicode NFC, leading and trailing whitespace removed) and checks it. The summary is accepted only when every rule passes.

| Issue code | Rule |
| --- | --- |
| `too_short`, `too_long` | Length is 600 to 650 characters, counted as Python `len()` of the normalised text |
| `too_few_sentences`, `too_many_sentences` | 4 to 6 sentences. Decimal numbers and common abbreviations (`e.g.`, `i.e.`, `etc.`, `vs.`, `approx.`, `Mr.`, `Ms.`, `Mrs.`, `Dr.`, `Inc.`, `Ltd.`, `No.`, `U.S.`, `St.`) do not end a sentence |
| `line_breaks` | One paragraph, no line breaks |
| `markdown` | No `**`, `__`, backticks, `#` headings, `[text](url)` links or single `*` or `_` emphasis at word boundaries. Technical text such as `snake_case`, `a * b`, `2*3*4` and `*.log` is allowed |
| `list_formatting` | No line starting with `-`, `*`, `•`, `1.` or `1)` |
| `preamble` | Does not start with a label or preamble such as "Summary", "Case summary", "Here is", "Here's", "Below is", "Sure", "Certainly" or "Of course" |
| `self_audit` | No character, sentence or word counts, no "(123 characters)" style notes and no "alternative summary" or "alternative version" |
| `unterminated` | Ends with `.`, `!` or `?` |

If the first reply fails, the stage sends `summary-correction-template.txt` with the failed rules (`MAX_SUMMARY_CORRECTIONS` is 1). If the corrected reply also fails, the stage raises `SummaryValidationError`, the workflow's failure handler records the failure on the workflow failure queue, and the next scheduled run retries the case. A summary is never padded, cut or rewritten by code.

## Classification parsing and validation

`src/lambda/common/json_extract.py` extracts the JSON object from a classifier reply:

1. Replies longer than 200,000 characters are rejected (`input_too_large`).
2. Markdown code fences (for example a fenced `json` block) are removed.
3. Balanced `{...}` candidates are scanned left to right, respecting strings and escapes, and the first candidate that parses as a JSON object is used. Duplicate keys, `NaN` and `Infinity` are rejected.

`src/lambda/common/classification.py` then validates the object:

| Code | Meaning |
| --- | --- |
| `no_json_object` | No candidate parsed as a JSON object |
| `input_too_large` | The reply exceeded 200,000 characters |
| `missing_category` | The category key (`RCA_Category` or `Lifecycle_Category`) is missing, empty or not a string |
| `invalid_category` | The category is not exactly one of the labels (after removing surrounding whitespace). `Unclassified` from the model is also rejected |
| `missing_reason` | The reason key (`RCA_Reason` or `Lifecycle_Reason`) is missing, empty or not a string |
| `reason_too_long` | The reason exceeds 500 characters after whitespace is normalised |

Extra keys are ignored. If the first reply fails, the stage sends `classification-correction-template.txt` once (`MAX_CLASSIFICATION_CORRECTIONS` is 1). If the corrected reply fails too, the stage records `Unclassified` with the reason `Automatic rca classification failed validation after one correction (<code>).` or the lifecycle equivalent, cut to 160 characters, and the workflow continues. `Unclassified` therefore always means "the model's reply could not be accepted", never a judgement about the case.

Worked examples of accepted and rejected replies are in `test/fixtures/prompts/classifications.json`, for example a reply with prose around a fenced block (`rca-fenced-with-prose`), braces inside a string (`rca-braces-in-string`), a label containing a slash (`lifecycle-slash-label`), wrong keys (`lifecycle-wrong-keys`) and prose only (`rca-prose-only`).

## Taxonomies

The categories and definitions below are generated from `src/lambda/common/taxonomy.py`, which is the only source for the prompts, the validators, the Athena and MCP enumerations and this document. Do not edit the generated block by hand.

<!-- taxonomy:start -->
<!-- Generated by scripts/render_taxonomy_docs.py; edit src/lambda/common/taxonomy.py. -->

### Root cause analysis (RCA) categories (16)

JSON keys: `RCA_Category`, `RCA_Reason`.

| Category | Definition |
| --- | --- |
| Customer Question | The customer needed guidance, clarification or how-to information and no fault occurred. |
| Limit reached | An adjustable service quota or account limit was reached and needed an increase. |
| Development Issue | A defect in the customer's own code, configuration or infrastructure definition caused the issue. |
| Customer Release | A deployment or change released by the customer introduced the issue. |
| Lack Monitoring | Missing or inadequate customer monitoring or alerting let the issue go undetected or escalate. |
| Customer Dependency | A third-party or customer-managed dependency outside AWS caused or contributed to the issue. |
| Throttling | API or service request-rate limits throttled the customer's requests. |
| Feature Request | The customer asked for functionality that the service does not currently provide. |
| Exceeding Capability | The workload exceeded the designed capability of the chosen service or resource type, not an adjustable quota. |
| Security Issue | The case concerned a security event, exposed credentials, abuse report or security misconfiguration. |
| Transient Issues | A short-lived problem resolved without intervention and no lasting root cause was identified. |
| ICE Error | An insufficient capacity error: AWS lacked capacity for the requested resource type in that location. |
| Service Event | A broader AWS service event or outage affecting multiple customers caused the issue. |
| AWS Release | A change or deployment released by AWS caused or contributed to the issue. |
| AWS Single Component Failure | Failure of one AWS-managed component, such as a host, volume or node, affected the customer's resource. |
| Upgrade Management | Planning or performing a version upgrade, end-of-support migration or scheduled maintenance caused the issue. |

### Operational lifecycle categories (20)

JSON keys: `Lifecycle_Category`, `Lifecycle_Reason`.

| Category | Definition |
| --- | --- |
| Anomaly Detection | Automatically detecting unusual patterns in metrics or behaviour before they become incidents. |
| Alarming | Configuring actionable alarms and notifications on key metrics and thresholds. |
| AB Testing | Comparing a change against a control group with a controlled traffic split before full rollout. |
| Architecture | Designing workloads for scale, fault isolation and appropriate service selection. |
| Backup/DR | Maintaining tested backups and disaster recovery plans to restore data and service. |
| CICD | Using automated build, test and deployment pipelines with safe rollout and rollback. |
| Code Review | Peer-reviewing code and configuration changes to catch defects before release. |
| Dependency Mapping | Understanding and documenting dependencies between components, services and third parties. |
| Event Management | Tracking AWS Health events, scheduled maintenance and announcements and acting on them in time. |
| Load Testing | Testing workloads at expected and peak load to validate capacity, scaling and quotas. |
| Logging | Capturing and retaining the application and service logs needed for diagnosis. |
| Monitoring | Observing workload health and performance continuously with metrics and dashboards. |
| Performance Benchmarking | Measuring baseline performance to select and size resources correctly. |
| Resilience | Building redundancy, retries and graceful degradation so the workload withstands component failure. |
| Run Books | Maintaining documented, repeatable procedures for operating and recovering the workload. |
| Training | Building team knowledge of AWS services, features and best practices. |
| Fault Injection and GameDay | Deliberately injecting failures and running game days to validate detection and recovery. |
| Ops Reviews | Regularly reviewing operational metrics, incidents and lessons learned to drive improvements. |
| Tracing | Using distributed tracing to follow requests across components and locate failures. |
| Risk Evaluation | Assessing the risk of changes, limits and design decisions before they affect production. |

`Unclassified` is not a category. It is recorded only when a classifier reply is still invalid after one corrective prompt, with a short explanation as the reason.
<!-- taxonomy:end -->

## Prompt version and provenance

Every processed record carries `Model_Id` (the configured inference profile) and `Prompt_Version`. `Prompt_Version` has the form `pv-<16 hex digits>`: the first 16 hexadecimal digits of a SHA-256 digest over all five template files and, for each taxonomy, its JSON keys and rendered category list. Any change to a template, a category label, a definition or a key produces a new version. Each stage and the Update Case Metadata function compute it from the deployed templates layer, and a record is committed only when all three stage checkpoints share the same model and prompt version.

To compute the version of your working copy (repository root):

```bash
CASE_INSIGHTS_TEMPLATES_DIR=src/templates PYTHONPATH=src/lambda \
  .venv/bin/python -c "from common.templates import prompt_version; print(prompt_version())"
```

Stage checkpoints are stored in the raw bucket under `account_number=<id>/case_number=<displayId>/checkpoints/` as `summary.json`, `rca.json` and `lifecycle.json`. A checkpoint is reused, for example when a workflow is retried after a later stage failed, only if it belongs to the same case, model and prompt version, its input digest (SHA-256 of the rendered conversation or of the summary) matches, and its output still passes validation. Otherwise the stage calls Bedrock again.

## Changing prompts, taxonomies or the model

Committed records are never analysed again automatically. A change applies to cases analysed after the deployment; existing records keep their old `Model_Id` and `Prompt_Version`, so query by those fields when you compare results (see [Data quality and provenance](athena.md#data-quality-and-provenance)). To analyse existing cases again with the new prompts or model, use the opt-in tool described in [Reprocess committed cases](runbook.md#reprocess-committed-cases).

**Editing a template.** Keep the placeholders of the file exactly as listed in [Templates](#templates). Keep the untrusted-data tags and instructions, and keep the output requirements consistent with the validators: a prompt that asks for anything the validators reject will produce corrections and failures. A template-only change rebuilds only the templates layer asset (`test/cdk/packaging/python-assets.test.ts` checks this).

**Changing the taxonomy.** Edit `src/lambda/common/taxonomy.py`, then regenerate and check the generated blocks in this document and in [athena.md](athena.md#classification-values):

```bash
.venv/bin/python scripts/render_taxonomy_docs.py --write docs/prompts.md
.venv/bin/python scripts/render_taxonomy_docs.py --write docs/athena.md
.venv/bin/python scripts/render_taxonomy_docs.py --check docs/prompts.md
.venv/bin/python scripts/render_taxonomy_docs.py --check docs/athena.md
```

`--check` exits 1 when a block is out of date and 2 when the markers are missing. A taxonomy change rebuilds the common layer `case-insights-common-<uniqueId>` and changes the MCP filter enumerations. Renaming or removing a label leaves older records with the old label, and the MCP filters then accept only the current labels and `Unclassified`.

**Changing the model.** Set `bedrockModelId` in `config/<env>.json` to the other supported profile. The Bedrock invoke policy follows the configured model (the inference profile, its foundation model in every Region, and the matching AWS Marketplace product), so no IAM change is needed. Checkpoints written for the previous model are not reused. See [configuration.md](configuration.md) and [permissions.md](permissions.md).

After any of these changes, run the offline prompt tests and, if you have Bedrock access, the live regression described below, then review `npx cdk diff --all -c env=<env>` before deploying.

## Testing prompts

The offline tests run in the default suite and need no AWS access:

```bash
.venv/bin/pytest test/prompts -q
```

They use the fixtures in `test/fixtures/prompts/`:

| Fixture | Content |
| --- | --- |
| `summaries.json` | 10 candidate summaries with the expected issue codes, for example `valid-five-sentences`, `markdown-emphasis-asterisk` and `technical-underscore-arithmetic` |
| `classifications.json` | 11 classifier replies with the expected category or error code |
| `case_annotations.json` | Invented conversations (`alb-503`, `dynamodb-throttle`, `injection-attempt`) used to check that rendered prompts are complete and within the size limits |

The tests also check that placeholder text inside case content is not expanded and that the live test stays opt-in.

The live regression sends only the invented conversations in `case_annotations.json` to Amazon Bedrock, runs the summary, RCA and lifecycle prompts with the same single correction as the pipeline, and asserts that every summary validates and both categories are allowed labels. It prints no prompts or replies. It needs credentials allowed `bedrock:InvokeModel` on the chosen inference profile and incurs model charges:

```bash
CASE_INSIGHTS_LIVE_BEDROCK=1 \
CASE_INSIGHTS_LIVE_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0 \
CASE_INSIGHTS_LIVE_REGION=us-east-1 \
  .venv/bin/pytest test/prompts/test_live_bedrock_prompts.py --no-cov
```

`CASE_INSIGHTS_LIVE_MODEL_ID` defaults to the Haiku profile and `CASE_INSIGHTS_LIVE_REGION` to `us-east-1`. The test is marked `live_bedrock` and is skipped unless `CASE_INSIGHTS_LIVE_BEDROCK=1`. It has not been run as part of this repository's verification; running it is part of the deferred live checks.

## Metrics

The stages publish these metrics in the `SupportCaseInsights` namespace (see [monitoring.md](monitoring.md)):

| Metric | Meaning |
| --- | --- |
| `BedrockInvocations`, `BedrockInvocationFailures`, `BedrockThrottles` | Model calls, failed calls and throttled attempts |
| `BedrockLatency`, `BedrockInputTokens`, `BedrockOutputTokens` | Latency in milliseconds and token usage per call |
| `CheckpointsReused`, `StagesSkippedAlreadyProcessed` | Stage results taken from a valid checkpoint, and stages skipped because the case was already committed |
| `SummariesGenerated` | Summaries accepted |
| `InvalidSummaries` | Summary replies that failed validation |
| `SummaryCorrections` | Corrective summary prompts sent |
| `SummaryValidationFailures` | Summaries still invalid after the correction (stage failed) |
| `CasesClassified` | Classifications accepted |
| `InvalidClassifications` | Classifier replies that failed validation |
| `ClassificationCorrections` | Corrective classification prompts sent |
| `ClassificationFallbacks` | Classifications recorded as `Unclassified` |
| `UnclassifiedFields` | `Unclassified` fields in committed records (Update Case Metadata) |
| `OversizedConversations` | Conversations too large to summarise |

A rising `ClassificationFallbacks` or `SummaryValidationFailures` count after a template or model change usually means the prompt and the validators disagree.
