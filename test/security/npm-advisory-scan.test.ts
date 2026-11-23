import * as fs from 'node:fs';
import * as path from 'node:path';
import { ownedTempDir } from '../helpers/temp-dirs';

interface Location { location: string; name: string; version: string; dev: boolean }
interface Finding { name: string; id: number; severity: string; url: string; range: string; affected: Location[] }
interface Exception { advisory: string; name: string; locations: string[]; reason: string; reviewBy: string }
interface Scanner {
  match(advisories: unknown, packages: Map<string, Set<string>>, locations: Location[]): Finding[];
  applyExceptions(findings: Finding[], exceptions: Exception[], today: string): {
    accepted: Finding[];
    unaccepted: Finding[];
    invalidExceptions: { advisory: string; problem: string }[];
  };
  readExceptions(file: string): Exception[];
  readLock(file: string): { packages: Map<string, Set<string>>; locations: Location[] };
}

// eslint-disable-next-line @typescript-eslint/no-require-imports
const scanner = require('../../scripts/security/npm-advisory-scan.cjs') as Scanner;

const REASON = 'Synthetic exception used to exercise the advisory scanner exception logic.';
const lockLocations: Location[] = [
  { location: 'node_modules/demo', name: 'demo', version: '1.2.0', dev: true },
  { location: 'node_modules/other/node_modules/demo', name: 'demo', version: '1.0.0', dev: false },
];
const packages = new Map([['demo', new Set(['1.2.0', '1.0.0'])]]);
const advisories = {
  demo: [{ id: 7, severity: 'high', url: 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc', vulnerable_versions: '>=1.1.0 <1.3.0' }],
};

function exception(overrides: Partial<Exception> = {}): Exception {
  return { advisory: 'GHSA-aaaa-bbbb-cccc', name: 'demo', locations: ['node_modules/demo'], reason: REASON, reviewBy: '2099-01-01', ...overrides };
}

describe('npm advisory scanner', () => {
  const findings = () => scanner.match(advisories, packages, lockLocations);

  test('matches only locked versions inside the vulnerable range', () => {
    expect(findings()).toHaveLength(1);
    expect(findings()[0]?.affected.map((a) => a.location)).toEqual(['node_modules/demo']);
  });

  test('rejects malformed advisory responses instead of reporting clean', () => {
    expect(() => scanner.match([], packages, lockLocations)).toThrow(/not an object/);
    expect(() => scanner.match({ unknown: [] }, packages, lockLocations)).toThrow(/Unexpected advisory package/);
    expect(() => scanner.match({ demo: [{ id: 1 }] }, packages, lockLocations)).toThrow(/Malformed advisory/);
  });

  test('findings without an exception stay unaccepted', () => {
    const result = scanner.applyExceptions(findings(), [], '2026-11-13');
    expect(result.unaccepted).toHaveLength(1);
    expect(result.accepted).toHaveLength(0);
  });

  test('an exact, current exception accepts the finding', () => {
    const result = scanner.applyExceptions(findings(), [exception()], '2026-11-13');
    expect(result.accepted).toHaveLength(1);
    expect(result.unaccepted).toHaveLength(0);
    expect(result.invalidExceptions).toHaveLength(0);
  });

  test('expired exceptions no longer accept findings and are reported', () => {
    const result = scanner.applyExceptions(findings(), [exception({ reviewBy: '2026-01-01' })], '2026-11-13');
    expect(result.unaccepted).toHaveLength(1);
    expect(result.invalidExceptions[0]?.problem).toMatch(/review date/);
  });

  test('exceptions must cover every affected location', () => {
    const result = scanner.applyExceptions(findings(), [exception({ locations: ['node_modules/elsewhere'] })], '2026-11-13');
    expect(result.unaccepted).toHaveLength(1);
  });

  test('unused exceptions are reported as stale', () => {
    const result = scanner.applyExceptions([], [exception()], '2026-11-13');
    expect(result.invalidExceptions[0]?.problem).toMatch(/stale/);
  });

  test('exception files require a specific reason and review date', () => {
    const file = path.join(ownedTempDir('scan-'), 'exceptions.json');
    fs.writeFileSync(file, JSON.stringify({ exceptions: [exception({ reason: 'ok' })] }));
    expect(() => scanner.readExceptions(file)).toThrow(/Malformed advisory exception/);
  });

  test('the committed lockfile and exceptions file are readable', () => {
    const repo = path.resolve(__dirname, '..', '..');
    const lock = scanner.readLock(path.join(repo, 'package-lock.json'));
    expect(lock.locations.length).toBeGreaterThan(100);
    expect(scanner.readExceptions(path.join(repo, 'scripts', 'security', 'npm-advisory-exceptions.json')).length).toBeGreaterThan(0);
  });
});
