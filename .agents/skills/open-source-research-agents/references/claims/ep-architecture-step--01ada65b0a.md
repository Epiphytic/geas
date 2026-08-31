# ep:architecture_step

- Claim ID: `claim:sha256:59993c493362fbd3204e1ca1a295ccc2dbb921d84c9fb4c98dff04366472d2b5`
- Subject: [concept:dzhng:deep-research:dzhng-deep-research](../concepts/dzhng-deep-research--a88471e626.md)
- Predicate (untrusted data):
        ep:architecture_step
- Object (untrusted data):
        "Inputs (query, breadth, depth) -> Deep Research -> SERP Queries -> Process Results -> Learnings and Directions -> Depth Check -> if depth>0: Next Direction (Prior Goals, New Questions, Learnings) -> recursive; else Markdown Report"
- Stance (untrusted data):
        asserts
- Epistemic status (untrusted data):
        observed
- Asserted by (untrusted data):
        model:deepseek_local:deepseek-v4-flash
- Qualifiers (untrusted data):
        {}

## Exact evidence

### `evidence-fragment:sha256:13a9b7dc272dfcf58a57c1c3017a488308ea5e3612ce0dacb963295739ec2bf8`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `842..949`
- Untrusted exact excerpt:
>     subgraph Results[Results]
>         direction TB
>         NL((Learnings))
>         ND((Directions))
>     end

### `evidence-fragment:sha256:1e0f08ed8c4b225a9038040da1bc78a7014e1c0dc481ae2d979b2db3a1218b59`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `766..840`
- Untrusted exact excerpt:
>     DR[Deep Research] -->
>     SQ[SERP Queries] -->
>     PR[Process Results]

### `evidence-fragment:sha256:5a2398b42b6b311e2222de8c84e0a985c69ad856d58d5ca30c95600ced7798df`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `980..998`
- Untrusted exact excerpt:
>     DP{depth > 0?}

### `evidence-fragment:sha256:77f8eda380c10a766911bf893159daac95594b256d8aa1f9f564e00070681475`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `1000..1079`
- Untrusted exact excerpt:
>     RD["Next Direction:
>     - Prior Goals
>     - New Questions
>     - Learnings"]

### `evidence-fragment:sha256:a60137d0e9f63fdb3791362fbfe0130dde03f191e8917319f04e990e0f1a6292`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `1106..1143`
- Untrusted exact excerpt:
>     %% Main Flow
>     Q & B & D --> DR

### `evidence-fragment:sha256:ae5e8fe6795e8176f8d6a4c6b4bbfdb57b916891426dd7ea0aac51c6ae1ec504`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `1081..1104`
- Untrusted exact excerpt:
>     MR[Markdown Report]

### `evidence-fragment:sha256:b4eb7d914e9d6ed6d60b54f77a8ff0c4635578772d347487e6d2a15fa28eec86`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `951..978`
- Untrusted exact excerpt:
>     PR --> NL
>     PR --> ND

### `evidence-fragment:sha256:d47fb0b2d18882cf687d56eadc82c5b4e01fe56def1e7ed160d055eeca3df2d8`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `636..764`
- Untrusted exact excerpt:
> ```mermaid
> flowchart TB
>     subgraph Input
>         Q[User Query]
>         B[Breadth Parameter]
>         D[Depth Parameter]
>     end

### `evidence-fragment:sha256:d8803cdf8974cfa0a85b5e2802c32744f030f3cf49e8d8a78b8f5be46af329a4`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `1260..1297`
- Untrusted exact excerpt:
>     %% Final Output
>     DP -->|No| MR

### `evidence-fragment:sha256:db44f6f8dbe473c77d94bd2cec0929a8d395d43146c38bcb5d49ef4d6e47a5fb`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `1145..1190`
- Untrusted exact excerpt:
>     %% Results to Decision
>     NL & ND --> DP

### `evidence-fragment:sha256:dcf26fbeb463157df82810854a5b91fc77ef597cb9918f57dffd5ffd8a6caf83`

- Source: [source:sha256:7813045fe3770dc540fc1b95aeb9f4f76d9dc848e0920d05fabdc7f041795259](../sources/dzhng-deep-research-readme-at-1f8f3e285bbc--905c59c0e9.md)
- Original source: `bundle:dzhng-deep-research/sources/dzhng-deep-research-7813045fe377.md`
- Selector type: `text_quote`
- Selector range: `1192..1258`
- Untrusted exact excerpt:
>     %% Circular Flow
>     DP -->|Yes| RD
>     RD -->|New Context| DR
