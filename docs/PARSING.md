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
- PDF text through Poppler `pdftotext` inside a bounded Bubblewrap sandbox.

HTML block and heading structure is rendered into inert text deterministically.
PDF form-feed page boundaries are retained. These markers feed the separate
structural derivation described in
[`STRUCTURAL_DERIVATIONS.md`](STRUCTURAL_DERIVATIONS.md).

HTML scripts, styles, templates, SVG, and remote resources are discarded.
Office macros, embedded media, external resources, and layout are not opened.
Office archives have entry-count and total-uncompressed-size caps. PDF actions,
annotations, images, layout, and embedded files are outside the initial
derivation.

All parser input is capped at 25 MB and derived text at 20 million characters.
Native parsers are fail-closed: no unsandboxed fallback is permitted when
Bubblewrap, `prlimit`, or the required Linux namespace support is unavailable.
Parser timeouts or unsupported formats preserve the original in quarantine and
create an explicit access constraint rather than silently treating binary bytes
as text.

## Native parser boundary

The current native adapter sends the complete input to the parser on stdin and
receives derived text on stdout. Bubblewrap creates fresh user, mount, PID,
network, IPC, UTS, and cgroup namespaces. The sandbox:

- has no bind mount for the workspace, home directory, source file, or host
  configuration;
- sees only read-only system executable/library trees, an empty temporary
  directory, and a minimal device tree;
- clears the inherited environment and supplies only fixed `PATH`, locale, and
  nonexistent `HOME` values;
- drops all capabilities, closes inherited descriptors, and has no network
  namespace route;
- applies CPU, address-space, process, file-descriptor, core-dump, wall-clock,
  input, and output limits.

The parser executable must resolve beneath `/usr`; an acquired document cannot
supply an executable, command option, path, mount, environment variable, or
limit. Sandbox setup and parser errors are redacted to fixed messages so
untrusted stderr cannot become instructions or logs.

Some container hosts disable nested unprivileged user or network namespaces.
Those hosts cannot run native adapters under this policy and must retain the
original until the deployment enables Bubblewrap or provides an approved WASI
adapter.

## WASI/WASM target

WASI is the preferred eventual parser runtime because it can make the
capability boundary portable and remove dependency on Linux namespace policy.
The existing byte-in/text-out adapter and `parser_runtime` provenance field are
designed for that transition. A WASI adapter will be eligible only when it:

- receives content through a bounded in-memory or stdin stream and has no
  preopened host directories, environment inheritance, clocks, randomness, or
  sockets unless a parser contract explicitly requires a deterministic subset;
- has enforced memory, output, wall-time, and instruction/fuel limits;
- is pinned by module digest and parser version, with signatures recorded when
  available;
- passes the same hostile-format, visibility, normalization, and golden-output
  fixtures as the native adapter;
- produces only a typed derivation proposal and never writes canonical state.

WASM availability alone will not trigger fallback or runtime selection.
Parser-to-runtime bindings remain trusted deterministic configuration.

## Trust and evidence

Parsing removes executable structure; it does not establish truth or
trustworthiness. Original and derived sources remain quarantined. Derived text
is scanned by deterministic prompt-injection rules before any later extraction
proposal. Findings are stored as evidence fragments and suspected threat
observations.

Claims continue to require reviewed, exact selectors against a content-addressed
source. The parser identity, version, runtime, original and derived hashes,
warnings, and extraction scope make that path reproducible.

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
