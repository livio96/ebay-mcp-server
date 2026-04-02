#!/usr/bin/env python3
"""
eBay MCP Server
Model Context Protocol server for the eBay Trading API.
Designed to be hosted on Render.com and used as a remote MCP connector in Claude.
"""

import os
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
EBAY_API_URL        = "https://api.ebay.com/ws/api.dll"
EBAY_SITE_ID        = os.getenv("EBAY_SITE_ID", "0")          # 0 = US
EBAY_COMPAT_LEVEL   = os.getenv("EBAY_COMPATIBILITY_LEVEL", "1421")
EBAY_IAF_TOKEN      = os.getenv("EBAY_IAF_TOKEN", "")         # eBay User Auth Token
MCP_AUTH_TOKEN      = os.getenv("MCP_AUTH_TOKEN", "")         # Optional server guard
PORT                = int(os.getenv("PORT", "8000"))

# ── eBay helpers ───────────────────────────────────────────────────────────────

def _headers(call_name: str) -> dict:
    return {
        "X-EBAY-API-SITEID":              EBAY_SITE_ID,
        "X-EBAY-API-COMPATIBILITY-LEVEL": EBAY_COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME":           call_name,
        "X-EBAY-API-IAF-TOKEN":           EBAY_IAF_TOKEN,
        "Content-Type":                   "text/xml",
    }


def _call(call_name: str, body: str) -> ET.Element:
    """POST to eBay Trading API; return namespace-stripped root element."""
    resp = requests.post(
        EBAY_API_URL, headers=_headers(call_name),
        data=body.encode("utf-8"), timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    for el in root.iter():          # strip {namespace}
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _check(root: ET.Element) -> None:
    """Raise RuntimeError on eBay Failure/PartialFailure response."""
    ack = root.findtext("Ack", "")
    if ack in ("Failure", "PartialFailure"):
        msgs = [
            f"[{e.findtext('SeverityCode','')}] "
            f"{e.findtext('LongMessage', e.findtext('ShortMessage','unknown error'))}"
            for e in root.findall(".//Errors")
        ]
        raise RuntimeError("eBay error: " + " | ".join(msgs))


def _t(el: ET.Element, path: str, default: str = "N/A") -> str:
    """Safe findtext helper."""
    return el.findtext(path, default) or default


# ── MCP Server ─────────────────────────────────────────────────────────────────

server = Server("ebay-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="end_listing",
            description=(
                "End an active eBay listing before it naturally expires. "
                "Use this to remove items that are sold, lost, or no longer available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "The eBay Item ID of the listing to end.",
                    },
                    "ending_reason": {
                        "type": "string",
                        "enum": [
                            "NotAvailable", "LostOrBroken",
                            "OtherListingError", "SellToHighBidder", "Sold",
                        ],
                        "default": "NotAvailable",
                        "description": "Reason for ending the listing.",
                    },
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="relist_item",
            description=(
                "Relist a previously ended eBay listing. "
                "Creates a new listing with the same details and returns the new Item ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Item ID of the ended listing to relist.",
                    },
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="revise_item_price",
            description=(
                "Update only the price of an active eBay listing. "
                "Faster alternative to revise_item when you only need to change the price."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "eBay Item ID.",
                    },
                    "start_price": {
                        "type": "number",
                        "description": "New price (e.g. 29.99).",
                    },
                    "currency": {
                        "type": "string",
                        "default": "USD",
                        "description": "ISO currency code (default USD).",
                    },
                },
                "required": ["item_id", "start_price"],
            },
        ),
        types.Tool(
            name="revise_item",
            description=(
                "Revise an active eBay listing. "
                "Can update title, description, price, and/or quantity in one call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id":     {"type": "string", "description": "eBay Item ID to revise."},
                    "title":       {"type": "string", "description": "New listing title (optional)."},
                    "start_price": {"type": "number", "description": "New price (optional)."},
                    "quantity":    {"type": "integer", "description": "New available quantity (optional)."},
                    "description": {"type": "string", "description": "New description HTML (optional)."},
                    "currency":    {"type": "string", "default": "USD", "description": "Currency code."},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="get_item",
            description="Retrieve full details of an eBay listing by Item ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "eBay Item ID to look up."},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="get_my_ebay_selling",
            description=(
                "Get a summary of the seller's active, sold, and/or unsold listings. "
                "Good for a quick overview of the account's selling activity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "active_list":      {"type": "boolean", "default": True,  "description": "Include active listings."},
                    "sold_list":        {"type": "boolean", "default": True,  "description": "Include sold items."},
                    "unsold_list":      {"type": "boolean", "default": False, "description": "Include unsold items."},
                    "entries_per_page": {"type": "integer", "default": 100,   "description": "Results per section (max 200)."},
                },
            },
        ),
        types.Tool(
            name="get_seller_list",
            description=(
                "Retrieve a paginated list of items the seller has listed. "
                "Supports filtering by how far back to look (up to 120 days)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entries_per_page": {"type": "integer", "default": 100, "description": "Items per page (max 200)."},
                    "page_number":      {"type": "integer", "default": 1,   "description": "Page to retrieve."},
                    "days_back":        {"type": "integer", "default": 30,  "description": "Days back to search (max 120)."},
                },
            },
        ),
        types.Tool(
            name="get_item_transactions",
            description="Get transaction (sale) records for a specific eBay listing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id":          {"type": "string",  "description": "eBay Item ID."},
                    "entries_per_page": {"type": "integer", "default": 50, "description": "Transactions per page (max 200)."},
                    "page_number":      {"type": "integer", "default": 1},
                },
                "required": ["item_id"],
            },
        ),
        types.Tool(
            name="get_seller_transactions",
            description="Get all recent transactions (sales) across the seller's account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days_back":        {"type": "integer", "default": 30,  "description": "Days back (max 30)."},
                    "entries_per_page": {"type": "integer", "default": 100, "description": "Results per page (max 200)."},
                    "page_number":      {"type": "integer", "default": 1},
                },
            },
        ),
        types.Tool(
            name="get_orders",
            description=(
                "Get orders placed with the seller. "
                "Supports filtering by status (Active, Completed, Cancelled, All)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days_back": {
                        "type": "integer",
                        "default": 30,
                        "description": "Days back to retrieve orders (max 30).",
                    },
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
            description="Get public profile and feedback information for any eBay user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The eBay username to look up.",
                    },
                },
                "required": ["user_id"],
            },
        ),
        types.Tool(
            name="get_feedback",
            description="Get feedback for the authenticated seller or any eBay user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "eBay username (leave empty for the authenticated seller).",
                    },
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


# ── Tool implementations ───────────────────────────────────────────────────────

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
    root = _call("EndItem", xml)
    _check(root)
    return (
        f"Listing {item_id} ended successfully.\n"
        f"End time: {_t(root, 'EndTime')}\n"
        f"Reason: {reason}"
    )


def _relist_item(args: dict) -> str:
    item_id = args["item_id"]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<RelistItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    <ItemID>{item_id}</ItemID>
  </Item>
</RelistItemRequest>"""
    root = _call("RelistItem", xml)
    _check(root)
    new_id = _t(root, "ItemID")
    fees = root.findall(".//Fee")
    fee_lines = [f"  {_t(f, 'Name')}: ${_t(f, 'Fee')}" for f in fees]
    lines = [
        f"Item relisted successfully.",
        f"Original Item ID: {item_id}",
        f"New Item ID:      {new_id}",
    ]
    if fee_lines:
        lines.append("Fees:")
        lines.extend(fee_lines)
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
    root = _call("ReviseItem", xml)
    _check(root)
    return f"Price updated for item {item_id}: {currency} {price}"


def _revise_item(args: dict) -> str:
    item_id = args["item_id"]
    parts   = [f"<ItemID>{item_id}</ItemID>"]

    if "title" in args:
        parts.append(f"<Title>{args['title']}</Title>")
    if "start_price" in args:
        cur = args.get("currency", "USD")
        parts.append(f'<StartPrice currencyID="{cur}">{args["start_price"]}</StartPrice>')
    if "quantity" in args:
        parts.append(f"<Quantity>{args['quantity']}</Quantity>")
    if "description" in args:
        parts.append(f"<Description><![CDATA[{args['description']}]]></Description>")

    if len(parts) == 1:
        return "No fields to revise were provided."

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    {''.join(parts)}
  </Item>
</ReviseItemRequest>"""
    root = _call("ReviseItem", xml)
    _check(root)
    changed = [k for k in ("title", "start_price", "quantity", "description") if k in args]
    return f"Item {item_id} revised successfully. Updated: {', '.join(changed)}"


def _get_item(args: dict) -> str:
    item_id = args["item_id"]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    root = _call("GetItem", xml)
    _check(root)
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
    per_page  = min(args.get("entries_per_page", 100), 200)
    active    = args.get("active_list", True)
    sold      = args.get("sold_list", True)
    unsold    = args.get("unsold_list", False)

    def _pag():
        return (
            f"<Pagination>"
            f"<EntriesPerPage>{per_page}</EntriesPerPage>"
            f"<PageNumber>1</PageNumber>"
            f"</Pagination>"
        )

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  {'<ActiveList><Include>true</Include>' + _pag() + '</ActiveList>' if active else ''}
  {'<SoldList><Include>true</Include>' + _pag() + '</SoldList>' if sold else ''}
  {'<UnsoldList><Include>true</Include>' + _pag() + '</UnsoldList>' if unsold else ''}
</GetMyeBaySellingRequest>"""
    root = _call("GetMyeBaySelling", xml)
    _check(root)

    sections = []

    if active:
        items = root.findall(".//ActiveList/ItemArray/Item")
        if items:
            lines = [f"=== ACTIVE LISTINGS ({len(items)}) ==="]
            for it in items:
                lines.append(
                    f"  {_t(it,'ItemID')} | {_t(it,'Title')[:55]} | "
                    f"${_t(it,'SellingStatus/CurrentPrice')} | "
                    f"Qty left: {_t(it,'QuantityAvailable')}"
                )
            sections.append("\n".join(lines))
        else:
            sections.append("No active listings.")

    if sold:
        txns = root.findall(".//SoldList/OrderTransactionArray/OrderTransaction/Transaction")
        if txns:
            lines = [f"=== SOLD ITEMS ({len(txns)}) ==="]
            for t in txns:
                lines.append(
                    f"  {_t(t,'Item/ItemID')} | {_t(t,'Item/Title')[:50]} | "
                    f"${_t(t,'TransactionPrice')} x {_t(t,'QuantityPurchased')} | "
                    f"Buyer: {_t(t,'Buyer/UserID')}"
                )
            sections.append("\n".join(lines))
        else:
            sections.append("No recently sold items.")

    if unsold:
        items = root.findall(".//UnsoldList/ItemArray/Item")
        if items:
            lines = [f"=== UNSOLD ITEMS ({len(items)}) ==="]
            for it in items:
                lines.append(f"  {_t(it,'ItemID')} | {_t(it,'Title')[:55]}")
            sections.append("\n".join(lines))
        else:
            sections.append("No unsold items.")

    return "\n\n".join(sections) if sections else "No data returned."


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
  <Pagination>
    <EntriesPerPage>{per_page}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
  <DetailLevel>ReturnAll</DetailLevel>
  <GranularityLevel>Fine</GranularityLevel>
</GetSellerListRequest>"""
    root = _call("GetSellerList", xml)
    _check(root)

    items = root.findall(".//ItemArray/Item")
    if not items:
        return "No listings found."
    total = _t(root, "TotalNumberOfEntries", "?")
    lines = [f"Listings — page {page}, showing {len(items)} of {total}:"]
    for it in items:
        lines.append(
            f"  {_t(it,'ItemID')} | {_t(it,'Title')[:55]} | "
            f"${_t(it,'SellingStatus/CurrentPrice')} | "
            f"Status: {_t(it,'SellingStatus/ListingStatus')}"
        )
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
  <Pagination>
    <EntriesPerPage>{per_page}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
</GetItemTransactionsRequest>"""
    root = _call("GetItemTransactions", xml)
    _check(root)

    txns = root.findall(".//TransactionArray/Transaction")
    if not txns:
        return f"No transactions found for item {item_id}."
    lines = [f"Transactions for item {item_id} ({len(txns)} found):"]
    for t in txns:
        lines.append(
            f"  TxID: {_t(t,'TransactionID')} | "
            f"Qty: {_t(t,'QuantityPurchased')} | "
            f"Price: ${_t(t,'TransactionPrice')} | "
            f"Buyer: {_t(t,'Buyer/UserID')} | "
            f"Date: {_t(t,'CreatedDate')}"
        )
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
  <Pagination>
    <EntriesPerPage>{per_page}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
</GetSellerTransactionsRequest>"""
    root = _call("GetSellerTransactions", xml)
    _check(root)

    txns = root.findall(".//TransactionArray/Transaction")
    if not txns:
        return "No transactions found."
    total = _t(root, "TotalNumberOfEntries", "?")
    lines = [f"Seller transactions — page {page}, showing {len(txns)} of {total}:"]
    for t in txns:
        lines.append(
            f"  Item: {_t(t,'Item/ItemID')} | {_t(t,'Item/Title')[:45]} | "
            f"Qty: {_t(t,'QuantityPurchased')} | "
            f"${_t(t,'TransactionPrice')} | "
            f"Buyer: {_t(t,'Buyer/UserID')} | "
            f"Date: {_t(t,'CreatedDate')}"
        )
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
  <Pagination>
    <EntriesPerPage>{per_page}</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
</GetOrdersRequest>"""
    root = _call("GetOrders", xml)
    _check(root)

    orders = root.findall(".//OrderArray/Order")
    if not orders:
        return "No orders found."
    total = _t(root, "TotalNumberOfEntries", "?")
    lines = [f"Orders — page {page}, showing {len(orders)} of {total}:"]
    for o in orders:
        lines.append(
            f"  OrderID: {_t(o,'OrderID')} | "
            f"Status: {_t(o,'OrderStatus')} | "
            f"Total: ${_t(o,'Total')} | "
            f"Buyer: {_t(o,'BuyerUserID')} | "
            f"Created: {_t(o,'CreatedTime')}"
        )
    return "\n".join(lines)


def _get_user_profile(args: dict) -> str:
    user_id = args["user_id"]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <UserID>{user_id}</UserID>
</GetUserRequest>"""
    root = _call("GetUser", xml)
    _check(root)
    u = root.find("User")
    if u is None:
        return "User not found."
    return "\n".join([
        f"Username:       {_t(u, 'UserID')}",
        f"Feedback Score: {_t(u, 'FeedbackScore')}",
        f"Positive (%):   {_t(u, 'PositiveFeedbackPercent')}",
        f"Registration:   {_t(u, 'RegistrationDate')}",
        f"Status:         {_t(u, 'Status')}",
        f"Site:           {_t(u, 'Site')}",
    ])


def _get_feedback(args: dict) -> str:
    user_id       = args.get("user_id", "")
    fb_type       = args.get("feedback_type", "FeedbackReceivedAsSeller")
    per_page      = min(args.get("entries_per_page", 25), 100)
    user_tag      = f"<UserID>{user_id}</UserID>" if user_id else ""

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetFeedbackRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  {user_tag}
  <FeedbackType>{fb_type}</FeedbackType>
  <Pagination>
    <EntriesPerPage>{per_page}</EntriesPerPage>
    <PageNumber>1</PageNumber>
  </Pagination>
  <DetailLevel>ReturnAll</DetailLevel>
</GetFeedbackRequest>"""
    root = _call("GetFeedback", xml)
    _check(root)

    entries = root.findall(".//FeedbackDetailArray/FeedbackDetail")
    total   = _t(root, "FeedbackSummary/TotalPositiveFeedbackEntries", "?")
    score   = _t(root, "FeedbackScore", "?")

    lines = [f"Feedback score: {score}  |  Positive total: {total}"]
    for e in entries:
        lines.append(
            f"  [{_t(e,'Role')}] {_t(e,'CommentType')} — "
            f"\"{_t(e,'CommentText')}\" — "
            f"from {_t(e,'CommentingUser')} on {_t(e,'CommentTime')}"
        )
    return "\n".join(lines) if lines else "No feedback entries found."


# ── HTTP / SSE transport ───────────────────────────────────────────────────────

sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request) -> Response:
    """SSE endpoint — Claude connects here."""
    if MCP_AUTH_TOKEN:
        auth_header = request.headers.get("Authorization", "")
        auth_param  = request.query_params.get("token", "")
        if auth_header != f"Bearer {MCP_AUTH_TOKEN}" and auth_param != MCP_AUTH_TOKEN:
            return Response("Unauthorized", status_code=401)

    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0], streams[1],
            server.create_initialization_options(),
        )
    return Response()


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "ebay-mcp", "version": "1.0.0"})


app = Starlette(
    routes=[
        Route("/health",    endpoint=health),
        Route("/sse",       endpoint=handle_sse),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
)


if __name__ == "__main__":
    if not EBAY_IAF_TOKEN:
        print("WARNING: EBAY_IAF_TOKEN is not set. All eBay calls will fail.")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
