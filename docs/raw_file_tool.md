# Binary and multimodal file reads

`read_file_raw` reads a regular file's original bytes. It does not decode text,
normalize line endings, resize images, or recompress their contents.

Example MCP arguments:

```json
{"relative_path": "screenshots/dialog.png"}
```

The default `max_bytes` is **5242880 (5 MiB)**. A caller may choose a lower limit
or raise it up to **20971520 (20 MiB)**. An oversized file produces a tool error;
partial or truncated binary content is never returned. Host/client limits may be
smaller than Serena's limit.

PNG, JPEG, GIF, and WebP signatures produce native MCP `ImageContent` blocks.
Other files (including SVG and PDF) produce an `EmbeddedResource` containing
`BlobResourceContents`. Base64 is used only as the protocol's binary encoding,
not as an ordinary text answer. The metadata text includes the filename, MIME
type, byte count, and SHA-256. The bytes are preserved exactly. Signature detection
selects a format; it is not complete validation of an image's internal structure.
Rendering and model ingestion depend on the client and model's supported formats.

Both `start-mcp-server` and `start-mcp-bridge` preserve these native content blocks.
The shared daemon and both HTTP clients use a dedicated response media type to
keep ordinary text (including JSON-looking file text) distinct from MCP results.
The shared protocol is version **7** following the MCP SDK 2.3.0 migration.
Update and restart all bridges and the daemon together while idle; a running
older bridge cannot gain the new tool automatically.

The tool is read-only, requires an active project, and does not start a language
server. Global/project tool exclusions and fixed tool sets still apply. Reads
outside the active project, including resolved symlink targets, require the
existing host-global `full_access_mode` opt-in. Encoded JetBrains external paths,
directories, pipes, sockets, and devices are rejected. Explicit reads follow the
existing file-read policy rather than discovery-time ignore filtering.

Binary payloads are omitted from Serena's tool-result logs and usage-estimator
input. Usage statistics record the metadata summary, not an image-token estimate.
Prefer `read_file` for source code and other text. This addition is a classic MCP
tool; it does not change the REPL interface's fixed tool set.
