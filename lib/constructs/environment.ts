import { RemovalPolicy } from 'aws-cdk-lib';
import type { CaseInsightsConfig } from '../config';

/** Prod keeps data on stack deletion; dev is disposable. */
export function isProduction(config: Pick<CaseInsightsConfig, 'environment'>): boolean {
  return config.environment === 'prod';
}

/** Removal policy for stateful resources: RETAIN in prod, DESTROY in dev. */
export function dataRemovalPolicy(config: Pick<CaseInsightsConfig, 'environment'>): RemovalPolicy {
  return isProduction(config) ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
}
