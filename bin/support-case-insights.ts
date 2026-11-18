#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { addCaseInsightsApp, ConfigValidationError } from '../lib';

const app = new App();
try {
  addCaseInsightsApp(app);
} catch (error) {
  if (error instanceof ConfigValidationError) {
    console.error(error.message);
    process.exit(1);
  }
  throw error;
}
