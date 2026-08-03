# Original documents and derived text

## Format-neutral contract

Every acquired document has two conceptually separate layers:

1. immutable original bytes with their source URI, media type, acquisition
   time, connector, license, and content hash;
2. one or more content-addressed derivations produced by named, versioned
   parsers.

The original is retained even when no parser is installed. Adding support for a
new format creates a new derivation; it does not replace or reinterpret the
original record. Text is the first derivation scope. Tables, figures, diagrams,
audio, video, layout, and other semantics can be added later as separately
typed derivations.

## Initial text adapters

The registry currently supports:

- UTF-8-compatible plain text and other `text/*` formats;
- canonicalized JSON;
- visible HTML/XHTML text;
- XML text with DTDs and entities forbidden;
- DOCX, PPTX, XLSX, ODT and related safe XML text parts;
- PDF text through a bounded, non-shell `pdftotext` subprocess.

HTML scripts, styles, templates, SVG, and remote resources are discarded.
Office macros, embedded media, external resources, and layout are not opened.
Office archives have entry-count and total-uncompressed-size caps. PDF actions,
annotations, images, layout, and embedded files are outside the initial
derivation.

All parser input is capped at 25 MB and derived text at 20 million characters.
Parser timeouts or unsupported formats preserve the original in quarantine and
create an explicit access constraint rather than silently treating binary bytes
as text.

## Trust and evidence

Parsing removes executable structure; it does not establish truth or
trustworthiness. Original and derived sources remain quarantined. Derived text
is scanned by deterministic prompt-injection rules before any later extraction
proposal. Findings are stored as evidence fragments and suspected threat
observations.

Claims continue to require reviewed, exact selectors against a content-addressed
source. The parser identity, version, original and derived hashes, warnings, and
extraction scope make that path reproducible.

## Remote acquisition

Remote content acquisition requires an existing location with a deterministically
permitted license. The fetcher:

- accepts HTTPS only and rejects credentials and non-default ports;
- resolves a public IPv4 address before each request and pins the connection to
  that address while retaining TLS hostname verification;
- disables proxies, cookies, authentication, and automatic redirects;
- validates and pins every redirect destination independently;
- permits at most three redirects, 25 MB, and 30 seconds;
- stores no remote instructions as configuration.

If a preferred publisher location is blocked, acquisition tries the remaining
licensed manifestations in deterministic order. Unknown, `other-oa`, NC, and
ND license classes remain review-gated.
