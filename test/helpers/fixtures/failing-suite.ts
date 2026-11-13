import * as fs from 'node:fs';
import * as path from 'node:path';
import { CfnWaitConditionHandle, Stack } from 'aws-cdk-lib';
import { testApp } from '../cdk';
import { ownedTempDir } from '../temp-dirs';

// Run only by temp-dirs.test.ts in a child Jest process: it fails on purpose after creating
// directories so the parent can check that suite cleanup still removes them.
test('fails after creating temporary directories', () => {
  const recordFile = process.env.CASE_INSIGHTS_TEMP_DIR_RECORD;
  if (recordFile === undefined) {
    throw new Error('CASE_INSIGHTS_TEMP_DIR_RECORD is not set');
  }
  const fixtureDir = ownedTempDir('case-insights-fixture-');
  fs.writeFileSync(path.join(fixtureDir, 'fixture.txt'), 'synthetic');
  const app = testApp();
  new CfnWaitConditionHandle(new Stack(app, 'Fixture'), 'Handle');
  app.synth();
  expect(fs.readdirSync(app.outdir)).toContain('manifest.json');
  fs.writeFileSync(recordFile, JSON.stringify([fixtureDir, app.outdir]));
  throw new Error('intentional failure after creating temporary directories');
});
