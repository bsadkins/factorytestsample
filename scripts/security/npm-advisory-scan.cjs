'use strict';
/**
 * Full-lockfile npm advisory check.
 *
 * `npm audit` stalls in some environments after receiving advisory data, so this
 * script posts every locked package name/version (production, development and
 * transitive, including bundled dependencies) to the configured registry's bulk
 * advisory endpoint and matches the returned ranges itself. Only package names and
 * versions leave the machine.
 *
 * Advisories with no reachable fix can be accepted in npm-advisory-exceptions.json with an exact
 * advisory, package, lockfile locations, written reason and review date. Accepted findings are still
 * reported; expired, partial or unused exceptions fail the scan.
 *
 * Exit codes: 0 no unaccepted advisories, 1 unaccepted advisories or invalid exceptions,
 * 2 scan error (never treated as clean).
 */
const fs = require('node:fs');
const https = require('node:https');
const path = require('node:path');
const crypto = require('node:crypto');
const semver = require('semver');

const USAGE = 'Usage: node scripts/security/npm-advisory-scan.cjs [--lockfile <path>] [--report <path>] [--exceptions <path>]';
const REGISTRY = (process.env.npm_config_registry || 'https://registry.npmjs.org/').replace(/\/?$/, '/');
const DEADLINE_MS = 30000;

function parseArgs(argv) {
  const repoRoot = path.resolve(__dirname, '..', '..');
  const args = {
    lockfile: path.join(repoRoot, 'package-lock.json'),
    report: path.join(repoRoot, 'reports', 'security', 'npm-advisories.json'),
    exceptions: path.join(__dirname, 'npm-advisory-exceptions.json'),
  };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === '--help' || flag === '-h') {
      console.log(USAGE);
      process.exit(0);
    }
    const value = argv[i + 1];
    if (['--lockfile', '--report', '--exceptions'].includes(flag) && value) {
      args[flag.slice(2)] = path.resolve(value);
      i += 1;
    } else {
      throw new Error(`Unknown or incomplete argument: ${flag}\n${USAGE}`);
    }
  }
  return args;
}

function readLock(lockfile) {
  const raw = fs.readFileSync(lockfile);
  const lock = JSON.parse(raw.toString('utf8'));
  if (![2, 3].includes(lock.lockfileVersion) || !lock.packages) {
    throw new Error('Requires an npm lockfile v2 or v3 with a "packages" map');
  }
  const packages = new Map();
  const locations = [];
  for (const [location, entry] of Object.entries(lock.packages)) {
    if (location === '') continue;
    if (entry.link) throw new Error(`Unresolved local link in lockfile: ${location}`);
    const name = entry.name || location.split('node_modules/').pop();
    if (!name || !semver.valid(entry.version)) throw new Error(`Unscannable lockfile entry: ${location}`);
    if (!packages.has(name)) packages.set(name, new Set());
    packages.get(name).add(entry.version);
    locations.push({ location, name, version: entry.version, dev: Boolean(entry.dev) });
  }
  if (locations.length === 0) throw new Error('Lockfile contains no packages');
  return { raw, lock, packages, locations };
}

function postBulk(payload) {
  return new Promise((resolve, reject) => {
    const url = new URL('-/npm/v1/security/advisories/bulk', REGISTRY);
    const req = https.request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('error', reject);
      res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString('utf8') }));
    });
    req.on('error', reject);
    req.setTimeout(20000, () => req.destroy(new Error('Advisory request socket timeout')));
    req.end(payload);
  });
}

function match(advisories, packages, locations) {
  if (!advisories || typeof advisories !== 'object' || Array.isArray(advisories)) {
    throw new Error('Advisory response is not an object');
  }
  const findings = [];
  for (const [name, entries] of Object.entries(advisories)) {
    if (!packages.has(name) || !Array.isArray(entries)) throw new Error(`Unexpected advisory package ${name}`);
    for (const a of entries) {
      if (!a.id || !a.severity || typeof a.vulnerable_versions !== 'string' || !semver.validRange(a.vulnerable_versions)) {
        throw new Error(`Malformed advisory for ${name}`);
      }
      const affected = locations.filter(
        (p) => p.name === name && semver.satisfies(p.version, a.vulnerable_versions, { includePrerelease: true }),
      );
      if (affected.length) {
        findings.push({ name, id: a.id, title: a.title, severity: a.severity, url: a.url, range: a.vulnerable_versions, affected });
      }
    }
  }
  return findings;
}

function ghsaOf(finding) {
  const match = /GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}/.exec(finding.url || '');
  return match ? match[0] : String(finding.id);
}

function readExceptions(file) {
  const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!Array.isArray(parsed.exceptions)) throw new Error(`${file} must contain an "exceptions" array`);
  for (const e of parsed.exceptions) {
    const valid = typeof e.advisory === 'string' && typeof e.name === 'string' && Array.isArray(e.locations)
      && e.locations.length > 0 && typeof e.reason === 'string' && e.reason.length >= 40
      && /^\d{4}-\d{2}-\d{2}$/.test(e.reviewBy || '');
    if (!valid) throw new Error(`Malformed advisory exception: ${JSON.stringify(e)}`);
  }
  return parsed.exceptions;
}

/** Splits findings into accepted and unaccepted; reports exceptions that are expired or unused. */
function applyExceptions(findings, exceptions, today) {
  const accepted = [];
  const unaccepted = [];
  const used = new Set();
  for (const finding of findings) {
    const ghsa = ghsaOf(finding);
    const locations = finding.affected.map((a) => a.location);
    const exception = exceptions.find((e) => e.advisory === ghsa && e.name === finding.name
      && e.reviewBy >= today && locations.every((l) => e.locations.includes(l)));
    if (exception) {
      used.add(exception);
      accepted.push({ ...finding, advisory: ghsa, reason: exception.reason, reviewBy: exception.reviewBy });
    } else {
      unaccepted.push({ ...finding, advisory: ghsa });
    }
  }
  const invalidExceptions = exceptions.filter((e) => !used.has(e)).map((e) => ({
    advisory: e.advisory,
    name: e.name,
    problem: e.reviewBy < today ? `review date ${e.reviewBy} has passed` : 'no matching finding (remove the stale exception)',
  }));
  return { accepted, unaccepted, invalidExceptions };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { raw, lock, packages, locations } = readLock(args.lockfile);
  const payload = JSON.stringify(Object.fromEntries([...packages].map(([n, vs]) => [n, [...vs]])));
  const started = Date.now();
  const { status, body } = await postBulk(payload);
  if (status !== 200) throw new Error(`Advisory endpoint returned HTTP ${status}: ${body.slice(0, 300)}`);
  const findings = match(JSON.parse(body), packages, locations);
  const today = new Date().toISOString().slice(0, 10);
  const { accepted, unaccepted, invalidExceptions } = applyExceptions(findings, readExceptions(args.exceptions), today);
  const result = {
    scanner: 'npm-bulk-lock-scan/1.0.0',
    registry: REGISTRY,
    lockfile: path.relative(process.cwd(), args.lockfile),
    sha256: crypto.createHash('sha256').update(raw).digest('hex'),
    lockfileVersion: lock.lockfileVersion,
    elapsedMs: Date.now() - started,
    locations: locations.length,
    devLocations: locations.filter((p) => p.dev).length,
    packageNames: packages.size,
    findings: unaccepted,
    acceptedFindings: accepted,
    invalidExceptions,
  };
  fs.mkdirSync(path.dirname(args.report), { recursive: true });
  fs.writeFileSync(args.report, `${JSON.stringify(result, null, 2)}\n`);
  const summarise = (list) => list.map(({ affected, ...f }) => ({ ...f, locations: affected.map((a) => a.location) }));
  console.log(JSON.stringify({ ...result, findings: summarise(unaccepted), acceptedFindings: summarise(accepted) }, null, 2));
  return unaccepted.length || invalidExceptions.length ? 1 : 0;
}

if (require.main === module) {
  const deadline = setTimeout(() => {
    console.error(`npm advisory scan exceeded ${DEADLINE_MS} ms; result is NOT clean`);
    process.exit(2);
  }, DEADLINE_MS);

  main().then((code) => {
    clearTimeout(deadline);
    process.exitCode = code;
  }, (err) => {
    clearTimeout(deadline);
    console.error(`npm advisory scan failed; result is NOT clean: ${err.message}`);
    process.exitCode = 2;
  });
}

module.exports = { applyExceptions, match, readExceptions, readLock };
