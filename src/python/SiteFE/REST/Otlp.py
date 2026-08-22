#!/usr/bin/env python3
# pylint: disable=line-too-long
"""
OTLP relay API Calls

The frontend is the site's telemetry egress point. Agents and switches sit on
private networks and only the frontend is reachable from outside, because
SENSE-O has to reach it -- so an agent either exports through here or does not
export at all.

This FORWARDS the payload rather than re-exporting it. The frontend does not
parse the protobuf, build spans from it, or feed it into its own provider. Two
reasons:

  - Re-exporting would rewrite the resource, and an agent's spans would arrive
    at the gateway looking like the frontend's. Telling agents apart is the
    entire point of agent tracing, so that is not a detail.
  - The gateway already stamps `sitename` from the verified credential and
    overwrites whatever the resource claims, so there is nothing the frontend
    needs to add on the way past. The agent's identity survives and the site's
    identity is asserted by the connection this relay opens, not by the agent.

One egress point, one credential, one firewall hole per site.

Off by default, gated on `general.otlp_relay`, mirroring how
`node_exporter_passthrough` gates the metrics passthrough next door.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import Response
from SiteFE.REST.dependencies import DEFAULT_RESPONSES, apiWriteDeps, checkSite
from SiteRMLibs.OtelAuth import getTokenSource
from SiteRMLibs.OtelRelay import MAX_BODY_BYTES, relayTarget

router = APIRouter()

RELAY_TIMEOUT = 30.0


# =========================================================
# /api/{sitename}/otlp/v1/{signal}
# =========================================================


@router.post(
    "/{sitename}/otlp/v1/{signal}",
    summary="Relay an OTLP payload upstream",
    description=(
        "Forwards an OTLP/HTTP payload to the central collector on behalf of an agent that "
        "cannot reach it directly. The body is passed through unmodified, so the agent's own "
        "resource attributes survive; the frontend supplies the credential and the collector "
        "derives the site from it. Requires general.otlp_relay."
    ),
    tags=["OTLP"],
    responses={
        **DEFAULT_RESPONSES,
        200: {"description": "Upstream accepted the payload"},
        404: {"description": "Relay not enabled, or unknown signal"},
        413: {"description": "Payload too large"},
        502: {"description": "Upstream collector could not be reached"},
        503: {"description": "Relay enabled but not usable -- see detail"},
    },
)
async def relayOtlp(
    request: Request,
    sitename: str = Path(..., description="Site name"),
    signal: str = Path(..., description="One of traces, metrics, logs"),
    deps=Depends(apiWriteDeps),
):
    """Forward one OTLP payload upstream, unmodified."""
    checkSite(deps, sitename)
    target, problem = relayTarget(deps["config"]["MAIN"].get("general", {}), signal)
    if problem:
        raise HTTPException(status_code=problem[0], detail=problem[1])

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"OTLP payload is {len(body)} bytes; the relay accepts at most {MAX_BODY_BYTES}.",
        )

    # Content-Type distinguishes protobuf from JSON and the collector needs the
    # original. Everything else is dropped: the agent's Authorization is for
    # THIS frontend and must not be replayed upstream.
    headers = {"Content-Type": request.headers.get("content-type", "application/x-protobuf")}
    token = getTokenSource().token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=RELAY_TIMEOUT) as client:
            upstream = await client.post(target, content=body, headers=headers)
    except httpx.RequestError as ex:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach the collector at {target}: {ex}",
        ) from ex

    # The upstream status is returned as-is rather than translated. An agent's
    # exporter already knows how to read OTLP status codes -- a 429 with
    # Retry-After means back off, and inventing our own code would lose that.
    passthrough = {}
    for header in ("content-type", "retry-after"):
        if header in upstream.headers:
            passthrough[header.title()] = upstream.headers[header]
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=passthrough,
    )
