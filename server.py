#!/usr/bin/env python3
"""
eBay + NetSuite MCP Server
Handles eBay Trading API calls AND NetSuite REST custom-record operations,
including end-to-end workflow tools that orchestrate both systems.
Hosted on Render.com, used as a remote MCP connector in Claude.
"""

import base64
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp import types
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount
import uvicorn

# ── Configuration ──────────────────────────────────────────────────────────────
EBAY_API_URL      = "https://api.ebay.com/ws/api.dll"
EBAY_SITE_ID      = os.getenv("EBAY_SITE_ID", "0")
EBAY_COMPAT_LEVEL = os.getenv("EBAY_COMPATIBILITY_LEVEL", "1421")
EBAY_IAF_TOKEN    = os.getenv("EBAY_IAF_TOKEN", "")

NS_ACCOUNT_ID    = os.getenv("NS_ACCOUNT_ID", "")       # e.g. 1234567 or 1234567-sb1
NS_CLIENT_ID     = os.getenv("NS_CLIENT_ID", "")
NS_CLIENT_SECRET = os.getenv("NS_CLIENT_SECRET", "")

MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
PORT           = int(os.getenv("PORT", "8000"))

# NetSuite custom record types and constants (from Celigo eBay integration)
NS_EBAY_RECORD  = "customrecord_celigo_ebayio_item_account"
NS_BBL_RECORD   = "customrecord_bbl"
NS_EBAY_ACCT_ID = "91199"


# ══════════════════════════════════════════════════════════════════════════════
# eBay helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ebay_headers(call_name: str) -> dict:
    return {
        "X-EBAY-API-SITEID":              EBAY_SITE_ID,
        "X-EBAY-API-COMPATIBILITY-LEVEL": EBAY_COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME":           call_name,
        "X-EBAY-API-IAF-TOKEN":           EBAY_IAF_TOKEN,
        "Content-Type":                   "text/xml",
    }


def _ebay_call(call_name: str, body: str) -> ET.Element:
    resp = requests.post(
        EBAY_API_URL, headers=_ebay_headers(call_name),
        data=body.encode("utf-8"), timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _ebay_check(root: ET.Element) -> None:
    ack = root.findtext("Ack", "")
    if ack in ("Failure", "PartialFailure"):
        msgs = [
            f"[{e.findtext('SeverityCode','')}] "
            f"{e.findtext('LongMessage', e.findtext('ShortMessage','unknown error'))}"
            for e in root.findall(".//Errors")
        ]
        raise RuntimeError("eBay error: " + " | ".join(msgs))


def _t(el: ET.Element, path: str, default: str = "N/A") -> str:
    return el.findtext(path, default) or default


# ══════════════════════════════════════════════════════════════════════════════
# NetSuite client (OAuth 2.0 client credentials)
# ══════════════════════════════════════════════════════════════════════════════

class NetSuiteClient:
    def __init__(self):
        self._access_token = None
        self._token_expiry = 0

    def _subdomain(self) -> str:
        # 1234567 → 1234567   |   1234567_SB1 → 1234567-sb1
        return NS_ACCOUNT_ID.lower().replace("_", "-")

    @property
    def base_url(self) -> str:
        return f"https://{self._subdomain()}.suitetalk.api.netsuite.com/services/rest/record/v1"

    @property
    def _token_url(self) -> str:
        return f"https://{self._subdomain()}.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token"

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        creds = base64.b64encode(f"{NS_CLIENT_ID}:{NS_CLIENT_SECRET}".encode()).decode()
        r = requests.post(
            self._token_url,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data="grant_type=client_credentials&scope=rest_webservices",
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self._access_token = data["access_token"]
        self._token_expiry  = time.time() + data.get("expires_in", 3600)
        return self._access_token

    def _h(self) -> dict:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type":  "application/json",
        }

    def query(self, record_type: str, q: str, limit: int = 50) -> list:
        r = requests.get(
            f"{self.base_url}/{record_type}",
            headers=self._h(),
            params={"q": q, "limit": limit},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("items", [])

    def get(self, record_type: str, record_id: str) -> dict:
        r = requests.get(
            f"{self.base_url}/{record_type}/{record_id}",
            headers=self._h(),
            params={"expandSubResources": "true"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def create(self, record_type: str, fields: dict) -> str:
        r = requests.post(
            f"{self.base_url}/{record_type}",
            headers=self._h(),
            json=fields,
            timeout=30,
        )
        r.raise_for_status()
        loc = r.headers.get("Location", "")
        return loc.split("/")[-1] if loc else "created"

    def update(self, record_type: str, record_id: str, fields: dict) -> None:
        r = requests.patch(
            f"{self.base_url}/{record_type}/{record_id}",
            headers=self._h(),
            json=fields,
            timeout=30,
        )
        r.raise_for_status()

    def delete(self, record_type: str, record_id: str) -> None:
        r = requests.delete(
            f"{self.base_url}/{record_type}/{record_id}",
            headers=self._h(),
            timeout=30,
        )
        r.raise_for_status()


ns = NetSuiteClient()


# ══════════════════════════════════════════════════════════════════════════════
# MCP Server
# ══════════════════════════════════════════════════════════════════════════════

server = Server("ebay-netsuite-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

        # ── End-to-end workflow tools ────────────────────────────────────────

        types.Tool(
            name="end_listing_by_sku",
            description=(
                "Full end-listing workflow using a SKU. "
                "Looks up the active eBay listing ID from NetSuite, ends the listing on eBay, "
                "then marks the NetSuite record as ended. One call does everything."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "The SKU / part number to look up in NetSuite."},
                    "ending_reason": {
                        "type": "string",
                        "enum": ["NotAvailable", "LostOrBroken", "OtherListingError", "SellToHighBidder", "Sold"],
                        "default": "NotAvailable",
                    },
                },
                "required": ["sku"],
            },
        ),
        types.Tool(
            name="relist_item_by_sku",
            description=(
                "Full relist workflow using a SKU. "
                "Finds the ended listing in NetSuite, relists it on eBay (getting a new Item ID), "
                "deletes the old NetSuite record, and creates a fresh one with the new eBay Item ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "The SKU / part number to relist."},
                },
                "required": ["sku"],
            },
        ),
        types.Tool(
            name="update_price_by_sku",
            description=(
                "Update the eBay price for a SKU by writing to the NetSuite BBL record "
                "(custrecord_bb_set_ebay_price on customrecord_bbl). "
                "Celigo then syncs this price to the live eBay listing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sku":   {"type": "string", "description": "The kit name / part number (custrecord_bbl_brokerbin_part_number)."},
                    "price": {"type": "number", "description": "New price to set (numeric, e.g. 49.99)."},
                },
                "required": ["sku", "price"],
            },
        ),

        # ── NetSuite low-level tools ─────────────────────────────────────────

        types.Tool(
            name="ns_query_records",
            description=(
                "Query any NetSuite custom record type with a filter. "
                "Use SuiteQL-style syntax, e.g. 'custrecordsku_text IS \"ABC\" AND isinactive IS false'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "record_type": {"type": "string", "description": "Internal ID of the custom record type, e.g. customrecord_celigo_ebayio_item_account."},
                    "query":       {"type": "string", "description": "SuiteQL WHERE clause filter."},
                    "limit":       {"type": "integer", "default": 20, "description": "Max records to return (max 100)."},
                },
                "required": ["record_type", "query"],
            },
        ),
        types.Tool(
            name="ns_get_record",
            description="Get a specific NetSuite custom record by its internal ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "record_type": {"type": "string", "description": "Custom record type internal ID."},
                    "record_id":   {"type": "string", "description": "NetSuite internal record ID."},
                },
                "required": ["record_type", "record_id"],
            },
        ),
        types.Tool(
            name="ns_update_record",
            description="Update fields on a NetSuite custom record by internal ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "record_type": {"type": "string", "description": "Custom record type internal ID."},
                    "record_id":   {"type": "string", "description": "NetSuite internal record ID."},
                    "fields":      {"type": "object", "description": "Key/value pairs of fields to update, e.g. {\"custrecord_celigo_ebay_listing_ended\": true}."},
                },
                "required": ["record_type", "record_id", "fields"],
            },
        ),
        types.Tool(
            name="ns_create_record",
            description="Create a new NetSuite custom record.",
            inputSchema={
                "type": "object",
                "properties": {
                    "record_type": {"type": "string", "description": "Custom record type internal ID."},
                    "fields":      {"type": "object", "description": "Field values for the new record. Include 'name' field (required by NetSuite)."},
                },
                "required": ["record_type", "fields"],
            },
        ),
        types.Tool(
            name="ns_delete_record",
            description="Delete a NetSuite custom record by internal ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "record_type": {"type": "string", "description": "Custom record type internal ID."},
                    "record_id":   {"type": "string", "description": "NetSuite internal record ID."},
                },
                "required": ["record_type", "record_id"],
            },
        ),

        # ── eBay direct tools ────────────────────────────────────────────────

        types.Tool(
            name="end_listing",
            description="End an active eBay listing by eBay Item ID (direct eBay call, no NetSuite update).",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "eBay Item ID."},
                    "ending_reason": {
                        "type": "string",
                        "enum": ["NotAvailable", "LostOrBroken", "OtherListingError", "SellToHighBidder", "Sold"],
                        "default": "NotAvailable",
                    },
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="relist_item",
            description="Relist an ended eBay listing by Item ID (direct eBay call, no NetSuite update).",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "Item ID of the ended listing."},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="revise_item_price",
            description="Update the price of an active eBay listing by Item ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id":     {"type": "string", "description": "eBay Item ID."},
                    "start_price": {"type": "number", "description": "New price (e.g. 29.99)."},
                    "currency":    {"type": "string", "default": "USD"},
                },
                "required": ["item_id", "start_price"],
            },
        ),
        types.Tool(
            name="revise_item",
            description="Revise title, description, price, and/or quantity on an active eBay listing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id":     {"type": "string",  "description": "eBay Item ID."},
                    "title":       {"type": "string",  "description": "New title (optional)."},
                    "start_price": {"type": "number",  "description": "New price (optional)."},
                    "quantity":    {"type": "integer", "description": "New quantity (optional)."},
                    "description": {"type": "string",  "description": "New description HTML (optional)."},
                    "currency":    {"type": "string",  "default": "USD"},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="get_item",
            description="Get full details of an eBay listing by Item ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "eBay Item ID."},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="get_my_ebay_selling",
            description="Summary of the seller's active, sold, and/or unsold listings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "active_list":      {"type": "boolean", "default": True},
                    "sold_list":        {"type": "boolean", "default": True},
                    "unsold_list":      {"type": "boolean", "default": False},
                    "entries_per_page": {"type": "integer", "default": 100},
                },
            },
        ),
        types.Tool(
            name="get_seller_list",
            description="Paginated list of all items the seller has listed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entries_per_page": {"type": "integer", "default": 100},
                    "page_number":      {"type": "integer", "default": 1},
                    "days_back":        {"type": "integer", "default": 30, "description": "Max 120."},
                },
            },
        ),
        types.Tool(
            name="get_item_transactions",
            description="Get sale transactions for a specific eBay listing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id":          {"type": "string",  "description": "eBay Item ID."},
                    "entries_per_page": {"type": "integer", "default": 50},
                    "page_number":      {"type": "integer", "default": 1},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="get_seller_transactions",
            description="All recent sale transactions across the seller's account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days_back":        {"type": "integer", "default": 30},
                    "entries_per_page": {"type": "integer", "default": 100},
                    "page_number":      {"type": "integer", "default": 1},
                },
            },
        ),
        types.Tool(
            name="get_orders",
            description="Get orders placed with the seller, filterable by status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days_back":    {"type": "integer", "default": 30},
                    "order_status": {
                        "type": "string",
                        "enum": ["Active", "Cancelled", "Completed", "Inactive", "All"],
                        "default": "All",
                    },
                    "entries_per_page": {"type": "integer", "default": 100},
                    "page_number":      {"type": "integer", "default": 1},
                },
            },
        ),
        types.Tool(
            name="get_user_profile",
            description="Get public profile and feedback info for any eBay user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "eBay username."},
                },
                "required": ["user_id"],
            },
        ),
        types.Tool(
            name="get_feedback",
            description="Get feedback entries for the seller or any eBay user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Leave empty for authenticated seller."},
                    "feedback_type": {
                        "type": "string",
                        "enum": ["FeedbackReceived", "FeedbackLeft", "FeedbackReceivedAsSeller",
                                 "FeedbackReceivedAsBuyer", "FeedbackLeftForBuyer"],
                        "default": "FeedbackReceivedAsSeller",
                    },
                    "entries_per_page": {"type": "integer", "default": 25},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as exc:
        result = f"Error: {exc}"
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    handlers = {
        # workflow
        "end_listing_by_sku":     _end_listing_by_sku,
        "relist_item_by_sku":     _relist_item_by_sku,
        "update_price_by_sku":    _update_price_by_sku,
        # netsuite
        "ns_query_records":       _ns_query_records,
        "ns_get_record":          _ns_get_record,
        "ns_update_record":       _ns_update_record,
        "ns_create_record":       _ns_create_record,
        "ns_delete_record":       _ns_delete_record,
        # ebay direct
        "end_listing":            _end_listing,
        "relist_item":            _relist_item,
        "revise_item_price":      _revise_item_price,
        "revise_item":            _revise_item,
        "get_item":               _get_item,
        "get_my_ebay_selling":    _get_my_ebay_selling,
        "get_seller_list":        _get_seller_list,
        "get_item_transactions":  _get_item_transactions,
        "get_seller_transactions":_get_seller_transactions,
        "get_orders":             _get_orders,
        "get_user_profile":       _get_user_profile,
        "get_feedback":           _get_feedback,
    }
    fn = handlers.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    return fn(args)


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end workflow tools
# ══════════════════════════════════════════════════════════════════════════════

def _end_listing_by_sku(args: dict) -> str:
    sku    = args["sku"]
    reason = args.get("ending_reason", "NotAvailable")

    # 1. Find active listing record in NetSuite
    items = ns.query(
        NS_EBAY_RECORD,
        f'custrecordsku_text IS "{sku}" AND custrecord_celigo_ebay_listing_ended IS false AND isinactive IS false',
    )
    if not items:
        return f"No active eBay listing found in NetSuite for SKU: {sku}"

    record    = ns.get(NS_EBAY_RECORD, str(items[0]["id"]))
    ns_id     = str(record["id"])
    ebay_id   = record.get("custrecord_celigo_ebay_listing_id", "")

    if not ebay_id:
        return f"NetSuite record {ns_id} found for SKU {sku} but has no eBay listing ID."

    # 2. End the eBay listing
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<EndItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <ItemID>{ebay_id}</ItemID>
  <EndingReason>{reason}</EndingReason>
</EndItemRequest>"""
    root = _ebay_call("EndItem", xml)
    _ebay_check(root)
    end_time = _t(root, "EndTime")

    # 3. Mark NetSuite record as ended
    ns.update(NS_EBAY_RECORD, ns_id, {"custrecord_celigo_ebay_listing_ended": True})

    return "\n".join([
        "Listing ended successfully.",
        f"  SKU:            {sku}",
        f"  eBay Item ID:   {ebay_id}",
        f"  End time:       {end_time}",
        f"  NS Record ID:   {ns_id} — marked as ended.",
    ])


def _relist_item_by_sku(args: dict) -> str:
    sku = args["sku"]

    # 1. Find ended listing record in NetSuite
    items = ns.query(
        NS_EBAY_RECORD,
        f'custrecordsku_text IS "{sku}" AND custrecord_celigo_ebay_listing_ended IS true AND isinactive IS false',
    )
    if not items:
        return f"No ended eBay listing found in NetSuite for SKU: {sku}"

    record      = ns.get(NS_EBAY_RECORD, str(items[0]["id"]))
    ns_id       = str(record["id"])
    old_ebay_id = record.get("custrecord_celigo_ebay_listing_id", "")

    # custrecord_celigo_ebay_item_nsid is a reference — extract its id
    ns_item_ref = record.get("custrecord_celigo_ebay_item_nsid", {})
    ns_item_id  = ns_item_ref.get("id", "") if isinstance(ns_item_ref, dict) else str(ns_item_ref)

    if not old_ebay_id:
        return f"NetSuite record {ns_id} found for SKU {sku} but has no eBay listing ID."

    # 2. Relist on eBay — returns a new Item ID
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<RelistItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    <ItemID>{old_ebay_id}</ItemID>
  </Item>
</RelistItemRequest>"""
    root = _ebay_call("RelistItem", xml)
    _ebay_check(root)
    new_ebay_id = _t(root, "ItemID")

    # 3. Delete old NetSuite record
    ns.delete(NS_EBAY_RECORD, ns_id)

    # 4. Create new NetSuite record with the new eBay Item ID
    new_fields = {
        "name":                            "-",
        "custrecord_celigo_ebay_listing_id": str(new_ebay_id),
        "custrecord_celigo_ebay_account_id": NS_EBAY_ACCT_ID,
    }
    if ns_item_id:
        new_fields["custrecord_celigo_ebay_item_nsid"] = {"id": ns_item_id}

    new_ns_id = ns.create(NS_EBAY_RECORD, new_fields)

    return "\n".join([
        "Item relisted successfully.",
        f"  SKU:               {sku}",
        f"  Old eBay Item ID:  {old_ebay_id}",
        f"  New eBay Item ID:  {new_ebay_id}",
        f"  Old NS Record:     {ns_id} — deleted.",
        f"  New NS Record:     {new_ns_id} — created.",
    ])


def _update_price_by_sku(args: dict) -> str:
    sku   = args["sku"]
    price = args["price"]

    # 1. Find BBL record by part number
    items = ns.query(
        NS_BBL_RECORD,
        f'custrecord_bbl_brokerbin_part_number IS "{sku}"',
    )
    if not items:
        return f"No BBL record found in NetSuite for SKU: {sku}"

    record_id = str(items[0]["id"])

    # 2. Update the eBay price field
    ns.update(NS_BBL_RECORD, record_id, {"custrecord_bb_set_ebay_price": price})

    return "\n".join([
        "eBay price updated in NetSuite.",
        f"  SKU:        {sku}",
        f"  New price:  ${price}",
        f"  NS Record:  {record_id}",
        "  Celigo will sync this price to the live eBay listing.",
    ])


# ══════════════════════════════════════════════════════════════════════════════
# NetSuite low-level tools
# ══════════════════════════════════════════════════════════════════════════════

def _ns_query_records(args: dict) -> str:
    record_type = args["record_type"]
    query       = args["query"]
    limit       = min(args.get("limit", 20), 100)

    items = ns.query(record_type, query, limit)
    if not items:
        return f"No records found in {record_type} matching: {query}"

    lines = [f"Found {len(items)} record(s) in {record_type}:"]
    for item in items:
        lines.append(f"  ID: {item.get('id')}  |  {item}")
    return "\n".join(lines)


def _ns_get_record(args: dict) -> str:
    record_type = args["record_type"]
    record_id   = args["record_id"]

    record = ns.get(record_type, record_id)
    lines  = [f"Record {record_type} / {record_id}:"]
    for k, v in record.items():
        if k != "links":
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def _ns_update_record(args: dict) -> str:
    record_type = args["record_type"]
    record_id   = args["record_id"]
    fields      = args["fields"]

    ns.update(record_type, record_id, fields)
    updated = ", ".join(fields.keys())
    return f"Record {record_type}/{record_id} updated. Fields changed: {updated}"


def _ns_create_record(args: dict) -> str:
    record_type = args["record_type"]
    fields      = args["fields"]

    new_id = ns.create(record_type, fields)
    return f"Record created in {record_type}. New NS ID: {new_id}"


def _ns_delete_record(args: dict) -> str:
    record_type = args["record_type"]
    record_id   = args["record_id"]

    ns.delete(record_type, record_id)
    return f"Record {record_type}/{record_id} deleted."


# ══════════════════════════════════════════════════════════════════════════════
# eBay direct tools
# ══════════════════════════════════════════════════════════════════════════════

def _end_listing(args: dict) -> str:
    item_id = args["item_id"]
    reason  = args.get("ending_reason", "NotAvailable")
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<EndItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <ItemID>{item_id}</ItemID>
  <EndingReason>{reason}</EndingReason>
</EndItemRequest>"""
    root = _ebay_call("EndItem", xml)
    _ebay_check(root)
    return f"Listing {item_id} ended. End time: {_t(root, 'EndTime')}"


def _relist_item(args: dict) -> str:
    item_id = args["item_id"]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<RelistItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item><ItemID>{item_id}</ItemID></Item>
</RelistItemRequest>"""
    root = _ebay_call("RelistItem", xml)
    _ebay_check(root)
    new_id = _t(root, "ItemID")
    fees   = [f"  {_t(f,'Name')}: ${_t(f,'Fee')}" for f in root.findall(".//Fee")]
    lines  = [f"Relisted. Original: {item_id} → New: {new_id}"]
    if fees:
        lines += ["Fees:"] + fees
    return "\n".join(lines)


def _revise_item_price(args: dict) -> str:
    item_id  = args["item_id"]
    price    = args["start_price"]
    currency = args.get("currency", "USD")
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    <ItemID>{item_id}</ItemID>
    <StartPrice currencyID="{currency}">{price}</StartPrice>
  </Item>
</ReviseItemRequest>"""
    root = _ebay_call("ReviseItem", xml)
    _ebay_check(root)
    return f"Price updated for item {item_id}: {currency} {price}"


def _revise_item(args: dict) -> str:
    item_id = args["item_id"]
    parts   = [f"<ItemID>{item_id}</ItemID>"]
    if "title"       in args: parts.append(f"<Title>{args['title']}</Title>")
    if "start_price" in args:
        cur = args.get("currency", "USD")
        parts.append(f'<StartPrice currencyID="{cur}">{args["start_price"]}</StartPrice>')
    if "quantity"    in args: parts.append(f"<Quantity>{args['quantity']}</Quantity>")
    if "description" in args: parts.append(f"<Description><![CDATA[{args['description']}]]></Description>")
    if len(parts) == 1:
        return "No fields to revise were provided."
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>{''.join(parts)}</Item>
</ReviseItemRequest>"""
    root = _ebay_call("ReviseItem", xml)
    _ebay_check(root)
    changed = [k for k in ("title", "start_price", "quantity", "description") if k in args]
    return f"Item {item_id} revised. Updated: {', '.join(changed)}"


def _get_item(args: dict) -> str:
    item_id = args["item_id"]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    root = _ebay_call("GetItem", xml)
    _ebay_check(root)
    item = root.find("Item")
    if item is None:
        return "Item not found."
    return "\n".join([
        f"Item ID:       {_t(item, 'ItemID')}",
        f"Title:         {_t(item, 'Title')}",
        f"SKU:           {_t(item, 'SKU')}",
        f"Status:        {_t(item, 'SellingStatus/ListingStatus')}",
        f"Current Price: {_t(item, 'SellingStatus/CurrentPrice')}",
        f"Quantity:      {_t(item, 'Quantity')}",
        f"Qty Sold:      {_t(item, 'SellingStatus/QuantitySold')}",
        f"Condition:     {_t(item, 'ConditionDisplayName')}",
        f"Category:      {_t(item, 'PrimaryCategory/CategoryName')}",
        f"Listed:        {_t(item, 'ListingDetails/StartTime')}",
        f"Ends:          {_t(item, 'ListingDetails/EndTime')}",
        f"View URL:      {_t(item, 'ListingDetails/ViewItemURL')}",
    ])


def _get_my_ebay_selling(args: dict) -> str:
    per_page = min(args.get("entries_per_page", 100), 200)
    active   = args.get("active_list", True)
    sold     = args.get("sold_list", True)
    unsold   = args.get("unsold_list", False)

    def _pag():
        return f"<Pagination><EntriesPerPage>{per_page}</EntriesPerPage><PageNumber>1</PageNumber></Pagination>"

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  {'<ActiveList><Include>true</Include>' + _pag() + '</ActiveList>' if active else ''}
  {'<SoldList><Include>true</Include>'   + _pag() + '</SoldList>'   if sold   else ''}
  {'<UnsoldList><Include>true</Include>' + _pag() + '</UnsoldList>' if unsold else ''}
</GetMyeBaySellingRequest>"""
    root = _ebay_call("GetMyeBaySelling", xml)
    _ebay_check(root)
    sections = []
    if active:
        its = root.findall(".//ActiveList/ItemArray/Item")
        lines = [f"=== ACTIVE ({len(its)}) ==="] if its else ["No active listings."]
        for it in its:
            lines.append(f"  {_t(it,'ItemID')} | {_t(it,'Title')[:55]} | ${_t(it,'SellingStatus/CurrentPrice')} | Qty: {_t(it,'QuantityAvailable')}")
        sections.append("\n".join(lines))
    if sold:
        txns = root.findall(".//SoldList/OrderTransactionArray/OrderTransaction/Transaction")
        lines = [f"=== SOLD ({len(txns)}) ==="] if txns else ["No sold items."]
        for t in txns:
            lines.append(f"  {_t(t,'Item/ItemID')} | {_t(t,'Item/Title')[:50]} | ${_t(t,'TransactionPrice')} x {_t(t,'QuantityPurchased')} | Buyer: {_t(t,'Buyer/UserID')}")
        sections.append("\n".join(lines))
    if unsold:
        its = root.findall(".//UnsoldList/ItemArray/Item")
        lines = [f"=== UNSOLD ({len(its)}) ==="] if its else ["No unsold items."]
        for it in its:
            lines.append(f"  {_t(it,'ItemID')} | {_t(it,'Title')[:55]}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) or "No data returned."


def _get_seller_list(args: dict) -> str:
    per_page = min(args.get("entries_per_page", 100), 200)
    page     = args.get("page_number", 1)
    days     = min(args.get("days_back", 30), 120)
    now      = datetime.utcnow()
    start    = now - timedelta(days=days)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <EndTimeFrom>{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</EndTimeFrom>
  <EndTimeTo>{now.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</EndTimeTo>
  <Pagination><EntriesPerPage>{per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  <DetailLevel>ReturnAll</DetailLevel>
  <GranularityLevel>Fine</GranularityLevel>
</GetSellerListRequest>"""
    root  = _ebay_call("GetSellerList", xml)
    _ebay_check(root)
    items = root.findall(".//ItemArray/Item")
    if not items:
        return "No listings found."
    total = _t(root, "TotalNumberOfEntries", "?")
    lines = [f"Page {page} — {len(items)} of {total} listings:"]
    for it in items:
        lines.append(f"  {_t(it,'ItemID')} | {_t(it,'Title')[:55]} | ${_t(it,'SellingStatus/CurrentPrice')} | {_t(it,'SellingStatus/ListingStatus')}")
    return "\n".join(lines)


def _get_item_transactions(args: dict) -> str:
    item_id  = args["item_id"]
    per_page = min(args.get("entries_per_page", 50), 200)
    page     = args.get("page_number", 1)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemTransactionsRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <ItemID>{item_id}</ItemID>
  <Pagination><EntriesPerPage>{per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
</GetItemTransactionsRequest>"""
    root = _ebay_call("GetItemTransactions", xml)
    _ebay_check(root)
    txns = root.findall(".//TransactionArray/Transaction")
    if not txns:
        return f"No transactions found for item {item_id}."
    lines = [f"Transactions for {item_id} ({len(txns)}):"]
    for t in txns:
        lines.append(f"  {_t(t,'TransactionID')} | Qty: {_t(t,'QuantityPurchased')} | ${_t(t,'TransactionPrice')} | {_t(t,'Buyer/UserID')} | {_t(t,'CreatedDate')}")
    return "\n".join(lines)


def _get_seller_transactions(args: dict) -> str:
    days     = min(args.get("days_back", 30), 30)
    per_page = min(args.get("entries_per_page", 100), 200)
    page     = args.get("page_number", 1)
    since    = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerTransactionsRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <ModTimeFrom>{since}</ModTimeFrom>
  <Pagination><EntriesPerPage>{per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
</GetSellerTransactionsRequest>"""
    root = _ebay_call("GetSellerTransactions", xml)
    _ebay_check(root)
    txns  = root.findall(".//TransactionArray/Transaction")
    total = _t(root, "TotalNumberOfEntries", "?")
    if not txns:
        return "No transactions found."
    lines = [f"Page {page} — {len(txns)} of {total} transactions:"]
    for t in txns:
        lines.append(f"  {_t(t,'Item/ItemID')} | {_t(t,'Item/Title')[:45]} | Qty:{_t(t,'QuantityPurchased')} | ${_t(t,'TransactionPrice')} | {_t(t,'Buyer/UserID')} | {_t(t,'CreatedDate')}")
    return "\n".join(lines)


def _get_orders(args: dict) -> str:
    days     = min(args.get("days_back", 30), 30)
    status   = args.get("order_status", "All")
    per_page = min(args.get("entries_per_page", 100), 200)
    page     = args.get("page_number", 1)
    since    = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <CreateTimeFrom>{since}</CreateTimeFrom>
  <OrderStatus>{status}</OrderStatus>
  <Pagination><EntriesPerPage>{per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
</GetOrdersRequest>"""
    root   = _ebay_call("GetOrders", xml)
    _ebay_check(root)
    orders = root.findall(".//OrderArray/Order")
    total  = _t(root, "TotalNumberOfEntries", "?")
    if not orders:
        return "No orders found."
    lines = [f"Page {page} — {len(orders)} of {total} orders:"]
    for o in orders:
        lines.append(f"  {_t(o,'OrderID')} | {_t(o,'OrderStatus')} | ${_t(o,'Total')} | {_t(o,'BuyerUserID')} | {_t(o,'CreatedTime')}")
    return "\n".join(lines)


def _get_user_profile(args: dict) -> str:
    user_id = args["user_id"]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <UserID>{user_id}</UserID>
</GetUserRequest>"""
    root = _ebay_call("GetUser", xml)
    _ebay_check(root)
    u = root.find("User")
    if u is None:
        return "User not found."
    return "\n".join([
        f"Username:      {_t(u,'UserID')}",
        f"Feedback:      {_t(u,'FeedbackScore')} ({_t(u,'PositiveFeedbackPercent')}% positive)",
        f"Registered:    {_t(u,'RegistrationDate')}",
        f"Status:        {_t(u,'Status')}",
    ])


def _get_feedback(args: dict) -> str:
    user_id  = args.get("user_id", "")
    fb_type  = args.get("feedback_type", "FeedbackReceivedAsSeller")
    per_page = min(args.get("entries_per_page", 25), 100)
    user_tag = f"<UserID>{user_id}</UserID>" if user_id else ""
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetFeedbackRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  {user_tag}
  <FeedbackType>{fb_type}</FeedbackType>
  <Pagination><EntriesPerPage>{per_page}</EntriesPerPage><PageNumber>1</PageNumber></Pagination>
  <DetailLevel>ReturnAll</DetailLevel>
</GetFeedbackRequest>"""
    root    = _ebay_call("GetFeedback", xml)
    _ebay_check(root)
    entries = root.findall(".//FeedbackDetailArray/FeedbackDetail")
    score   = _t(root, "FeedbackScore", "?")
    total   = _t(root, "FeedbackSummary/TotalPositiveFeedbackEntries", "?")
    lines   = [f"Score: {score}  |  Positive total: {total}"]
    for e in entries:
        lines.append(f"  [{_t(e,'Role')}] {_t(e,'CommentType')} — \"{_t(e,'CommentText')}\" — {_t(e,'CommentingUser')} on {_t(e,'CommentTime')}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP / SSE transport
# ══════════════════════════════════════════════════════════════════════════════

sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request) -> Response:
    if MCP_AUTH_TOKEN:
        auth_header = request.headers.get("Authorization", "")
        auth_param  = request.query_params.get("token", "")
        if auth_header != f"Bearer {MCP_AUTH_TOKEN}" and auth_param != MCP_AUTH_TOKEN:
            return Response("Unauthorized", status_code=401)
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())
    return Response()


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "ebay-netsuite-mcp", "version": "2.0.0"})


app = Starlette(
    routes=[
        Route("/health",    endpoint=health),
        Route("/sse",       endpoint=handle_sse),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
)

if __name__ == "__main__":
    if not EBAY_IAF_TOKEN:
        print("WARNING: EBAY_IAF_TOKEN is not set.")
    if not NS_ACCOUNT_ID:
        print("WARNING: NS_ACCOUNT_ID is not set.")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
