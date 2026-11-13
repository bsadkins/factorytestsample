import { Stack, type StackProps } from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type { CaseInsightsConfig } from '../config';
import { AccountDiscovery } from '../constructs/account-discovery';
import { CaseAnalysis } from '../constructs/case-analysis';
import { CaseCollection } from '../constructs/case-collection';
import { CoreStorage } from '../constructs/core-storage';
import { MemberAccessRoles } from '../constructs/member-access-roles';
import { Observability } from '../constructs/observability';
import { PipelineQueues } from '../constructs/pipeline-queues';
import { SharedLayers } from '../constructs/shared-layers';

export interface CaseInsightsCoreStackProps extends StackProps {
  readonly config: CaseInsightsConfig;
}

/**
 * Core pipeline stack: account discovery, case collection, case analysis and observability.
 * It owns the shared foundation those constructs receive as typed props: storage, stage queues,
 * the execution roles trusted by member accounts and the shared Lambda layers.
 */
export class CaseInsightsCoreStack extends Stack {
  public readonly config: CaseInsightsConfig;
  public readonly layers: SharedLayers;
  public readonly storage: CoreStorage;
  public readonly queues: PipelineQueues;
  public readonly memberAccess: MemberAccessRoles;
  public readonly accountDiscovery: AccountDiscovery;
  public readonly caseCollection: CaseCollection;
  public readonly caseAnalysis: CaseAnalysis;
  public readonly observability: Observability;

  public constructor(scope: Construct, id: string, props: CaseInsightsCoreStackProps) {
    super(scope, id, props);
    this.config = props.config;
    this.storage = new CoreStorage(this, 'Storage', { config: props.config });
    this.queues = new PipelineQueues(this, 'Queues', { config: props.config });
    this.memberAccess = new MemberAccessRoles(this, 'MemberAccess', { config: props.config });
    this.layers = new SharedLayers(this, 'SharedLayers', { uniqueId: props.config.uniqueId });
    this.accountDiscovery = new AccountDiscovery(this, 'AccountDiscovery', {
      config: props.config,
      accountListBucket: this.storage.accountListBucket,
      idempotencyTable: this.storage.idempotencyTable,
      activeAccountsQueue: this.queues.activeAccounts.queue,
      failureQueue: this.queues.accountDiscoveryFailures,
      commonLayer: this.layers.commonLayer,
    });
    this.caseCollection = new CaseCollection(this, 'CaseCollection', {
      config: props.config,
      rawBucket: this.storage.rawBucket,
      processedBucket: this.storage.processedBucket,
      idempotencyTable: this.storage.idempotencyTable,
      activeAccountsQueue: this.queues.activeAccounts.queue,
      annotationQueue: this.queues.annotation.queue,
      summaryQueue: this.queues.summary.queue,
      failureQueue: this.queues.workflowFailures,
      retrievalRole: this.memberAccess.retrievalRole,
      annotationRole: this.memberAccess.annotationRole,
      commonLayer: this.layers.commonLayer,
    });
    this.caseAnalysis = new CaseAnalysis(this, 'CaseAnalysis', {
      config: props.config,
      rawBucket: this.storage.rawBucket,
      processedBucket: this.storage.processedBucket,
      idempotencyTable: this.storage.idempotencyTable,
      summaryQueue: this.queues.summary.queue,
      failureQueue: this.queues.workflowFailures,
      commonLayer: this.layers.commonLayer,
      templatesLayer: this.layers.templatesLayer,
      templateEnvironment: this.layers.templateEnvironment,
    });
    const { accountDiscovery, caseCollection, caseAnalysis, queues } = this;
    this.observability = new Observability(this, 'Observability', {
      config: props.config,
      accountListBucket: this.storage.accountListBucket,
      rawBucket: this.storage.rawBucket,
      commonLayer: this.layers.commonLayer,
      stateMachine: caseAnalysis.stateMachine,
      failureQueue: queues.workflowFailures,
      functions: [
        accountDiscovery.lookupFunction,
        accountDiscovery.readerFunction,
        caseCollection.retrievalFunction,
        caseCollection.annotationFunction,
        caseAnalysis.starterFunction,
        caseAnalysis.stagesFunction,
        caseAnalysis.updateFunction,
        caseAnalysis.failureFunction,
        caseAnalysis.cleanupFunction,
      ],
      deadLetterQueues: [
        { label: 'ActiveAccountsDLQ', queue: queues.activeAccounts.deadLetterQueue },
        { label: 'AnnotationDLQ', queue: queues.annotation.deadLetterQueue },
        { label: 'SummaryDLQ', queue: queues.summary.deadLetterQueue },
        { label: 'WorkflowFailures', queue: queues.workflowFailures },
        { label: 'AccountDiscoveryFailures', queue: queues.accountDiscoveryFailures },
      ],
    });
  }
}
