GEGL FILTER SYSTEM: MCP CLIENT GUIDE

This guide provides step-by-step instructions for MCP clients (Claude Desktop, Python scripts, and other MCP clients) to discover, apply, and preview GEGL filters exposed by the GIMP MCP server.

Prerequisites
- MCP server is running and accessible (default host: localhost, port: 9877).
- The MCP server exposes Python-Fu / GEGL-related procedures via the call_api interface:
  - api_path: "exec"
  - args: array of Python expressions to execute
- The MCP client can send JSON payloads and receive structured JSON responses.

1) Discover GEGL Filters
- Purpose: list available GEGL filters that can be applied to images.
- MCP client tasks:
  - Claude Desktop
  - Python scripts
  - Other MCP clients
- Client-specific examples:
  - Claude Desktop
    - Use the MCP call_api tool to execute list_gegl_filters():
    
    Code block (JSON payload):
    ```json
    {
      "api_path": "exec",
      "args": ["list_gegl_filters()"]
    }
    ```
    - Expected response: a list of filters and their parameters (success payload depends on server implementation).
  - Python script
    - Build the same payload and send it (example uses requests.post):
    
    Code block (Python):
    ```python
    import json, requests
    payload = {"api_path": "exec", "args": ["list_gegl_filters()"]}
    resp = requests.post("http://localhost:9877/call_api", json=payload, timeout=5)
    print(resp.json())
    ```
  - Other MCP clients
    - Construct the identical JSON payload and POST to the MCP server endpoint.
    - Ensure you handle JSON response and parse the list of available filters.

2) Apply a GEGL Filter
- Purpose: apply a selected GEGL filter to a specific image layer.
- Client tasks:
  - Identify target image and layer identifiers (image_id, layer_id or similar API objects).
  - Choose filter name and parameter set.
- Examples:
  - Claude Desktop
    - Example to apply Gaussian blur with radius 8 on image_id 42, layer_id 1:
    
    Code block (JSON payload):
    ```json
    {
      "api_path": "exec",
      "args": ["apply_gegl_filter(\"gaussian-blur\", 42, 1, {\"radius\": 8})"]
    }
    ```
  - Python script
    - Equivalent payload:
    ```python
    payload = {"api_path": "exec", "args": ["apply_gegl_filter(\"gaussian-blur\", 42, 1, {\"radius\": 8})"]}
    resp = requests.post("http://localhost:9877/call_api", json=payload, timeout=5)
    print(resp.json())
    ```
  - Other MCP clients
    - Use the same payload shape and post to the MCP server endpoint.
  - Preview after applying
    - If supported, trigger a live preview or render preview on a temporary layer:
    
    Code block (JSON payload):
    ```json
    {
      "api_path": "exec",
      "args": ["preview_gegl_filter(\"gaussian-blur\", 42, 1, {\"radius\": 8})"]
    }
    ```

3) Preview GEGL Filters
- Purpose: preview a GEGL filter before committing changes.
- Claude Desktop / MPC clients
  - Call preview_gegl_filter with the same parameters as apply_gegl_filter.
  - Expect a preview image update on the target image and layer (if supported by the client/UI).
- Python example:
  - Payload:
    ```json
    {
      "api_path": "exec",
      "args": ["preview_gegl_filter(\"gaussian-blur\", 42, 1, {\"radius\": 8})"]
    }
    ```
  - Handling the preview response:
    - The response may include a reference to a preview image or status indicating success.
    - If the server returns a temporary preview, refresh the client view accordingly.

4) Error Handling
- Structured error responses
  - On success, the MCP server returns a payload with status and result.
  - On error, the MCP server returns a structured error object, for example:
    ```json
    {
      "status": "error",
      "error": {
        "code": "GEGL_FILTER_001",
        "message": "Unknown filter name",
        "details": "Filter 'gaussian-blur' is not available in this build",
        "trace": "gegl_filter.py:123 in apply_gegl_filter"
      }
    }
    ```
- How to interpret
  - Read error.code for machine-friendly categorization.
  - Read error.message for human-friendly explanation.
  - Check error.details for additional context (e.g., invalid parameter names, missing resources).
  - Use error.trace to locate the failure origin in the server code.
- Client-side handling guidance
  - Log structured errors to a dedicated error log.
  - Show a concise user-facing message (error.message) and, if available, error.details.
  - If a transient issue (e.g., server busy), implement a retry with backoff.
  - If authentication or permission issues arise, surface a clear permission error and guidance to re-authenticate.

5) Troubleshooting quick reference
- Example failure: invalid filter name
  - Response: status: "error", error.code: GEGL_FILTER_001, error.message: "Unknown filter"
- Example failure: invalid parameters
  - Response: status: "error", error.code: GEGL_FILTER_002, error.message: "Invalid parameters", error.details: "radius must be a non-negative number"
- Example failure: server unavailable
  - Response: status: "error", error.code: "SERVER_UNAVAILABLE", error.message: " MCP server not reachable"

Appendix: API interaction patterns
- Discover: list_gegl_filters()
- Apply: apply_gegl_filter(filter_name, image_id, layer_id, {parameters})
- Preview: preview_gegl_filter(filter_name, image_id, layer_id, {parameters})
- The exact function signatures may vary across MCP server implementations; adapt accordingly.

This guide is intended to be a quick, practical reference for MCP clients integrating GEGL filtering into their workflows.
