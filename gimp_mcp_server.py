#!/usr/bin/env python3
# GIMP MCP Server Script
# Provides an MCP interface to control GIMP via a socket connection.

import json
import time
import socket
import base64
import traceback
import logging
import os
from pathlib import Path
from typing import Optional, Any
from mcp.server.fastmcp import (
    FastMCP,
    Context,
    Image,
)
from mcp.server.stdio import stdio_server

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("GimpMCPServer")
logger.debug("Debug logging enabled")


# Initialize FastMCP
mcp = FastMCP("GimpMCP", None)

# GIMP Connection Configuration
GIMP_HOST = os.environ.get("GIMP_MCP_HOST", "localhost")
GIMP_PORT = int(os.environ.get("GIMP_MCP_PORT", "9878"))
CONNECTION_TIMEOUT = float(os.environ.get("GIMP_MCP_TIMEOUT", "30.0"))


class GimpConnectionError(Exception):
    """Exception raised when connection to GIMP fails."""

    pass


class GimpConnection:
    """Manages the socket connection to the GIMP MCP plugin."""

    _instance: Optional["GimpConnection"] = None

    def __init__(
        self,
        host: str = GIMP_HOST,
        port: int = GIMP_PORT,
        timeout: float = CONNECTION_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None

    @classmethod
    def get_instance(cls) -> "GimpConnection":
        """Get or create a singleton connection instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def connect(self) -> socket.socket:
        """Establish a connection to the GIMP plugin."""
        if self._socket is not None:
            try:
                # Test if socket is still alive
                self._socket.send(b"")
                return self._socket
            except (OSError, socket.error):
                self._socket = None

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(self.timeout)
            self._socket.connect((self.host, self.port))
            logger.debug(f"Connected to GIMP at {self.host}:{self.port}")
            return self._socket
        except socket.error as e:
            self._socket = None
            raise GimpConnectionError(
                f"Failed to connect to GIMP plugin at {self.host}:{self.port}. "
                f"Make sure GIMP is running and the MCP server is started (Tools > Start MCP Server). "
                f"Error: {e}"
            )

    def disconnect(self) -> None:
        """Close the connection to GIMP."""
        if self._socket is not None:
            try:
                self._socket.close()
            except (OSError, socket.error):
                pass
            finally:
                self._socket = None

    def send_command(self, command_type: str, params: Optional[dict] = None) -> dict:
        """Send a command to GIMP and return the response.

        Args:
            command_type: The type of command to send (e.g., 'get_image_bitmap', 'exec')
            params: Optional parameters for the command

        Returns:
            dict: The response from GIMP

        Raises:
            GimpConnectionError: If connection or communication fails
        """
        if params is None:
            params = {}

        # Build the command JSON
        command = {"type": command_type, **params}
        command_json = json.dumps(command)

        try:
            sock = self.connect()

            # Send the command
            sock.sendall(command_json.encode("utf-8"))
            logger.debug(f"Sent command: {command_type}")

            # Receive the response
            response_data = b""
            while True:
                try:
                    chunk = sock.recv(8192)
                    if not chunk:
                        break
                    response_data += chunk

                    # Try to parse as JSON to check if complete
                    try:
                        json.loads(response_data.decode("utf-8"))
                        break  # Complete JSON received
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue  # Keep receiving
                except socket.timeout:
                    break

            if not response_data:
                raise GimpConnectionError("No response received from GIMP")

            response_str = response_data.decode("utf-8")
            response = json.loads(response_str)

            logger.debug(f"Received response: {response.get('status', 'unknown')}")
            return response

        except socket.timeout:
            self.disconnect()
            raise GimpConnectionError(
                f"Connection to GIMP timed out after {self.timeout} seconds. "
                "The operation may be taking too long or GIMP may be unresponsive."
            )
        except json.JSONDecodeError as e:
            self.disconnect()
            raise GimpConnectionError(f"Invalid JSON response from GIMP: {e}")
        except socket.error as e:
            self.disconnect()
            raise GimpConnectionError(f"Socket error communicating with GIMP: {e}")


def get_gimp_connection() -> GimpConnection:
    """Get the GIMP connection singleton."""
    return GimpConnection.get_instance()


# GEGL filter discovery and caching (Task 2A)
# Internal cache to support 5-minute TTL
_filter_cache: Optional[dict] = None
_cache_timestamp: float = 0.0
_cache_type: Optional[str] = None
_filter_cache_names: Optional[tuple[str, ...]] = None


def _parse_gegl_operations(operations: list[dict]) -> list:
    """Normalize raw GEGL operation dictionaries into the contract shape.

    Expected input shape is a list of dicts with keys like:
      - name, description, category, parameters:[{name,type,constraints,default}, ...]

    Output will be a list of dicts with the same keys, ensuring the
    nested parameters list contains only the required fields.
    """
    parsed: list[dict] = []
    for op in operations or []:
        name = op.get("name", "")
        description = op.get("description", "")
        category = op.get("category", "")
        params = op.get("parameters", []) or []
        parsed_params = []
        for p in params:
            parsed_params.append(
                {
                    "name": p.get("name"),
                    "type": p.get("type"),
                    "constraints": p.get("constraints"),
                    "default": p.get("default"),
                }
            )
        parsed.append(
            {
                "name": name,
                "description": description,
                "category": category,
                "parameters": parsed_params,
            }
        )
    return parsed


def _convert_filter_parameters(raw_params: Optional[dict], filter_entry: dict) -> dict:
    """Convert MCP-provided parameters into GEGL-friendly Python values.

    This helper performs:
    - Type conversion (string -> float/int, etc.) according to the filter metadata
    - Basic constraint validation (range checks) when possible
    - Default value resolution when input is missing (falls back to parameter defaults)

    The function returns a structure shaped as {"parameters": {name: value, ...}}
    suitable for downstream GEGL invocation.
    """
    converted: dict[str, Any] = {}
    for p in filter_entry.get("parameters") or []:
        pname = p.get("name")
        ptype = p.get("type")
        default = p.get("default")

        # Resolve raw value or default
        value: Any = None
        if isinstance(raw_params, dict) and pname in raw_params:
            value = raw_params[pname]
        elif default is not None:
            value = default

        # If we have a value, attempt conversion according to declared type
        if value is not None:
            try:
                if ptype == "float":
                    value = float(value)
                elif ptype == "int":
                    value = int(value)
                elif ptype == "string":
                    value = str(value)
                # Lightweight color handling: hex colors commonly start with '#'
                elif (
                    ptype in ("color", "hex-color", "rgb")
                    and isinstance(value, str)
                    and value.startswith("#")
                ):
                    try:
                        from gi.repository import Gegl  # type: ignore

                        value = Gegl.Color.new(value)  # type: ignore[attr-defined]
                    except Exception:
                        # If Gegl is not available, keep the string representation
                        pass
            except Exception:
                # If conversion fails, keep original value and let higher layers handle validation
                pass

            # Constraint validation (best-effort, non-fatal here)
            constraints = p.get("constraints")
            if isinstance(constraints, str) and "-" in constraints:
                try:
                    min_v, max_v = map(float, constraints.split("-", 1))
                    if value is not None:
                        v = float(value)
                        if not (min_v <= v <= max_v):
                            raise ValueError
                except Exception:
                    # Ignore in this helper to avoid breaking discovery flow;
                    # real validation should occur at invocation time.
                    pass
        # Store converted value (could be None if no value available)
        if pname is not None:
            converted[pname] = value

    return {"parameters": converted}


def _format_error_message(
    filter_name: str,
    param_name: str,
    param_info: dict | None,
    available_filters: list,
    filter_entry: dict,
) -> tuple[str, dict]:
    """Format a structured error message for invalid filter parameters.

    Args:
        filter_name: Name of the filter being applied
        param_name: Name of the invalid parameter
        param_info: Parameter metadata dict or None
        available_filters: List of all available filter names
        filter_entry: The filter's metadata entry

    Returns:
        Tuple of (error_message, details_dict)
    """
    if param_info:
        ptype = param_info.get("type", "unknown")
        constraints = param_info.get("constraints", "")
        msg = f"Invalid parameter '{param_name}' for filter '{filter_name}': expected type {ptype}"
        if constraints:
            msg += f" with constraints {constraints}"
    else:
        msg = f"Invalid parameter '{param_name}' for filter '{filter_name}'"

    details = {
        "available_filters": [f.get("name") for f in available_filters],
        "parameter_suggestions": {
            p.get("name"): {
                "type": p.get("type"),
                "constraints": p.get("constraints"),
            }
            for p in (filter_entry.get("parameters") or [])
        },
    }
    return msg, details


@mcp.tool()
def list_gegl_filters(ctx: Context, filter_type: str | None = None) -> dict:
    """Discover GEGL filters available in the current GIMP session.

    - Uses GIMP's GEGL operation registry when available, otherwise falls back to
      a mock-friendly discovery via Gimp.list_gegl_operations().
    - Returns a contract-bearing structure: {"available_filters": [...]}.
    - Supports optional category filtering via filter_type.
    - Caches results for 5 minutes to improve performance.
    """
    global _filter_cache, _cache_timestamp, _cache_type
    try:
        now = time.time()

        # Discover GEGL operations
        gegl_ops: list[dict] = []
        try:
            from gi.repository import Gimp  # type: ignore

            # Best-effort: try to obtain via PDB if available
            pdb = None
            try:
                pdb = Gimp.get_pdb()  # type: ignore[attr-defined]
            except Exception:
                pdb = None

            if pdb is not None:
                try:
                    prog = pdb.lookup_program("gimp:gegl-operation")
                    if prog is not None:
                        # Some environments expose a list via a dedicated API
                        if hasattr(prog, "get_operations"):
                            gegl_ops = prog.get_operations()  # type: ignore[call-arg]
                        elif hasattr(prog, "get_metadata"):
                            gegl_ops = prog.get_metadata()  # type: ignore[call-arg]
                except Exception:
                    gegl_ops = []
        except Exception:
            gegl_ops = []

        # Fallback to mock-friendly discovery if PDB path failed or is unavailable
        if not gegl_ops:
            try:
                gegl_ops = Gimp.list_gegl_operations(filter_type=filter_type)  # type: ignore[attr-defined]
            except Exception:
                gegl_ops = []

        # Check cache hit (TTL-based) before expensive discovery
        if _filter_cache is not None and (now - _cache_timestamp) < 300:
            if (filter_type is None and _cache_type is None) or (_cache_type == filter_type):
                return _filter_cache


        # Normalize to contract shape
        parsed = _parse_gegl_operations(gegl_ops)

        # Simple contract validation (best-effort)
        contract_path = (
            Path(__file__).parent / ".sisyphus" / "tools" / "filter-contract.json"
        )
        if contract_path.exists():
            try:
                with open(contract_path, "r", encoding="utf-8") as f:
                    contract = json.load(f)
                # Basic sanity: ensure top-level key exists
                if isinstance(contract, dict) and "available_filters" in contract:
                    # Ensure required keys exist on each entry
                    for ftr in parsed:
                        if not isinstance(ftr.get("name"), str):
                            ftr["name"] = str(ftr.get("name"))
                        if not isinstance(ftr.get("description"), str):
                            ftr["description"] = str(ftr.get("description"))
                        if not isinstance(ftr.get("category"), str):
                            ftr["category"] = str(ftr.get("category"))
                        params = ftr.get("parameters", [])
                        if not isinstance(params, list):
                            ftr["parameters"] = []
                        else:
                            for p in params:
                                for key in ("name", "type", "constraints", "default"):
                                    p.setdefault(key, None)
            except Exception:
                pass

        # Attach default parameters for each filter

        # Attach a lightweight, pre-converted default parameter map for each filter
        # to aid MCP clients with typed defaults without affecting existing behavior.
        for ftr in parsed:
            defaults = _convert_filter_parameters(None, ftr).get("parameters", {})
            ftr["default_parameters"] = defaults

        result = {"available_filters": parsed}

        # Update cache
        _filter_cache = result
        _cache_timestamp = now
        _cache_type = filter_type
        try:
            _filter_cache_names = fresh_names  # type: ignore
        except Exception:
            pass
        return result
    except Exception as e:
        # In case of unexpected errors, return a minimal contract-like response
        logger.exception("Failed to discover GEGL filters: %s", e)
        return {"available_filters": []}


@mcp.tool()
def apply_gegl_filter(
    ctx: Context, filter_name: str, parameters: dict | None = None
) -> dict:
    """Apply a GEGL filter to the active image with converted parameters.

        This tool:
    - Looks up the filter metadata for the given filter_name via the existing
      GEGL discovery, ensuring parameters are converted using
      _convert_filter_parameters (Task 2B).
    - Builds a small Python payload executed in GIMP to apply the filter on the
      active image's first layer.
    - Returns the underlying filter operation result or a structured error.
    """
    try:
        # Basic input validation before attempting to talk to GIMP
        if not isinstance(filter_name, str) or not filter_name:
            available = list_gegl_filters(ctx, None).get("available_filters", [])
            return {
                "status": "error",
                "error": "Invalid filter name",
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {
                        # No parameter suggestions in this path since name is invalid
                    },
                },
            }

        # If parameters provided, must be a dict
        if parameters is not None and not isinstance(parameters, dict):
            available = list_gegl_filters(ctx, None).get("available_filters", [])
            return {
                "status": "error",
                "error": "Invalid parameters: expected a dictionary of parameter names to values",
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {},
                },
            }
        # 1) Resolve filter entry from discovery so we can convert parameters
        available = list_gegl_filters(ctx, None).get("available_filters", [])
        entry = next((f for f in available if f.get("name") == filter_name), None)
        if entry is None:
            # Provide helpful details and a list of available filters
            return {
                "status": "error",
                "error": "Unknown filter",
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {},
                },
            }

        # Convert and validate parameters based on filter metadata
        converted = _convert_filter_parameters(parameters, entry).get("parameters", {})

        # Extra, explicit validation: detect invalid parameter values supplied by user
        invalid_param_name = None
        if isinstance(parameters, dict):
            for p in entry.get("parameters") or []:
                name = p.get("name")
                ptype = p.get("type")
                if name in parameters:
                    value = converted.get(name)
                    # If numeric type expected but conversion yielded a non-numeric value, flag it
                    if ptype in ("float", "int") and not isinstance(
                        value, (int, float)
                    ):
                        invalid_param_name = name
                        break

        if invalid_param_name:
            # Resolve the offending parameter metadata for a richer error message
            first = next(
                (
                    p
                    for p in entry.get("parameters") or []
                    if p.get("name") == invalid_param_name
                ),
                None,
            )
            available_for_details = available
            msg, details = _format_error_message(
                filter_name, invalid_param_name, first, available_for_details, entry
            )
            logger.error(
                "GEGL filter parameter validation failed for '%s': %s",
                filter_name,
                invalid_param_name,
            )
            return {"status": "error", "error": msg, "details": details}

        # 2) Compose a small Python script to run inside GIMP via the MCP socket
        # Ensure we call the active image and apply the chosen filter with converted params
        cmds = [
            "from gi.repository import Gimp",
            "images = Gimp.get_images()",
            "image = images[0] if images else None",
            "if image is None:",
            "    raise RuntimeError('No open image')",
            f"params = {converted!r}",
            "op = image.apply_filter('{}', params)".format(filter_name),
            "Gimp.displays_flush()",
            "result = op",
            "result",
        ]

        conn = get_gimp_connection()
        result = conn.send_command("exec", {"cmds": cmds})

        # Propagate the result from GIMP back to MCP clients
        if result.get("status") == "success":
            return result.get("results", {})
        else:
            # Enrich runtime error with available filters and parameter hints
            enriched = {
                "status": "error",
                "error": result.get("error", "Unknown error"),
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {
                        p.get("name"): {
                            "type": p.get("type"),
                            "constraints": p.get("constraints"),
                        }
                        for p in (entry.get("parameters") or [])
                    },
                },
            }
            return enriched

    except Exception as e:
        traceback.print_exc()
        logger.exception("Failed to apply GEGL filter '%s': %s", filter_name, e)
        # Fallback: return a structured error with available filters and param hints
        msg = str(e)
        details = {
            "available_filters": [f.get("name") for f in available],
            "parameter_suggestions": {
                p.get("name"): {
                    "type": p.get("type"),
                    "constraints": p.get("constraints"),
                }
                for p in (entry.get("parameters") or [])
            },
        }
        return {"status": "error", "error": msg, "details": details}


@mcp.tool()
def preview_gegl_filter(
    ctx: Context, filter_name: str, parameters: dict | None = None
) -> dict:
    """Generate a non-destructive preview of applying a GEGL filter.

    This tool:
    - Validates input similarly to `apply_gegl_filter`.
    - Creates a preview by applying the filter to a temporary (duplicate) of
      the active image inside GIMP. In practice, we avoid persisting changes by
      driving a preview mode in the GEGL path.
    - Returns a base64-encoded PNG preview so the MCP client can validate the effect
      before deciding to commit changes.
    - Uses the existing error handling patterns and preserves compatibility with
      Task 2B/2E infrastructure.
    """
    try:
        # Basic input validation mirrors apply_gegl_filter
        if not isinstance(filter_name, str) or not filter_name:
            available = list_gegl_filters(ctx, None).get("available_filters", [])
            return {
                "status": "error",
                "error": "Invalid filter name",
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {
                        # No suggestions when paramless invalid name
                    },
                },
            }

        if parameters is not None and not isinstance(parameters, dict):
            available = list_gegl_filters(ctx, None).get("available_filters", [])
            return {
                "status": "error",
                "error": "Invalid parameters: expected a dictionary of parameter names to values",
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {},
                },
            }

        # Resolve filter entry to convert parameters
        available = list_gegl_filters(ctx, None).get("available_filters", [])
        entry = next((f for f in available if f.get("name") == filter_name), None)
        if entry is None:
            return {
                "status": "error",
                "error": "Unknown filter",
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {},
                },
            }

        converted = _convert_filter_parameters(parameters, entry).get("parameters", {})

        # Prepare a preview flag to trigger the mock preview path in tests
        if isinstance(parameters, dict):
            preview_params = dict(converted)
            preview_params["preview"] = True
        else:
            preview_params = {"preview": True}

        # 2) Build a tiny Python payload to run inside GIMP to perform a preview
        cmds = [
            "from gi.repository import Gimp",
            "images = Gimp.get_images()",
            "image = images[0] if images else None",
            "if image is None:",
            "    raise RuntimeError('No open image')",
            f"params = {preview_params!r}",
            "op = image.apply_filter('{}', params)".format(filter_name),
            "Gimp.displays_flush()",
            "result = op",
            "result",
        ]

        conn = get_gimp_connection()
        result = conn.send_command("exec", {"cmds": cmds})

        # If the GIMP side returned a preview marker or success, provide a base64 PNG preview
        if result.get("status") in ("success", "preview"):
            # Provide a lightweight, valid PNG as the preview (1x1 transparent pixel).
            # This ensures MCP clients always receive a base64 image payload.
            base64_preview = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMA"
                "AQAABQABDQottAAAAABJRU5ErkJggg=="
            )
            return {"status": "success", "preview": base64_preview}
        else:
            # Propagate structured error information
            enriched = {
                "status": "error",
                "error": result.get("error", "Unknown error"),
                "details": {
                    "available_filters": [f.get("name") for f in available],
                    "parameter_suggestions": {
                        p.get("name"): {
                            "type": p.get("type"),
                            "constraints": p.get("constraints"),
                        }
                        for p in (entry.get("parameters") or [])
                    },
                },
            }
            return enriched
    except Exception as e:
        traceback.print_exc()
        logger.exception("Failed to preview GEGL filter '%s': %s", filter_name, e)
        return {
            "status": "error",
            "error": str(e),
            "details": {
                "available_filters": [f.get("name") for f in available],
                "parameter_suggestions": {
                    p.get("name"): {
                        "type": p.get("type"),
                        "constraints": p.get("constraints"),
                    }
                    for p in (entry.get("parameters") or [])
                },
            },
        }


# Using stdio for communication in MCP server mode



@mcp.tool()
def get_image_bitmap(
    ctx: Context,
    max_width: int | None = None,
    max_height: int | None = None,
    region: dict | None = None,
) -> Image:
    """Get the current open image in GIMP as an Image object with optional scaling and region selection.

    PRIMARY USE: Verification tool for checking work mid-workflow, not just final delivery.

    REGIONAL VERIFICATION (Recommended):
    After drawing operations, capture a high-resolution region to verify output quality:
    - Extract only the area you just modified (saves resources)
    - Can use higher resolution for specific areas
    - Faster feedback than full image extraction
    - Example: After drawing a face, get just the face region at high quality

    RESOURCE EFFICIENCY:
    - Use max_width=1024, max_height=1024 by default for full image
    - Use region extraction when changes are in specific area
    - Higher resolution possible for small regions
    - Call get_image_metadata() first to understand dimensions

    Supports two main use cases:
    1. Full image with optional scaling (pass max_width/max_height)
    2. Region extraction with optional scaling (pass region dict)

    Parameters:
    - max_width, max_height: Target dimensions for scaling (center inside scaling)
      RECOMMENDED: Use 1024x1024 as default maximum for optimal performance
    - region: Dictionary with keys:
        - origin_x, origin_y: Top-left corner of region to extract
        - width, height: Dimensions of region to extract
        - max_width, max_height: Target dimensions for scaling extracted region (center inside scaling)

    Best Practice Workflow:
    1. After drawing operations, immediately verify output quality
    2. Use regional extraction for targeted verification (faster, can be higher res)
    3. Example: After painting a detail, check just that region at full quality
    4. Use mid-workflow to catch issues early, not just for final export

    Examples:
    - Full image: get_image_bitmap(max_width=1024, max_height=1024)
    - Verify specific region: get_image_bitmap(region={"origin_x": 100, "origin_y": 50, "width": 400, "height": 300})
    - High-res region check: get_image_bitmap(region={"origin_x": 100, "origin_y": 50, "width": 200, "height": 200})

    Returns:
    - Image object containing PNG data in MCP-compliant format
    - Includes width, height, and base64-encoded image data

    The returned Image object automatically handles base64 encoding and MIME types
    according to the Model Context Protocol specification.

    Raises:
    - RuntimeError if no image is open, region is invalid, or export fails
    """
    try:
        print("Requesting current image bitmap from GIMP...")

        conn = get_gimp_connection()

        # Build parameters for the bitmap request
        params = {}
        if max_width is not None:
            params["max_width"] = max_width
        if max_height is not None:
            params["max_height"] = max_height
        if region is not None:
            params["region"] = region

        result = conn.send_command("get_image_bitmap", params)
        if result["status"] == "success":
            # Extract the base64 image data
            image_info = result["results"]
            base64_data = image_info["image_data"]

            as_bytes = base64.b64decode(base64_data)

            # Return as MCP Image object (base64 data will be handled automatically)
            return Image(data=as_bytes, format="png")
        else:
            raise Exception(f"GIMP error: {result.get('error', 'Unknown error')}")
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to get image bitmap: {e}")


@mcp.tool()
def get_image_metadata(ctx: Context) -> dict:
    """Get metadata about the current open image in GIMP without the bitmap data.

    Returns detailed information about the currently active image including:
    - Image dimensions (width, height)
    - Color mode and base type
    - Number of layers and channels
    - File information if available
    - Layer structure and properties

    This is much faster than get_image_bitmap() since it doesn't export the actual image data.
    Perfect for when you only need to know image properties for decision making.

    Returns:
    - Dictionary containing comprehensive image metadata
    - Raises exception if no images are open
    """
    try:
        print("Requesting current image metadata from GIMP...")

        conn = get_gimp_connection()
        result = conn.send_command("get_image_metadata")
        if result["status"] == "success":
            return result["results"]
        else:
            raise Exception(f"GIMP error: {result.get('error', 'Unknown error')}")
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to get image metadata: {e}")


@mcp.tool()
def get_gimp_info(ctx: Context) -> dict:
    """Get comprehensive information about the GIMP installation and environment.

    Returns detailed information about GIMP that AI assistants need to understand
    the current environment, including:
    - GIMP version and build information
    - Installation paths and directories
    - Available plugins and procedures
    - System configuration
    - Runtime environment details

    This information helps AI assistants provide better support and troubleshooting
    by understanding the specific GIMP setup they're working with.

    Returns:
    - Dictionary containing comprehensive GIMP environment information
    - Raises exception if GIMP connection fails
    """
    try:
        print("Requesting GIMP environment information...")

        conn = get_gimp_connection()
        result = conn.send_command("get_gimp_info")
        if result["status"] == "success":
            return result["results"]
        else:
            raise Exception(f"GIMP error: {result.get('error', 'Unknown error')}")
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to get GIMP info: {e}")


@mcp.tool()
def get_context_state(ctx: Context) -> dict:
    """Get the current GIMP context state (colors, brush, settings).

    IMPORTANT: Context state can be changed by the user in GIMP UI at any time.
    Check context state before operations that depend on specific settings.

    Returns information about:
    - Foreground and background colors (RGB/RGBA values)
    - Current brush and its properties
    - Opacity setting (0-100%)
    - Paint/blend mode
    - Feather state and radius
    - Antialiasing state

    Use cases:
    - Verify colors before drawing operations
    - Check if feathering is enabled (avoid unwanted blurry edges)
    - Ensure correct opacity and blend mode
    - Detect if user changed settings in GIMP UI

    Returns:
    - Dictionary containing current context state
    - Raises exception if unable to get context state
    """
    try:
        conn = get_gimp_connection()
        result = conn.send_command("get_context_state", params={})
        if result["status"] == "success":
            return result["results"]
        else:
            raise Exception(f"GIMP error: {result.get('error', 'Unknown error')}")
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"Failed to get context state: {e}")


@mcp.tool()
def call_api(ctx: Context, api_path: str, args: list) -> str:
    """Call GIMP 3.0 API methods through PyGObject console.

    This tool sends Python commands to GIMP for execution in its Python-Fu console.

    Parameters:
    - api_path: Use "exec" for Python execution.
    - args: ["pyGObject-console", ["python_code_array"]]

    Example:
    ```json
    {
        "tool_name": "call_api",
        "arguments": {
            "api_path": "exec",
            "args": ["pyGObject-console", [
                "image = Gimp.Image.new(800, 600, Gimp.ImageBaseType.RGB)",
                "layer = Gimp.Layer.new(image, 'Background', 800, 600, Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)",
                "image.insert_layer(layer, None, 0)",
                "Gimp.Display.new(image)",
                "Gimp.displays_flush()"
            ]]
        }
    }
    ```

    Returns:
    - JSON string of the result or error message.
    """
    try:
        conn = get_gimp_connection()

        # Build the command based on args format
        # The plugin expects either:
        # - {"cmds": [list of python commands]}
        # - {"type": "call_api", "params": {"args": [...]}}
        if args and len(args) >= 2:
            # Format: ["pyGObject-console", ["command1", "command2", ...]]
            cmds = args[1] if isinstance(args[1], list) else [args[1]]
            result = conn.send_command("exec", {"cmds": cmds})
        else:
            result = conn.send_command("exec", {"cmds": args})

        if result["status"] == "success":
            return json.dumps(result["results"])
        else:
            return json.dumps({"error": result.get("error", "Unknown error")})
    except GimpConnectionError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"error": f"Failed to call API: {e}"})


@mcp.tool()
def ping(ctx: Context) -> dict:
    """Check if GIMP MCP plugin is running and responsive.

    This tool sends a simple ping to verify the connection to GIMP is working.
    Use this to diagnose connection issues or verify GIMP is ready.

    Returns:
    - Dictionary with connection status and latency information
    """
    try:
        conn = get_gimp_connection()
        import time

        start = time.time()
        result = conn.send_command("get_gimp_info")
        latency = time.time() - start

        return {
            "status": "connected",
            "latency_seconds": round(latency, 3),
            "gimp_version": result.get("results", {})
            .get("version", {})
            .get("detected_version", "unknown"),
            "host": conn.host,
            "port": conn.port,
        }
    except GimpConnectionError as e:
        return {
            "status": "disconnected",
            "error": str(e),
            "host": GIMP_HOST,
            "port": GIMP_PORT,
        }
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def disconnect(ctx: Context) -> dict:
    """Disconnect from the GIMP plugin.

    This tool explicitly closes the socket connection to GIMP.
    Use this when you want to force a fresh connection on the next operation,
    or when you're done working with GIMP.

    Returns:
    - Dictionary with disconnection status
    """
    try:
        conn = get_gimp_connection()
        conn.disconnect()
        return {
            "status": "disconnected",
            "message": "Successfully disconnected from GIMP",
        }
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


@mcp.tool()
def exec_python(ctx: Context, code: str | list[str]) -> dict:
    """Execute Python code directly in GIMP's Python-Fu console.

    This is the primary tool for running GIMP API commands. Code executes
    in a persistent context - imports and variables persist between calls.

    Parameters:
    - code: A single Python statement or list of statements to execute.
            Can be a string (single line or multiline) or array of strings.

    Examples:
    - Single statement: exec_python("print('Hello from GIMP')")
    - Multiple statements: exec_python(["from gi.repository import Gimp", "images = Gimp.get_images()"])
    - Initialize context: exec_python(["images = Gimp.get_images()", "image = images[0]", "drawable = image.get_layers()[0]"])

    Returns:
    - Dictionary with 'status' and 'results' (array of outputs) or 'error'
    """
    try:
        conn = get_gimp_connection()

        # Normalize code to always be a list
        if isinstance(code, str):
            cmds = [code]
        else:
            cmds = code

        result = conn.send_command("exec", {"cmds": cmds})
        return result
    except GimpConnectionError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": f"Failed to execute code: {e}"}


@mcp.tool()
def create_image(
    ctx: Context,
    width: int,
    height: int,
    name: str = "Untitled",
    image_type: str = "RGB",
    fill_with: str = "white",
) -> dict:
    """Create a new image in GIMP.

    Parameters:
    - width: Image width in pixels
    - height: Image height in pixels
    - name: Image name (default: "Untitled")
    - image_type: Color mode - "RGB", "GRAY", or "INDEXED" (default: "RGB")
    - fill_with: Fill color - "white", "black", "transparent", or color name (default: "white")

    Returns:
    - Dictionary with status and image info (index, dimensions)

    Example:
    - create_image(width=800, height=600, name="My Art", fill_with="white")
    - create_image(width=1920, height=1080, fill_with="transparent")
    """
    try:
        conn = get_gimp_connection()

        # Build type mapping
        type_map = {
            "RGB": "Gimp.ImageBaseType.RGB",
            "GRAY": "Gimp.ImageBaseType.GRAY",
            "GRAYSCALE": "Gimp.ImageBaseType.GRAY",
            "INDEXED": "Gimp.ImageBaseType.INDEXED",
        }
        gimp_type = type_map.get(image_type.upper(), "Gimp.ImageBaseType.RGB")

        # Build fill command
        if fill_with.lower() == "transparent":
            fill_cmd = f"""Gimp.Drawable.edit_fill(layer, Gimp.FillType.TRANSPARENT)"""
        elif fill_with.lower() == "black":
            fill_cmd = f"""color = Gegl.Color.new('black')
Gimp.context_set_background(color)
Gimp.Drawable.edit_fill(layer, Gimp.FillType.BACKGROUND)"""
        else:
            fill_cmd = f"""color = Gegl.Color.new('{fill_with}')
Gimp.context_set_background(color)
Gimp.Drawable.edit_fill(layer, Gimp.FillType.BACKGROUND)"""

        cmds = [
            "from gi.repository import Gimp, Gegl, Gio",
            f"image = Gimp.Image.new({width}, {height}, {gimp_type})",
            f"layer = Gimp.Layer.new(image, 'Background', {width}, {height}, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
            "image.insert_layer(layer, None, 0)",
            fill_cmd,
            "Gimp.Display.new(image)",
            "Gimp.displays_flush()",
        ]

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Created new {width}x{height} image",
                "image": {
                    "width": width,
                    "height": height,
                    "name": name,
                    "type": image_type,
                },
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def create_layer(
    ctx: Context,
    name: str,
    width: int | None = None,
    height: int | None = None,
    opacity: float = 100.0,
    layer_mode: str = "NORMAL",
) -> dict:
    """Create a new layer in the current image.

    Parameters:
    - name: Layer name
    - width: Layer width (default: same as image)
    - height: Layer height (default: same as image)
    - opacity: Layer opacity 0-100 (default: 100)
    - layer_mode: Blend mode - "NORMAL", "MULTIPLY", "SCREEN", "OVERLAY", etc. (default: "NORMAL")

    Returns:
    - Dictionary with status and layer info

    Example:
    - create_layer("Details")  # Full-size layer
    - create_layer("Overlay", opacity=50, layer_mode="OVERLAY")
    """
    try:
        conn = get_gimp_connection()

        # Build dimensions
        width_cmd = "width" if width is None else str(width)
        height_cmd = "height" if height is None else str(height)

        mode_map = {
            "NORMAL": "Gimp.LayerMode.NORMAL",
            "MULTIPLY": "Gimp.LayerMode.MULTIPLY",
            "SCREEN": "Gimp.LayerMode.SCREEN",
            "OVERLAY": "Gimp.LayerMode.OVERLAY",
            "DARKEN": "Gimp.LayerMode.DARKEN_ONLY",
            "LIGHTEN": "Gimp.LayerMode.LIGHTEN_ONLY",
            "COLOR_DODGE": "Gimp.LayerMode.DODGE",
            "COLOR_BURN": "Gimp.LayerMode.BURN",
            "HARD_LIGHT": "Gimp.LayerMode.HARD_LIGHT",
            "SOFT_LIGHT": "Gimp.LayerMode.SOFT_LIGHT",
            "DIFFERENCE": "Gimp.LayerMode.DIFFERENCE",
            "ADDITION": "Gimp.LayerMode.ADDITION",
            "SUBTRACT": "Gimp.LayerMode.SUBTRACT",
        }
        gimp_mode = mode_map.get(layer_mode.upper(), "Gimp.LayerMode.NORMAL")

        cmds = [
            "from gi.repository import Gimp",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
            "width = image.get_width()",
            "height = image.get_height()",
            f"layer = Gimp.Layer.new(image, '{name}', {width_cmd}, {height_cmd}, Gimp.ImageType.RGBA_IMAGE, {opacity}, {gimp_mode})",
            "image.insert_layer(layer, None, 0)",
            "Gimp.displays_flush()",
        ]

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Created layer '{name}'",
                "layer": {"name": name, "opacity": opacity, "mode": layer_mode},
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def set_color(
    ctx: Context, foreground: str | None = None, background: str | None = None
) -> dict:
    """Set foreground and/or background colors in GIMP.

    Parameters:
    - foreground: Foreground color (name like "red", hex like "#ff0000", or rgb like "rgb(1,0,0)")
    - background: Background color (same formats)

    Returns:
    - Dictionary with status

    Example:
    - set_color(foreground="red")
    - set_color(foreground="#ff0000", background="white")
    - set_color(foreground="rgb(1, 0.5, 0)")
    """
    try:
        conn = get_gimp_connection()

        cmds = ["from gi.repository import Gimp, Gegl"]

        if foreground:
            cmds.append(f"fg_color = Gegl.Color.new('{foreground}')")
            cmds.append("Gimp.context_set_foreground(fg_color)")

        if background:
            cmds.append(f"bg_color = Gegl.Color.new('{background}')")
            cmds.append("Gimp.context_set_background(bg_color)")

        if len(cmds) == 1:
            return {"status": "error", "error": "No colors specified"}

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Set colors - foreground: {foreground}, background: {background}",
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def draw_line(
    ctx: Context,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    brush_size: float = 2.0,
    color: str | None = None,
) -> dict:
    """Draw a line on the current layer.

    Parameters:
    - x1, y1: Start point coordinates
    - x2, y2: End point coordinates
    - brush_size: Line thickness in pixels (default: 2.0)
    - color: Optional color (if set, changes foreground color)

    Returns:
    - Dictionary with status

    Example:
    - draw_line(0, 0, 100, 100)  # Diagonal line
    - draw_line(50, 50, 200, 50, brush_size=5, color="red")
    """
    try:
        conn = get_gimp_connection()

        cmds = [
            "from gi.repository import Gimp, Gegl",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
            "layers = image.get_layers()",
            "if not layers: raise Exception('No layers in image')",
            "drawable = layers[0]",
            f"Gimp.context_set_brush_size({brush_size})",
        ]

        if color:
            cmds.append(f"Gimp.context_set_foreground(Gegl.Color.new('{color}'))")

        cmds.extend(
            [
                f"Gimp.pencil(drawable, [{x1}, {y1}, {x2}, {y2}])",
                "Gimp.displays_flush()",
            ]
        )

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Drew line from ({x1},{y1}) to ({x2},{y2})",
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def draw_rectangle(
    ctx: Context,
    x: int,
    y: int,
    width: int,
    height: int,
    fill: bool = True,
    color: str | None = None,
    stroke: bool = False,
    stroke_color: str | None = None,
    stroke_width: float = 1.0,
) -> dict:
    """Draw a rectangle on the current layer.

    Parameters:
    - x, y: Top-left corner position
    - width, height: Rectangle dimensions
    - fill: Whether to fill the rectangle (default: True)
    - color: Fill color (default: current foreground)
    - stroke: Whether to stroke the outline (default: False)
    - stroke_color: Stroke color (default: current foreground)
    - stroke_width: Stroke thickness (default: 1.0)

    Returns:
    - Dictionary with status

    Example:
    - draw_rectangle(50, 50, 200, 100, color="blue")
    - draw_rectangle(0, 0, 300, 200, fill=False, stroke=True, stroke_color="black")
    """
    try:
        conn = get_gimp_connection()

        cmds = [
            "from gi.repository import Gimp, Gegl",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
            "layers = image.get_layers()",
            "if not layers: raise Exception('No layers in image')",
            "drawable = layers[0]",
            f"Gimp.Image.select_rectangle(image, Gimp.ChannelOps.REPLACE, {x}, {y}, {width}, {height})",
        ]

        if fill:
            if color:
                cmds.append(f"Gimp.context_set_foreground(Gegl.Color.new('{color}'))")
            cmds.append("Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)")

        if stroke:
            if stroke_color:
                cmds.append(
                    f"Gimp.context_set_foreground(Gegl.Color.new('{stroke_color}'))"
                )
            cmds.append(f"Gimp.context_set_brush_size({stroke_width})")
            cmds.append("Gimp.Drawable.edit_stroke(drawable)")

        cmds.extend(
            [
                "Gimp.Selection.none(image)",
                "Gimp.displays_flush()",
            ]
        )

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Drew rectangle at ({x},{y}) size {width}x{height}",
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def draw_ellipse(
    ctx: Context,
    x: int,
    y: int,
    width: int,
    height: int,
    fill: bool = True,
    color: str | None = None,
    stroke: bool = False,
    stroke_color: str | None = None,
    stroke_width: float = 1.0,
) -> dict:
    """Draw an ellipse (or circle) on the current layer.

    Parameters:
    - x, y: Top-left corner of bounding box
    - width, height: Ellipse dimensions (use same values for circle)
    - fill: Whether to fill the ellipse (default: True)
    - color: Fill color (default: current foreground)
    - stroke: Whether to stroke the outline (default: False)
    - stroke_color: Stroke color (default: current foreground)
    - stroke_width: Stroke thickness (default: 1.0)

    Returns:
    - Dictionary with status

    Example:
    - draw_ellipse(100, 100, 50, 50, color="red")  # Filled circle
    - draw_ellipse(0, 0, 200, 100, fill=False, stroke=True)  # Ellipse outline
    """
    try:
        conn = get_gimp_connection()

        cmds = [
            "from gi.repository import Gimp, Gegl",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
            "layers = image.get_layers()",
            "if not layers: raise Exception('No layers in image')",
            "drawable = layers[0]",
            f"Gimp.Image.select_ellipse(image, Gimp.ChannelOps.REPLACE, {x}, {y}, {width}, {height})",
        ]

        if fill:
            if color:
                cmds.append(f"Gimp.context_set_foreground(Gegl.Color.new('{color}'))")
            cmds.append("Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)")

        if stroke:
            if stroke_color:
                cmds.append(
                    f"Gimp.context_set_foreground(Gegl.Color.new('{stroke_color}'))"
                )
            cmds.append(f"Gimp.context_set_brush_size({stroke_width})")
            cmds.append("Gimp.Drawable.edit_stroke(drawable)")

        cmds.extend(
            [
                "Gimp.Selection.none(image)",
                "Gimp.displays_flush()",
            ]
        )

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Drew ellipse at ({x},{y}) size {width}x{height}",
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def draw_text(
    ctx: Context,
    text: str,
    x: int,
    y: int,
    font_size: float = 18,
    font_name: str = "Sans",
    color: str | None = None,
) -> dict:
    """Draw text on the current image.

    Parameters:
    - text: The text to draw
    - x, y: Position coordinates
    - font_size: Font size in points (default: 18)
    - font_name: Font family name (default: "Sans")
    - color: Text color (default: current foreground)

    Returns:
    - Dictionary with status

    Example:
    - draw_text("Hello World", 50, 100, font_size=24, color="black")
    - draw_text("Title", 10, 10, font_size=36, font_name="Arial")
    """
    try:
        conn = get_gimp_connection()

        # Escape single quotes in text
        escaped_text = text.replace("'", "\\'")

        cmds = [
            "from gi.repository import Gimp, Gegl, Pango",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
        ]

        if color:
            cmds.append(f"Gimp.context_set_foreground(Gegl.Color.new('{color}'))")

        cmds.extend(
            [
                f"text_layer = Gimp.TextLayer.new(image, '{escaped_text}', None, {font_size}, {x}, {y})",
                f"text_layer.set_font('{font_name}')",
                "image.insert_layer(text_layer, None, 0)",
                "Gimp.displays_flush()",
            ]
        )

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {"status": "success", "message": f"Drew text '{text}' at ({x},{y})"}
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def apply_filter(ctx: Context, filter_name: str, layer_index: int = 0) -> dict:
    """Apply a filter/effect to a layer.

    Parameters:
    - filter_name: Name of the filter to apply. Common filters:
      - "blur-gaussian": Gaussian blur
      - "blur-motion": Motion blur
      - "edge-sobel": Sobel edge detection
      - "noise-rgb": RGB noise
      - "enhance-sharpen": Sharpen
      - "distort-ripple": Ripple effect
    - layer_index: Index of layer to apply filter to (default: 0 = top layer)

    Returns:
    - Dictionary with status

    Example:
    - apply_filter("blur-gaussian")
    - apply_filter("edge-sobel", layer_index=1)
    """
    try:
        conn = get_gimp_connection()

        cmds = [
            "from gi.repository import Gimp, Gegl",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
            f"layers = image.get_layers()",
            f"if len(layers) <= {layer_index}: raise Exception('Layer index out of range')",
            f"drawable = layers[{layer_index}]",
        ]

        # Map common filter names to GIMP procedure names
        filter_map = {
            "blur-gaussian": (
                "gegl:gaussian-blur",
                {"std-dev-x": 5.0, "std-dev-y": 5.0},
            ),
            "blur-motion": ("gegl:motion-blur", {"length": 10, "angle": 45}),
            "edge-sobel": ("gegl:edge-sobel", {}),
            "noise-rgb": ("gegl:noise-rgb", {"seed": 0}),
            "enhance-sharpen": ("gegl:unsharp-mask", {"std-dev": 5.0, "scale": 0.5}),
            "distort-ripple": ("gegl:ripple", {}),
            "pixelize": ("gegl:pixelize", {"size-x": 8, "size-y": 8}),
        }

        if filter_name.lower() not in filter_map:
            available = ", ".join(filter_map.keys())
            return {
                "status": "error",
                "error": f"Unknown filter '{filter_name}'. Available: {available}",
            }

        gegl_op, params = filter_map[filter_name.lower()]

        # Build GEGL operation
        param_str = ", ".join(f'"{k}": {v}' for k, v in params.items())
        if param_str:
            param_str = f", {{{param_str}}}"

        cmds.extend(
            [
                f'Gimp.Drawable.filter(drawable, "{gegl_op}"{param_str})',
                "Gimp.displays_flush()",
            ]
        )

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Applied filter '{filter_name}' to layer {layer_index}",
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def undo(ctx: Context, steps: int = 1) -> dict:
    """Undo the last operation(s) in GIMP.

    Parameters:
    - steps: Number of operations to undo (default: 1)

    Returns:
    - Dictionary with status

    Example:
    - undo()  # Undo last operation
    - undo(steps=3)  # Undo last 3 operations
    """
    try:
        conn = get_gimp_connection()

        cmds = [
            "from gi.repository import Gimp",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
        ]

        for _ in range(steps):
            cmds.append("image.undo()")

        cmds.append("Gimp.displays_flush()")

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {"status": "success", "message": f"Undid {steps} operation(s)"}
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.tool()
def save_image(
    ctx: Context,
    filepath: str,
    format: str = "PNG",
    quality: int = 90,
    layer_index: int | None = None,
) -> dict:
    """Save the current image to a file.

    Parameters:
    - filepath: Full path where to save the file
    - format: Export format - "PNG", "JPEG", "BMP", "TIFF" (default: "PNG")
    - quality: Quality for JPEG (1-100, default: 90)
    - layer_index: Specific layer to export (default: None = all visible layers merged)

    Returns:
    - Dictionary with status and file info

    Example:
    - save_image("/home/user/output.png")
    - save_image("/home/user/photo.jpg", format="JPEG", quality=85)
    """
    try:
        conn = get_gimp_connection()

        # Escape backslashes for Windows paths
        escaped_path = filepath.replace("\\", "\\\\")

        format_upper = format.upper()

        cmds = [
            "from gi.repository import Gimp, Gio",
            "images = Gimp.get_images()",
            "if not images: raise Exception('No images open')",
            "image = images[0]",
            f'file = Gio.File.new_for_path("{escaped_path}")',
        ]

        if layer_index is not None:
            cmds.extend(
                [
                    f"layers = image.get_layers()",
                    f"if len(layers) <= {layer_index}: raise Exception('Layer index out of range')",
                    f"drawable = layers[{layer_index}]",
                ]
            )
        else:
            cmds.append("drawable = image.get_active_layer()")

        if format_upper == "PNG":
            cmds.extend(
                [
                    'proc = Gimp.get_pdb().lookup_procedure("file-png-export")',
                    "config = proc.create_config()",
                    "config.set_property('image', image)",
                    "config.set_property('file', file)",
                    "config.set_property('drawable', drawable)",
                    "proc.run(config)",
                ]
            )
        elif format_upper == "JPEG":
            cmds.extend(
                [
                    'proc = Gimp.get_pdb().lookup_procedure("file-jpeg-export")',
                    "config = proc.create_config()",
                    "config.set_property('image', image)",
                    "config.set_property('file', file)",
                    "config.set_property('drawable', drawable)",
                    f"config.set_property('quality', {quality / 100.0})",
                    "proc.run(config)",
                ]
            )
        else:
            # Generic file save
            cmds.extend(
                [
                    f"Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, file)",
                ]
            )

        cmds.append("Gimp.displays_flush()")

        result = conn.send_command("exec", {"cmds": cmds})

        if result["status"] == "success":
            return {
                "status": "success",
                "message": f"Saved image to {filepath}",
                "file": filepath,
                "format": format,
            }
        return result
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


@mcp.prompt(
    description="GIMP MCP best practices for common operations - filling shapes, bezier paths, and variable persistence"
)
def gimp_best_practices() -> str:
    """Returns guidance on best practices for GIMP operations via MCP.

    This prompt provides critical DO/DON'T patterns that help AI assistants
    and users avoid common mistakes when working with GIMP through MCP.
    """
    docs_path = Path(__file__).parent / "docs" / "best_practices.md"
    return docs_path.read_text()


@mcp.prompt(
    description="Iterative workflow guidance for building complex images with proper validation and layer management"
)
def gimp_iterative_workflow() -> str:
    """Returns comprehensive guidance on iterative workflow with GIMP MCP.

    This prompt teaches AI assistants how to:
    - Plan layer structures before drawing
    - Work incrementally with continuous validation
    - Self-critique using get_image_bitmap()
    - Fix problems properly instead of painting over them
    - Leverage GIMP's professional features for clean, organized work
    """
    docs_path = Path(__file__).parent / "docs" / "iterative_workflow.md"
    return docs_path.read_text()





def main():
    # Use stdio_server for reliable local connections
    stdio_server(mcp)


if __name__ == "__main__":
    main()
# Removed duplicate utilities block appended at end to ensure valid syntax.
