# Publication plan

## GitHub now

Publish this directory on a dedicated Cortex proposal branch and draft pull request. Review the specification, threat model, schema, and conformance plan together. Mark the proposal experimental. The existing Cortex runtime is not a GAP implementation.

The current repository contains differently worded `LICENSE` and `License` files. The draft does not resolve or replace them. Before presenting a separate specification as openly implementable, the author should choose explicit document/code licensing and contribution terms. Public visibility alone does not establish those rights.

## Possible arXiv paper

The companion RESEARCH-NOTE.md is a starting manuscript, not a submission-ready or submitted paper. It includes an abstract, conditional properties, prior work, evaluation design, and limitations. No research results are asserted.

Recommended research sequence:

1. Review the full closest prior work and identify a concrete contribution or decide to extend an existing proposal.
2. Implement the minimal profile and a synthetic transactional/idempotent adapter.
3. Run the adversarial and recovery suite; publish the actual observations.
4. Compare two independently implemented integrations with appropriate baselines.
5. Revise the manuscript, verify citations, and prepare LaTeX source and a reviewed PDF.
6. Have the author select a suitable category, license, and account details and self-submit if they choose to proceed.

Experiments are our proposed strengthening step, not a claim that arXiv requires every paper to contain experiments. arXiv requests topical, refereeable scholarly contributions; submissions are moderated and acceptance is not guaranteed. Registered authors may need endorsement when submitting to a new category. arXiv expects author self-submission and an irrevocable distribution license. We have not selected a license, logged into an arXiv account, or submitted anything.

Primary guidance checked 2026-09-25:

- [Submission overview](https://info.arxiv.org/help/submit/index.html)
- [Endorsement](https://info.arxiv.org/help/endorsement.html)
- [Code of conduct](https://info.arxiv.org/help/policies/code_of_conduct.html)

Author review should establish responsibility for technical claims, attribution, rights, and the use of AI-assisted drafting. Do not add institutional affiliations, ORCID identifiers, endorsements, benchmark results, or coauthors without verification.
