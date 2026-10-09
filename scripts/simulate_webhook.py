#!/usr/bin/env python3
"""CLI utility to simulate Meta webhook POST deliveries with HMAC-SHA256 signatures."""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import uuid


def parse_args() -> argparse.Namespace:
    """Parse command line flags."""
    parser = argparse.ArgumentParser(
        description="Simulate signed Meta webhook events against the running server."
    )
    parser.add_argument(
        "--platform",
        choices=["instagram", "facebook"],
        default="instagram",
        help="Target platform (default: instagram)",
    )
    parser.add_argument(
        "--event-type",
        choices=["comment", "message"],
        default="comment",
        help="Type of event to simulate (default: comment)",
    )
    parser.add_argument(
        "--text",
        default="How much does this cost?",
        help="Comment text or message body",
    )
    parser.add_argument(
        "--commenter-id",
        default="999888777111",
        help="Customer or commenter external ID",
    )
    parser.add_argument(
        "--comment-id",
        default=None,
        help="Comment ID or message MID (default: randomly generated)",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000/webhook",
        help="Server webhook endpoint URL (default: http://localhost:8000/webhook)",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="Meta App Secret for HMAC-SHA256 signature (reads APP_SECRET env var if omitted)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of times to send the same event (default: 1; use >1 to test duplicate delivery)",
    )
    parser.add_argument(
        "--bad-signature",
        action="store_true",
        help="Deliberately send an invalid HMAC signature to test security rejection",
    )
    return parser.parse_args()


def get_app_secret(arg_secret: str | None) -> str:
    """Resolve Meta App Secret from CLI argument, environment, or .env file."""
    if arg_secret:
        return arg_secret

    env_val = os.environ.get("APP_SECRET")
    if env_val:
        return env_val

    # Try reading .env file
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                if line.startswith("APP_SECRET="):
                    return line.strip().split("=", 1)[1]

    # Fallback to test secret from .env.example / docker-compose default
    return "mock_app_secret_value_for_testing_purposes"


def build_payload(
    platform: str,
    event_type: str,
    text: str,
    commenter_id: str,
    comment_id: str,
) -> dict[str, object]:
    """Construct Meta webhook JSON payload matching official Graph API schema."""
    now_epoch = int(time.time())

    if platform == "instagram":
        if event_type == "comment":
            return {
                "object": "instagram",
                "entry": [
                    {
                        "id": "17841400000000001",
                        "time": now_epoch,
                        "changes": [
                            {
                                "field": "comments",
                                "value": {
                                    "id": comment_id,
                                    "text": text,
                                    "from": {
                                        "id": commenter_id,
                                        "username": f"user_{commenter_id[-4:]}",
                                    },
                                    "media": {
                                        "id": "17841499999999999",
                                        "media_product_type": "FEED",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        # Instagram DM
        return {
            "object": "instagram",
            "entry": [
                {
                    "id": "17841400000000001",
                    "time": now_epoch,
                    "messaging": [
                        {
                            "sender": {"id": commenter_id},
                            "recipient": {"id": "17841400000000001"},
                            "timestamp": now_epoch * 1000,
                            "message": {
                                "mid": comment_id,
                                "text": text,
                            },
                        }
                    ],
                }
            ],
        }

    # Facebook Page
    if event_type == "comment":
        return {
            "object": "page",
            "entry": [
                {
                    "id": "100000000000001",
                    "time": now_epoch,
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "verb": "add",
                                "comment_id": comment_id,
                                "post_id": "100000000000001_101",
                                "from": {
                                    "id": commenter_id,
                                    "name": f"Customer {commenter_id[-4:]}",
                                },
                                "message": text,
                                "created_time": now_epoch,
                            },
                        }
                    ],
                }
            ],
        }
    # Facebook Messenger
    return {
        "object": "page",
        "entry": [
            {
                "id": "100000000000001",
                "time": now_epoch,
                "messaging": [
                    {
                        "sender": {"id": commenter_id},
                        "recipient": {"id": "100000000000001"},
                        "timestamp": now_epoch * 1000,
                        "message": {
                            "mid": comment_id,
                            "text": text,
                        },
                    }
                ],
            }
        ],
    }


def send_webhook(
    url: str,
    payload_bytes: bytes,
    secret: str,
    bad_signature: bool = False,
) -> tuple[int, str, float]:
    """Calculate HMAC-SHA256 signature and execute HTTP POST request."""
    if bad_signature:
        sig_header = "sha256=invalid_tampered_signature_hex_deadbeef0000"
    else:
        mac = hmac.new(
            key=secret.encode("utf-8"),
            msg=payload_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()
        sig_header = f"sha256={mac}"

    req = urllib.request.Request(
        url=url,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig_header,
            "User-Agent": "MetaWebhookSimulator/1.0",
        },
        method="POST",
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            body = resp.read().decode("utf-8")
            return resp.status, body, elapsed_ms
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        body = exc.read().decode("utf-8")
        return exc.code, body, elapsed_ms


def main() -> None:
    """Run webhook simulation."""
    args = parse_args()
    secret = get_app_secret(args.secret)
    comment_id = args.comment_id or f"sim_{uuid.uuid4().hex[:12]}"

    payload = build_payload(
        platform=args.platform,
        event_type=args.event_type,
        text=args.text,
        commenter_id=args.commenter_id,
        comment_id=comment_id,
    )
    payload_bytes = json.dumps(payload).encode("utf-8")

    print("=" * 60)
    print("Meta Webhook Event Simulator")
    print(f"Target URL:    {args.url}")
    print(f"Platform:      {args.platform}")
    print(f"Event Type:    {args.event_type}")
    print(f"Comment/MID:   {comment_id}")
    print(f"Commenter ID:  {args.commenter_id}")
    print(f"Text Content:  {args.text}")
    print(f"Bad Signature: {args.bad_signature}")
    print(f"Repetitions:   {args.repeat}")
    print("=" * 60)

    for i in range(1, args.repeat + 1):
        status_code, body, elapsed_ms = send_webhook(
            url=args.url,
            payload_bytes=payload_bytes,
            secret=secret,
            bad_signature=args.bad_signature,
        )
        print(
            f"[{i}/{args.repeat}] Status: {status_code} | Latency: {elapsed_ms:.1f}ms | Response: {body}"
        )
        if i < args.repeat:
            time.sleep(0.1)

    if status_code == 200:
        print("\nSuccess: Webhook ingested successfully.")
    elif status_code == 403:
        print("\nSecurity verification working: Request rejected with 403 Forbidden.")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
