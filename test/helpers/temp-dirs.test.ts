import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { REPO_ROOT } from '../../lib';
import { tempOutdir, testApp } from './cdk';
import { ownedTempDir, ownedTempDirs, removeOwnedTempDirs } from './temp-dirs';

describe('owned temporary directories', () => {
  test('are created under the OS temporary directory and removed without touching other directories', () => {
    const owned = ownedTempDir('case-insights-owned-');
    fs.mkdirSync(path.join(owned, 'nested'));
    fs.writeFileSync(path.join(owned, 'nested', 'file.txt'), 'x');
    const unowned = fs.mkdtempSync(path.join(os.tmpdir(), 'case-insights-owned-'));
    try {
      expect(path.dirname(owned)).toBe(os.tmpdir());
      expect(path.basename(owned)).toMatch(/^case-insights-owned-/);
      expect(ownedTempDirs()).toContain(owned);
      expect(ownedTempDirs()).not.toContain(unowned);

      removeOwnedTempDirs();

      expect(fs.existsSync(owned)).toBe(false);
      expect(fs.existsSync(unowned)).toBe(true);
      expect(ownedTempDirs()).toEqual([]);
    } finally {
      fs.rmSync(unowned, { recursive: true, force: true });
    }
  });

  test('include every CDK test app output directory', () => {
    const outdir = tempOutdir();
    const app = testApp();
    expect(ownedTempDirs()).toEqual(expect.arrayContaining([outdir, app.outdir]));
  });

  test('are removed after a suite whose test fails', () => {
    const recordFile = path.join(ownedTempDir('case-insights-record-'), 'created.json');
    const result = spawnSync(
      process.execPath,
      [
        path.join(REPO_ROOT, 'node_modules', 'jest', 'bin', 'jest.js'),
        '--config',
        path.join(REPO_ROOT, 'jest.config.cjs'),
        '--testMatch',
        '**/test/helpers/fixtures/failing-suite.ts',
        '--coverage=false',
      ],
      { cwd: REPO_ROOT, encoding: 'utf8', env: { ...process.env, CASE_INSIGHTS_TEMP_DIR_RECORD: recordFile }, timeout: 110_000 },
    );

    expect(result.status).toBe(1);
    expect(result.stderr).toMatch(/intentional failure after creating temporary directories/);
    const created = JSON.parse(fs.readFileSync(recordFile, 'utf8')) as string[];
    expect(created).toHaveLength(2);
    for (const dir of created) {
      expect(path.dirname(dir)).toBe(os.tmpdir());
      expect(fs.existsSync(dir)).toBe(false);
    }
  });
});
