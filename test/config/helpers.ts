import * as fs from 'node:fs';
import * as path from 'node:path';
import { type CaseInsightsConfig, CONFIG_DIR, type EnvironmentName, loadEnvironmentConfig } from '../../lib';
import { ownedTempDir } from '../helpers/temp-dirs';

export type JsonObject = Record<string, unknown>;

/**
 * Committed synthetic dev, prod and pipeline configurations for fixed assertions and snapshots.
 * Operators edit config/, so tests that expect particular accounts, names, flags or templates read
 * these files instead; the files in config/ are checked by test/config/operator-config.test.ts and
 * synthesised by `npm run synth`.
 */
export const FIXTURE_CONFIG_DIR = path.resolve(__dirname, '..', 'fixtures', 'config');

export type FixtureConfigFile = 'dev' | 'prod' | 'pipeline';

/** Reads a file from the repository config/ directory (schemas and committed examples). */
export function readConfigJson(fileName: string): JsonObject {
  return JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, fileName), 'utf8')) as JsonObject;
}

/** Reads test/fixtures/config/<file>.json. */
export function readFixtureJson(file: FixtureConfigFile): JsonObject {
  return JSON.parse(fs.readFileSync(path.join(FIXTURE_CONFIG_DIR, `${file}.json`), 'utf8')) as JsonObject;
}

/** The validated fixture configuration of one environment. */
export function fixtureEnvironmentConfig(env: EnvironmentName): CaseInsightsConfig {
  return loadEnvironmentConfig(env, { configDir: FIXTURE_CONFIG_DIR });
}

/**
 * A fresh config directory holding the listed fixture files with top-level overrides; `null` omits a
 * file. Files not listed are not written.
 */
export function fixtureConfigDir(files: Partial<Record<FixtureConfigFile, JsonObject | null>>): string {
  const dir = ownedTempDir('case-insights-config-');
  for (const [file, overrides] of Object.entries(files) as [FixtureConfigFile, JsonObject | null][]) {
    if (overrides !== null) {
      fs.writeFileSync(path.join(dir, `${file}.json`), JSON.stringify({ ...readFixtureJson(file), ...overrides }));
    }
  }
  return dir;
}

/** Smallest valid dev configuration: only the required fields. */
export function minimalConfig(overrides: JsonObject = {}): JsonObject {
  return {
    uniqueId: 'scitest',
    orgId: 'o-exampleorg1',
    managementAccountId: '111111111111',
    environment: 'dev',
    memberRoleTargets: ['r-ab12'],
    ...overrides,
  };
}
