#!/usr/bin/env python3
"""
Dump Origin's GraphQL schema so we know what's actually available.

Everything the integration queries so far was reverse-engineered from the
web app's own network traffic, which only shows what that app happens to
use. If the gateway allows introspection it will tell us the whole
surface directly -- every field, argument and enum -- including data the
SPA never asks for.

Writes the raw introspection result to JSON for follow-up querying and
prints a readable summary of the parts that matter for energy data.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/explore_schema.py
"""
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS, GRAPHQL_URL

OUT_PATH = "/tmp/origin_energy_test/schema.json"

# Types whose fields we always print in full -- the ones an energy
# integration would plausibly build on.
INTERESTING = (
    "usage",
    "service",
    "account",
    "bill",
    "invoice",
    "tariff",
    "rate",
    "meter",
    "property",
    "payment",
    "solar",
    "feed",
    "consumption",
    "cost",
    "viewer",
    "kraken",
    "query",
)

# Full introspection, minus descriptions on every field (too noisy) and
# minus deprecated members we'd never call anyway.
INTROSPECTION = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      description
      fields(includeDeprecated: false) {
        name
        description
        args { name type { ...TypeRef } defaultValue }
        type { ...TypeRef }
      }
      inputFields { name type { ...TypeRef } defaultValue }
      enumValues(includeDeprecated: false) { name description }
      possibleTypes { name }
      interfaces { name }
    }
  }
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType { kind name ofType { kind name } }
    }
  }
}
"""


def type_name(ref: dict | None) -> str:
    """Flatten a nested introspection type ref into GraphQL notation."""
    if not ref:
        return "?"
    kind = ref.get("kind")
    if kind == "NON_NULL":
        return f"{type_name(ref.get('ofType'))}!"
    if kind == "LIST":
        return f"[{type_name(ref.get('ofType'))}]"
    return ref.get("name") or "?"


def print_type(entry: dict) -> None:
    """Print one type's fields/enum values in a compact readable form."""
    name = entry.get("name")
    kind = entry.get("kind")
    print(f"\n--- {kind} {name} ---")
    if entry.get("description"):
        print(f"    {entry['description'].strip().splitlines()[0][:100]}")

    for field in entry.get("fields") or ():
        args = ", ".join(
            f"{a['name']}: {type_name(a['type'])}" for a in field.get("args") or ()
        )
        sig = f"{field['name']}({args})" if args else field["name"]
        print(f"    {sig} -> {type_name(field['type'])}")

    for field in entry.get("inputFields") or ():
        print(f"    .{field['name']}: {type_name(field['type'])}")

    values = entry.get("enumValues") or ()
    if values:
        print("    " + " | ".join(v["name"] for v in values))


async def main() -> None:
    cookie = os.environ.get("ORIGIN_SESSION_COOKIE") or getpass.getpass(
        "auth0 session cookie value: "
    ).strip()

    async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
        try:
            tokens = await async_login_with_cookie(session, cookie)
        except OriginEnergyAuthError as err:
            print(f"AUTH FAILED: {err}")
            return

        async with session.post(
            GRAPHQL_URL,
            json={"query": INTROSPECTION},
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        ) as resp:
            print(f"HTTP {resp.status}")
            payload = await resp.json()

    if payload.get("errors"):
        # Introspection being disabled is itself worth knowing -- it means
        # any further discovery has to come from error messages.
        print("INTROSPECTION REJECTED:")
        print(json.dumps(payload["errors"], indent=2)[:2000])
        return

    schema = payload["data"]["__schema"]
    types = [t for t in schema["types"] if not (t.get("name") or "").startswith("__")]

    Path(OUT_PATH).write_text(json.dumps(schema, indent=2))
    print(f"\nWrote full schema to {OUT_PATH}")
    print(f"Query root: {schema['queryType']['name']}")
    print(f"Mutation root: {(schema.get('mutationType') or {}).get('name')}")

    by_kind: dict[str, list[str]] = {}
    for entry in types:
        by_kind.setdefault(entry["kind"], []).append(entry.get("name") or "?")
    print("\n=== type counts ===")
    for kind, names in sorted(by_kind.items()):
        print(f"  {kind:12} {len(names)}")

    print("\n=== enums ===")
    for entry in types:
        if entry["kind"] == "ENUM":
            values = " | ".join(v["name"] for v in entry.get("enumValues") or ())
            print(f"  {entry['name']}: {values[:160]}")

    print("\n=== energy-relevant types ===")
    for entry in sorted(types, key=lambda t: t.get("name") or ""):
        name = (entry.get("name") or "").lower()
        if entry["kind"] in ("OBJECT", "INTERFACE", "INPUT_OBJECT") and any(
            key in name for key in INTERESTING
        ):
            print_type(entry)

    print("\n=== all type names ===")
    for kind, names in sorted(by_kind.items()):
        print(f"\n{kind}:")
        for chunk_start in range(0, len(names), 4):
            print("  " + "".join(f"{n:38}" for n in sorted(names)[chunk_start:chunk_start + 4]))


if __name__ == "__main__":
    asyncio.run(main())
