import socket
import json


def test_list_filters():
    """Helper: Fetch available GEGL filters dynamically."""
    host = "127.0.0.1"
    port = 9878  # MCP server port
    payload = {
        "tool_name": "call_api",
        "arguments": {
            "api_path": "list_gegl_filters",
            "args": [],
        },
    }
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            resp = s.recv(4096)
            # Attempt to parse response as JSON; be resilient to partial reads
            data = None
            try:
                data = json.loads(resp.decode("utf-8", errors="replace"))
            except Exception:
                more = s.recv(4096)
                if more:
                    try:
                        data = json.loads(
                            (resp + more).decode("utf-8", errors="replace")
                        )
                    except Exception:
                        data = None
            if data is None:
                print("ERROR: Unable to parse response from list_gegl_filters")
                return []
            # Normalize into a list of dicts with 'name'
            filters = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item:
                        filters.append(item)
                    elif isinstance(item, str):
                        filters.append({"name": item})
            elif isinstance(data, dict):
                # Common keys that may contain a list
                candidate_lists = []
                for key in (
                    "results",
                    "filters",
                    "gegl_filters",
                    "items",
                    "filters_list",
                ):
                    if key in data and isinstance(data[key], list):
                        candidate_lists.append(data[key])
                if candidate_lists:
                    for item in candidate_lists[0]:
                        if isinstance(item, dict) and "name" in item:
                            filters.append(item)
                        elif isinstance(item, str):
                            filters.append({"name": item})
                elif "name" in data:
                    val = data["name"]
                    if isinstance(val, str):
                        filters.append({"name": val})
            return filters
    except Exception as e:
        print("ERROR", e)
        return []


def test_apply_filter():
    """Test apply_gegl_filter with the first discovered filter."""
    filters = test_list_filters()
    if not filters:
        print("No filters available for apply_gegl_filter test")
        return
    filter_name = filters[0]["name"] if isinstance(filters[0], dict) else filters[0]
    payload = {
        "tool_name": "call_api",
        "arguments": {
            "api_path": "apply_gegl_filter",
            "args": [filter_name, {"std_dev_x": 5.0}],
        },
    }
    host = "127.0.0.1"
    port = 9878
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            resp = s.recv(4096)
            print("Apply filter response:", resp.decode("utf-8", errors="replace"))
    except Exception as e:
        print("ERROR", e)


def test_preview_filter():
    """Test preview_gegl_filter with the first discovered filter."""
    filters = test_list_filters()
    if not filters:
        print("No filters available for preview_gegl_filter test")
        return
    filter_name = filters[0]["name"] if isinstance(filters[0], dict) else filters[0]
    payload = {
        "tool_name": "call_api",
        "arguments": {
            "api_path": "preview_gegl_filter",
            "args": [filter_name, {"std_dev_x": 5.0}],
        },
    }
    host = "127.0.0.1"
    port = 9878
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            resp = s.recv(4096)
            print("Preview filter response:", resp.decode("utf-8", errors="replace"))
    except Exception as e:
        print("ERROR", e)


if __name__ == "__main__":
    test_list_filters()
    test_apply_filter()
    test_preview_filter()
