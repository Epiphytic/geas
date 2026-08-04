# Structural derivations

## Purpose

Structural anchors are the stable coordinate system between inert document text
and later claims, tables, figures, and model-assisted extraction proposals.
They are canonical immutable records, not inferred SQLite rows. Every anchor
selects an exact Unicode code-point range from one content-addressed text
source.

The first deterministic extractor emits:

- one document anchor;
- real page anchors when the parser preserves page separators, or one page
  explicitly marked `synthetic` when the format has no pagination;
- Markdown/normalized-HTML headings and nested section extents;
- paragraphs, list items, footnotes, and captions.

It does not guess visual headings from typography or ask a model to infer
structure. Unknown structure remains unknown.

## Identity and reproducibility

A `StructuralDerivation` binds the text-derivation ID, source ID and hash, input
media type, extractor identity and version, timestamp, offset unit, ordered
anchor IDs, and counts by kind.

Each `StructuralAnchor` stores:

- its derivation and source identities;
- kind, global document ordinal, code-point start and end;
- SHA-256 of the exact selected text;
- optional label, heading level, parent, and page number;
- whether its page boundary is synthetic.

Anchor identifiers are content-derived from the derivation and selector. A
projection rebuild independently reads the source blob and verifies every range
and exact-text hash before indexing it. Parent relations create:

```text
document
└── page
    ├── section
    │   ├── heading
    │   ├── paragraph
    │   ├── list item
    │   ├── footnote
    │   └── caption
    └── unsectioned blocks
```

Nested sections point to the nearest preceding lower-level section. A section
ends at the next heading of equal or higher rank or at the end of its page.

## Parsing behavior

Poppler PDF extraction preserves form-feed page separators and records parser
version 2. HTML parser version 2 deterministically renders heading levels, list
items, figure captions, and block boundaries into inert normalized text before
structural extraction. No scripts, styles, SVG, remote resources, layout
instructions, or acquired markup become executable configuration.

Office and generic text documents receive exact block anchors. They receive
heading or page structure only when the normalized text contains an explicit
supported marker. Richer parser-produced layout hints can be added as a later
typed derivation without changing existing anchors.

## Storage and query

`parse-document` and governed remote acquisition automatically emit structural
records. Existing text can be reprocessed idempotently:

```bash
uv run geas derive-structure \
  text-derivation:sha256:<digest> \
  --root data
```

Anchors are stored in a verified content-addressed batch to avoid one durable
filesystem operation per block. SQLite schema version 6 projects the derivation
and parent graph. It indexes leaf-block text and heading/section labels under
query kind `anchor`; document and page containers are not duplicated into FTS:

```bash
uv run geas knowledge-query \
  "population evidence uncertainty" \
  --kind anchor \
  --database data/query.sqlite
```

Every anchor hit carries the original and derived source IDs, source URI, trust
zone, exact range, page and parent metadata, and all deterministic threat
observations for the derived source, including type, status, and severity.
Lexical relevance never removes quarantine or hides known poisoning context.

SQLite remains disposable. It cannot add, edit, or reconcile anchors back into
the immutable store.

## Security boundary

Structural extraction is fixed local code with no model, network, secret,
shell, approval, or canonical mutation capabilities beyond its typed output
records. Hostile instructions are ordinary paragraph text and are still handled
by the deterministic threat scanner. Anchor labels never select tools, parser
runtimes, policies, destinations, or queries.
