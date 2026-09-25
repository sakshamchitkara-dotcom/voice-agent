"""Create/update the saved Vapi assistant and attach an existing phone number.

  python -m scripts.vapi_setup assistant [--dry-run]
  python -m scripts.vapi_setup phone --list
  python -m scripts.vapi_setup phone [--number-id ID] [--mode dynamic|static] [--dry-run]

Needs VAPI_API_KEY (private key) unless --dry-run. If VAPI_ASSISTANT_ID is set the assistant is
updated in place (PATCH /assistant/{id}); otherwise a new one is created (POST /assistant).

Phone modes (PATCH /phone-number/{id}); this never buys a number:
  dynamic  number's server URL points at our webhook, which answers `assistant-request`
           with a per-caller assistant (allowlisted callers get the agentic tools)
  static   number is bound to VAPI_ASSISTANT_ID
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from app.assistant import build_assistant, server_block
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


def cmd_phone(args) -> None:
    if args.list:
        for n in vapi("GET", "/phone-number"):
            print(f"{n['id']}  {n.get('number') or n.get('sipUri', '')}  "
                  f"provider={n.get('provider')}  assistantId={n.get('assistantId')}")
        return
    number_id = args.number_id or os.getenv("VAPI_PHONE_NUMBER_ID")
    if not number_id and not args.dry_run:
        sys.exit("Pass --number-id or set VAPI_PHONE_NUMBER_ID (see `phone --list`).")
    if args.mode == "dynamic":
        payload = {"assistantId": None, "squadId": None, "server": server_block(get_settings())}
    else:
        assistant_id = os.getenv("VAPI_ASSISTANT_ID")
        if not assistant_id:
            sys.exit("static mode needs VAPI_ASSISTANT_ID (run the `assistant` command first).")
        payload = {"assistantId": assistant_id}
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return
    out = vapi("PATCH", f"/phone-number/{number_id}", payload)
    print(f"Phone number {out.get('number') or out['id']} now in {args.mode} mode")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assistant", help="create or update the saved assistant")
    a.add_argument("--name", default="Voice Agent")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_assistant)
    ph = sub.add_parser("phone", help="attach an existing Vapi phone number")
    ph.add_argument("--list", action="store_true", help="list phone numbers and exit")
    ph.add_argument("--number-id")
    ph.add_argument("--mode", choices=["dynamic", "static"], default="dynamic")
    ph.add_argument("--dry-run", action="store_true")
    ph.set_defaults(fn=cmd_phone)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
