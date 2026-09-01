#!/usr/bin/env python3
"""Verify Object Storage conditional PUT semantics before a production release.

Requires the same standard AWS/Yandex credential environment as the function,
plus OBJECT_STORAGE_BUCKET and OBJECT_STORAGE_ENDPOINT.  The probe only creates
objects beneath ``release-probes/conditional-writes/`` and never prints secrets.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import uuid

import boto3
from botocore.exceptions import ClientError


def _precondition_failed(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") in {"PreconditionFailed", "412"} or status == 412


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=os.getenv("OBJECT_STORAGE_BUCKET"))
    parser.add_argument("--endpoint", default=os.getenv("OBJECT_STORAGE_ENDPOINT"))
    args = parser.parse_args()
    if not args.bucket or not args.endpoint:
        parser.error("OBJECT_STORAGE_BUCKET and OBJECT_STORAGE_ENDPOINT are required")

    client = boto3.client("s3", endpoint_url=args.endpoint)
    key = f"release-probes/conditional-writes/{uuid.uuid4().hex}.json"

    def create_once() -> bool:
        try:
            client.put_object(
                Bucket=args.bucket,
                Key=key,
                Body=b'{"state":"created"}',
                ContentType="application/json",
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            if _precondition_failed(exc):
                return False
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        created = sum(executor.map(lambda _: create_once(), range(10)))
    if created != 1:
        print(f"FAIL create_if_absent_successes={created}", file=sys.stderr)
        return 1

    etag = client.head_object(Bucket=args.bucket, Key=key)["ETag"]
    client.put_object(
        Bucket=args.bucket,
        Key=key,
        Body=b'{"state":"reclaimed"}',
        ContentType="application/json",
        IfMatch=etag,
    )
    try:
        client.put_object(
            Bucket=args.bucket,
            Key=key,
            Body=b'{"state":"stale-write"}',
            ContentType="application/json",
            IfMatch=etag,
        )
    except ClientError as exc:
        if _precondition_failed(exc):
            print(f"PASS key={key} create_if_absent_successes=1 stale_etag_rejected=true")
            return 0
        raise
    print("FAIL stale_etag_rejected=false", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
