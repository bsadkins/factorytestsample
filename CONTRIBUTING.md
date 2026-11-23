# Contributing

Thank you for your interest in improving Support Case Insights. This guide explains how to report problems, propose changes and get a pull request merged. Contributions are accepted under the project's [MIT No Attribution licence](LICENSE), and everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and requesting features

Use GitHub issues for bugs, documentation errors and feature requests. Before opening one, search the existing open and closed issues. A good bug report includes:

- what you ran (the exact command, run from the repository root) and the commit or release you used;
- what you expected and what happened, with the relevant error output;
- your environment: operating system, Node.js and Python versions, AWS CDK CLI version (`npx cdk --version`) and AWS Region;
- the relevant parts of `config/<env>.json`, with account IDs, organisation IDs, email addresses and principal ARNs replaced by placeholders such as `111111111111` and `o-exampleorg1`.

**Never include support case content, customer data, log extracts containing account or case identifiers, credentials, session tokens or signed request headers in an issue, pull request or commit.** Use invented data in examples and tests.

### Security issues

Do not report security vulnerabilities in public issues. Use GitHub's private vulnerability reporting for this repository (the **Security** tab, then **Report a vulnerability**). If that is unavailable, contact the repository owner privately and wait for a reply before disclosing anything. [Security](docs/security.md) describes the threat model and the controls the solution relies on.

## Proposing a change

For anything larger than a small fix, open an issue first to agree the approach. This matters most for changes to IAM permissions, encryption, the member account role, the Bedrock model list, the taxonomies, the MCP tools or the retry and idempotency behaviour, because those are deliberate design decisions recorded in the [documentation](README.md#documentation).

### Branches

1. Fork the repository, or create a branch if you have write access. Never commit directly to `main`; the optional pipeline deploys from `main` (see [Optional CDK pipeline](docs/pipeline.md)).
2. Branch from the latest `main` and use a short, descriptive name with a prefix, for example `fix/cleanup-grace-check`, `feature/mcp-new-tool` or `docs/athena-queries`.
3. Keep one logical change per branch and rebase on `main` before asking for review.

### Commits

- Write the subject line in the imperative mood, at most about 72 characters, without a trailing full stop, for example `Add week-of-month filter to get_temporal_patterns`.
- Explain in the body why the change is needed and anything a reviewer should check, wrapped at about 72 characters.
- Keep generated output out of commits (`cdk.out/`, `.cache/`, `.venv/`, `node_modules/`, coverage reports). Lock files (`package-lock.json`, `requirements.txt`, `requirements-dev.txt`) are committed and must stay consistent with their sources.
- Do not commit `config/pipeline.json` or environment files that contain real account details to a public fork.

### Before opening a pull request

Set up the development environment as described in the [development guide](docs/development.md), then run the complete local verification from the repository root:

```bash
npm run verify
```

It runs ESLint and Ruff, the strict TypeScript and mypy type checks, the Jest and pytest suites (with the 80% coverage threshold for `src/lambda`), and `cdk synth` for both shipped environments with cdk-nag. It never calls AWS. Run the dependency and code scans as well when you change dependencies or Python code:

```bash
npm run security
```

Also:

- add or update tests for every behaviour you change, using invented data only;
- update the snapshots only when the template change is intended (`npx jest -u`), and explain the change in the pull request;
- acknowledge a cdk-nag finding only on the narrowest construct, with a specific written reason, and list it in [Security](docs/security.md#cdk-nag);
- update the documentation and the [changelog](CHANGELOG.md) (under `Unreleased`) in the same pull request;
- write prose in British English and keep AWS product and API names as AWS spells them.

### Continuous integration

Every pull request runs the GitHub Actions workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml). It uses Node.js 24.22.3 (from `.nvmrc`) and Python 3.12.3 on `ubuntu-24.04`, installs the locked dependencies with `npm ci` and `pip install --require-hashes -r requirements-dev.txt`, runs `npm run verify` and fails if verification changed any file in the working tree. It has no AWS credentials or repository secrets, so a change that needs them to pass is not accepted. Run the same commands locally before you push.
### Pull requests

1. Open the pull request against `main` and fill in what changed, why, and how you verified it (the commands you ran and their results). Say clearly if anything needs a real AWS deployment to verify and has not been run.
2. Keep pull requests focused and reasonably small; a reviewer must be able to check every IAM, network and data-handling change.
3. Respond to review comments with further commits; the maintainer may squash them when merging.
4. A pull request is merged only after the CI check `npm run verify` passes and a maintainer has approved it.

## Licensing

By contributing, you agree that your contributions are licensed under the [MIT No Attribution licence](LICENSE) that covers this project.
