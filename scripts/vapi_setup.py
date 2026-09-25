"""Create/update the saved Vapi assistant.

  python -m scripts.vapi_setup assistant [--dry-run]

Needs VAPI_API_KEY (private key) unless --dry-run. If VAPI_ASSISTANT_ID is set the assistant is
updated in place (PATCH /assistant/{id}); otherwise a new one is created (POST /assistant).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from app.assistant import build_assistant
from app.config import get_settings

API = "https://api.vapi.ai"


def vapi(method: str, path: str, body: dict | None = None) -> dict:
    key = os.getenv("VAPI_API_KEY")
    if not key:
        sys.exit("VAPI_API_KEY is not set (use --dry-run to just print the payload).")
    r = httpx.request(method, f"{API}{path}", json=body, timeout=30,
                      headers={"Authorization": f"Bearer {key}"})
    if r.is_error:
        sys.exit(f"Vapi {method} {path} failed: {r.status_code} {r.text}")
    return r.json()


def cmd_assistant(args) -> None:
    s = get_settings()
    # The saved assistant carries every tool; the server still enforces the allowlist per call.
    payload = build_assistant(s, trusted=True, name=args.name)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return
    assistant_id = os.getenv("VAPI_ASSISTANT_ID")
    if assistant_id:
        out = vapi("PATCH", f"/assistant/{assistant_id}", payload)
        print(f"Updated assistant {out['id']}")
    else:
        out = vapi("POST", "/assistant", payload)
        print(f"Created assistant {out['id']}\nAdd VAPI_ASSISTANT_ID={out['id']} to your .env")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assistant", help="create or update the saved assistant")
    a.add_argument("--name", default="Voice Agent")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_assistant)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
