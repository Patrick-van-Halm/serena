# Binary and multimodal file reads

`read_files_raw` reads original bytes from one to eight regular files in one call. It does not decode
text, normalize line endings, resize images, or recompress their contents. Use a one-item `files`
array for one file.

Example MCP arguments:

```json
{
  "files": [
    {"relative_path": "screenshots/dialog.png"},
    {"relative_path": "assets/icon.webp", "max_bytes": 1048576}
  ],
  "max_total_bytes": 20971520
}
```

Each item defaults to **5242880 bytes (5 MiB)** and accepts a `max_bytes` limit up to
**20971520 (20 MiB)**. The batch `max_total_bytes` defaults to and is capped at **20 MiB**.
These limits count original binary bytes; base64 encoding and metadata add transport overhead.
Host/client limits may be smaller. The entire request shape and limits are checked before reads
begin. An oversized or inaccessible file produces an individual error; successful siblings are
preserved. Binary content is never truncated to fit a limit. Cancellation stops later reads.

The result contains ordered metadata text blocks, one for every request. Each identifies the
request `index`, `relative_path`, `status` (`completed` or `error`), and `error`. Successful metadata
also includes the filename, MIME type, byte count, and SHA-256, followed by its native content block.
Failed requests have no binary content block. Use the metadata to associate native content with
its file; an error changes the number of blocks preceding later files.

PNG, JPEG, GIF, and WebP signatures produce native MCP `ImageContent` blocks. Other files,
including SVG and PDF, produce `EmbeddedResource` blocks containing `BlobResourceContents`.
Base64 is used only as the protocol's binary encoding, not as an ordinary text answer. Bytes are
preserved exactly. Signature detection selects a format; it is not complete validation of an
image's internal structure. Rendering and model ingestion depend on the client and model.

Both `start-mcp-server` and `start-mcp-bridge` preserve native content blocks. The shared daemon
and HTTP clients use a dedicated response media type to keep ordinary text, including JSON-looking
file text, distinct from native MCP results. The shared protocol remains version **7**. Runtime
fingerprints cover the implementation. Update and restart all bridges and the daemon together
while idle, then refresh the client's tool catalog.

The tool is read-only and does not start a language server. With an active project, paths use
its root; otherwise they use the selected startup working directory without creating a project.
Global/project exclusions and fixed tool sets still apply; the old configuration name
`read_file_raw` maps to `read_files_raw`. The old scalar MCP endpoint and argument shape are not
preserved. Reads outside the selected root, including resolved symlink targets, still require the
host-global `full_access_mode` setting. Encoded JetBrains external paths, directories, pipes,
sockets, and devices are rejected. Explicit reads follow the existing file-read policy rather
than discovery-time ignore filtering. Reads are serial and do not create a multi-file snapshot.

Binary payloads are omitted from tool-result logs and usage-estimator input. Usage statistics
record the metadata summary, not an image-token estimate. Prefer `read_files` for source code and
other text, including one file. The REPL interface retains its fixed tool set.
