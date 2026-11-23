"""Single source of truth for the RCA and lifecycle classification taxonomies.

Labels are the original brief's exact strings. Definitions are authored only here; prompt
templates, classifier validation and generated documentation all render from these objects.
`UNCLASSIFIED` is not a category: it is the explicit fallback recorded only after one corrective
classification attempt has also failed validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

TaxonomyName = Literal["rca", "lifecycle"]

UNCLASSIFIED: Final = "Unclassified"


@dataclass(frozen=True, slots=True)
class Category:
    """One classification label and its one-line definition."""

    label: str
    definition: str


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """An ordered, immutable set of categories plus the classifier's JSON output keys."""

    name: TaxonomyName
    title: str
    category_key: str
    reason_key: str
    categories: tuple[Category, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        """Category labels in their canonical order."""
        return tuple(category.label for category in self.categories)

    def is_valid(self, label: str) -> bool:
        """Return True when `label` exactly matches a regular category (never `Unclassified`)."""
        return label in self.labels

    def is_record_value(self, label: str) -> bool:
        """Return True when `label` may appear in a processed record: a category or the fallback."""
        return label == UNCLASSIFIED or self.is_valid(label)

    def definition(self, label: str) -> str:
        """Return the definition of `label`; raise `KeyError` for unknown labels."""
        for category in self.categories:
            if category.label == label:
                return category.definition
        raise KeyError(label)

    def render_prompt_list(self) -> str:
        """Render `- Label: definition` lines for prompt templates."""
        return "\n".join(f"- {c.label}: {c.definition}" for c in self.categories)

    def render_markdown_table(self) -> str:
        """Render a Markdown table of labels and definitions for documentation."""
        rows = ["| Category | Definition |", "| --- | --- |"]
        rows.extend(f"| {c.label} | {c.definition} |" for c in self.categories)
        return "\n".join(rows)


def _categories(*pairs: tuple[str, str]) -> tuple[Category, ...]:
    return tuple(Category(label, definition) for label, definition in pairs)


RCA: Final = Taxonomy(
    name="rca",
    title="Root cause analysis (RCA)",
    category_key="RCA_Category",
    reason_key="RCA_Reason",
    categories=_categories(
        (
            "Customer Question",
            "The customer needed guidance, clarification or how-to information and no fault "
            "occurred.",
        ),
        (
            "Limit reached",
            "An adjustable service quota or account limit was reached and needed an increase.",
        ),
        (
            "Development Issue",
            "A defect in the customer's own code, configuration or infrastructure definition "
            "caused the issue.",
        ),
        (
            "Customer Release",
            "A deployment or change released by the customer introduced the issue.",
        ),
        (
            "Lack Monitoring",
            "Missing or inadequate customer monitoring or alerting let the issue go undetected "
            "or escalate.",
        ),
        (
            "Customer Dependency",
            "A third-party or customer-managed dependency outside AWS caused or contributed to "
            "the issue.",
        ),
        (
            "Throttling",
            "API or service request-rate limits throttled the customer's requests.",
        ),
        (
            "Feature Request",
            "The customer asked for functionality that the service does not currently provide.",
        ),
        (
            "Exceeding Capability",
            "The workload exceeded the designed capability of the chosen service or resource "
            "type, not an adjustable quota.",
        ),
        (
            "Security Issue",
            "The case concerned a security event, exposed credentials, abuse report or security "
            "misconfiguration.",
        ),
        (
            "Transient Issues",
            "A short-lived problem resolved without intervention and no lasting root cause was "
            "identified.",
        ),
        (
            "ICE Error",
            "An insufficient capacity error: AWS lacked capacity for the requested resource type "
            "in that location.",
        ),
        (
            "Service Event",
            "A broader AWS service event or outage affecting multiple customers caused the issue.",
        ),
        (
            "AWS Release",
            "A change or deployment released by AWS caused or contributed to the issue.",
        ),
        (
            "AWS Single Component Failure",
            "Failure of one AWS-managed component, such as a host, volume or node, affected the "
            "customer's resource.",
        ),
        (
            "Upgrade Management",
            "Planning or performing a version upgrade, end-of-support migration or scheduled "
            "maintenance caused the issue.",
        ),
    ),
)

LIFECYCLE: Final = Taxonomy(
    name="lifecycle",
    title="Operational lifecycle",
    category_key="Lifecycle_Category",
    reason_key="Lifecycle_Reason",
    categories=_categories(
        (
            "Anomaly Detection",
            "Automatically detecting unusual patterns in metrics or behaviour before they become "
            "incidents.",
        ),
        (
            "Alarming",
            "Configuring actionable alarms and notifications on key metrics and thresholds.",
        ),
        (
            "AB Testing",
            "Comparing a change against a control group with a controlled traffic split before "
            "full rollout.",
        ),
        (
            "Architecture",
            "Designing workloads for scale, fault isolation and appropriate service selection.",
        ),
        (
            "Backup/DR",
            "Maintaining tested backups and disaster recovery plans to restore data and service.",
        ),
        (
            "CICD",
            "Using automated build, test and deployment pipelines with safe rollout and rollback.",
        ),
        (
            "Code Review",
            "Peer-reviewing code and configuration changes to catch defects before release.",
        ),
        (
            "Dependency Mapping",
            "Understanding and documenting dependencies between components, services and third "
            "parties.",
        ),
        (
            "Event Management",
            "Tracking AWS Health events, scheduled maintenance and announcements and acting on "
            "them in time.",
        ),
        (
            "Load Testing",
            "Testing workloads at expected and peak load to validate capacity, scaling and quotas.",
        ),
        (
            "Logging",
            "Capturing and retaining the application and service logs needed for diagnosis.",
        ),
        (
            "Monitoring",
            "Observing workload health and performance continuously with metrics and dashboards.",
        ),
        (
            "Performance Benchmarking",
            "Measuring baseline performance to select and size resources correctly.",
        ),
        (
            "Resilience",
            "Building redundancy, retries and graceful degradation so the workload withstands "
            "component failure.",
        ),
        (
            "Run Books",
            "Maintaining documented, repeatable procedures for operating and recovering the "
            "workload.",
        ),
        (
            "Training",
            "Building team knowledge of AWS services, features and best practices.",
        ),
        (
            "Fault Injection and GameDay",
            "Deliberately injecting failures and running game days to validate detection and "
            "recovery.",
        ),
        (
            "Ops Reviews",
            "Regularly reviewing operational metrics, incidents and lessons learned to drive "
            "improvements.",
        ),
        (
            "Tracing",
            "Using distributed tracing to follow requests across components and locate failures.",
        ),
        (
            "Risk Evaluation",
            "Assessing the risk of changes, limits and design decisions before they affect "
            "production.",
        ),
    ),
)

TAXONOMIES: Final[dict[TaxonomyName, Taxonomy]] = {"rca": RCA, "lifecycle": LIFECYCLE}


def taxonomy_for(name: str) -> Taxonomy:
    """Return the taxonomy called `name` (`"rca"` or `"lifecycle"`)."""
    for key, taxonomy in TAXONOMIES.items():
        if key == name:
            return taxonomy
    raise ValueError(f"Unknown taxonomy {name!r}; expected one of {sorted(TAXONOMIES)}")
