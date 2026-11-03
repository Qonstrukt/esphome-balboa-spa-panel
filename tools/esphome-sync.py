#!/usr/bin/env python3
"""Sync a device config with the ESPHome Device Builder through its MCP endpoint.

The builder's MCP server (/api/mcp) is plain JSON-RPC over HTTP, so its tools can
be called with a POST and the file read from disk, instead of pasting the YAML.

Usage (run from a project folder, e.g. ~/ESPHome/jacuzzi):
  ../esphome-sync.py pull [file.yaml]      print the builder's copy (diff it: pull | diff - file.yaml)
  ../esphome-sync.py push [file.yaml]      upload the local file, then validate it in the builder
  ../esphome-sync.py validate [file.yaml]  validate the builder's copy
  ../esphome-sync.py install [file.yaml]   compile and install over the air, following the jobs
  ../esphome-sync.py job <job_id>          show a firmware job's status and last output lines

The builder URL comes from ESPHOME_URL, or from ~/.esphome-sync-url (one line),
e.g. http://homeassistant.local:6052 for the add-on with its port exposed.
When file.yaml is omitted, the one .yaml in the current folder with a top-level
`esphome:` section is used. The builder's copy is addressed by
the file's base name.
"""
import json
import os
import sys
import time
import urllib.request

# Builder URL: ESPHOME_URL in the environment, else the first line of ~/.esphome-sync-url
def builder_url():
    url = os.environ.get("ESPHOME_URL")
    if not url:
        try:
            url = open(os.path.expanduser("~/.esphome-sync-url")).readline().strip()
        except OSError:
            sys.exit("set ESPHOME_URL or write the builder URL to ~/.esphome-sync-url")
    return url.rstrip("/")


BASE = builder_url()
MCP = BASE + "/api/mcp"


def default_file():
    """The single device YAML (one with a top-level `esphome:` section) in the current folder."""
    names = []
    for f in sorted(os.listdir(".")):
        if f.endswith((".yaml", ".yml")) and f != "secrets.yaml":
            text = open(f, encoding="utf-8", errors="replace").read()
            if text.startswith("esphome:") or "\nesphome:" in text:
                names.append(f)
    if len(names) != 1:
        sys.exit("name the device YAML: found " + (", ".join(names) if names else "no device .yaml here"))
    return names[0]


def call(tool, **args):
    """Call one MCP tool and return its parsed result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(MCP, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        reply = json.loads(r.read())
    if "error" in reply:
        sys.exit(f"{tool}: {reply['error']}")
    result = reply["result"]
    if result.get("isError"):
        sys.exit(f"{tool} failed: {text_of(result)}")
    if "structuredContent" in result:
        return result["structuredContent"]
    text = text_of(result)
    try:
        return json.loads(text)
    except ValueError:
        return text


def text_of(result):
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def as_text(value):
    """get_config returns the YAML as text, possibly wrapped in a dict."""
    if isinstance(value, dict):
        for k in ("content", "config", "text"):
            if k in value:
                return value[k]
    return value if isinstance(value, str) else json.dumps(value)


def show_job(job_id):
    job = call("get_job", job_id=job_id, tail_lines=15)
    status = job.get("status") if isinstance(job, dict) else None
    print(json.dumps(job, indent=1) if not isinstance(job, dict) else
          f"{job_id}: {status} exit={job.get('exit_code')}\n" + "\n".join(job.get("output", job.get("lines", []))[-15:]))
    return job


def follow(job_id, label):
    last = None
    while True:
        job = call("get_job", job_id=job_id, tail_lines=3)
        status = job.get("status") if isinstance(job, dict) else None
        if status != last:
            print(f"{label}: {status}", file=sys.stderr)
            last = status
        if status in ("completed", "finished", "done", "success", "failed", "error", "cancelled"):
            tail = job.get("output", job.get("lines", []))
            print("\n".join(tail[-3:]) if isinstance(tail, list) else tail)
            return job
        time.sleep(5)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("pull", "push", "validate", "install", "job"):
        sys.exit(__doc__)
    action = sys.argv[1]
    if action == "job":
        show_job(sys.argv[2])
        return
    path = sys.argv[2] if len(sys.argv) > 2 else default_file()
    name = os.path.basename(path)

    if action == "pull":
        sys.stdout.write(as_text(call("get_config", configuration=name)))
        return

    if action == "push":
        current = as_text(call("get_config", configuration=name))
        content = open(path, encoding="utf-8").read()
        if current == content:
            print(f"{name}: builder copy already identical", file=sys.stderr)
        else:
            call("update_config", configuration=name, content=content, expected=current)
            print(f"{name}: uploaded {len(content)} bytes", file=sys.stderr)
        action = "validate"

    if action == "validate":
        res = call("validate_config", configuration=name, tail_lines=20)
        text = as_text(res) if not isinstance(res, dict) else json.dumps(res, indent=1)
        print(text)
        if "Configuration is valid" not in text:
            sys.exit(1)
        return

    if action == "install":
        res = call("install", configuration=name)
        print(json.dumps(res), file=sys.stderr)
        compile_job = follow(res["job_id"], "compile")
        if compile_job.get("exit_code") not in (0, None):
            sys.exit("compile failed")
        if res.get("upload_job_id"):
            follow(res["upload_job_id"], "upload")


if __name__ == "__main__":
    main()
