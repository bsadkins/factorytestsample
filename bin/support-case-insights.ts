#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { addCaseInsightsStacks, ConfigValidationError, resolveEnvironmentName } from '../lib';

const app = new App();
try {
  addCaseInsightsStacks(app, { env: resolveEnvironmentName(app.node.tryGetContext('env')) });
} catch (error) {
  if (error instanceof ConfigValidationError) {
    console.error(error.message);
    process.exit(1);
  }
  throw error;
}
