# Claude Desktop MCP client test adjusted to MCP protocol (type + cmds framing)
import socket
import json


def test_exec():
    host = "127.0.0.1"
    port = 9878
    # Protocol: MCP tool invocation for call_api
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
            print("Response:", resp.decode("utf-8", errors="replace"))
    except Exception as e:
        print("ERROR", e)


if __name__ == "__main__":
    test_exec()
