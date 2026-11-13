import { Stack } from 'aws-cdk-lib';
import { Bucket } from 'aws-cdk-lib/aws-s3';
import { acknowledgeCdkWarning, acknowledgeWildcardResourceFinding } from '../lib/nag';
import { acknowledgedRules, testApp } from './helpers/cdk';

const REASON = 'A specific written reason that is long enough to be accepted.';

describe('acknowledgement helpers', () => {
  test('aws-cdk-lib warnings need a qualified warning ID and a specific reason', () => {
    const stack = new Stack(testApp(), 'Ack');
    expect(() => { acknowledgeCdkWarning(stack, 'AwsSolutions-S1', REASON); }).toThrow(/Invalid aws-cdk-lib warning ID/);
    expect(() => { acknowledgeCdkWarning(stack, '@aws-cdk/aws-s3:accessLogsPolicyNotAdded', 'too short'); }).toThrow(/specific reason/);
    expect(() => { acknowledgeCdkWarning(stack, '@aws-cdk/aws-s3:accessLogsPolicyNotAdded', REASON); }).not.toThrow();
  });

  test('wildcard resource acknowledgements need a specific reason and are made once, at synthesis', () => {
    const app = testApp();
    const stack = new Stack(app, 'Ack');
    const bucket = new Bucket(stack, 'Bucket');
    expect(() => { acknowledgeWildcardResourceFinding(stack, bucket.arnForObjects('*'), 'too short'); }).toThrow(/specific reason/);
    acknowledgeWildcardResourceFinding(stack, bucket.arnForObjects('prefix/*'), REASON);
    const acknowledgements = (): string[] => acknowledgedRules(stack).map(({ id }) => id);
    expect(acknowledgements()).toEqual([]);
    app.synth();
    app.synth({ force: true });
    const logicalId = stack.getLogicalId(bucket.node.defaultChild as never);
    expect(acknowledgements()).toEqual([`Annotation::AwsSolutions-IAM5[Resource::<${logicalId}.Arn>/prefix/*]`]);
  });
});
