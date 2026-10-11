# Angel One SmartAPI — Market Data WebSocket / REST research findings

Date of verification: 2026-10-11. All facts below were read today from official sources only:

- Official docs portal (React SPA): <https://smartapi.angelbroking.com/docs> — page content is compiled into
  `https://smartapi.angelbroking.com/static/js/main.7b870e66.js`; every quoted string below was extracted from that
  bundle (sections `/docs/WebSocket2`, `/docs/Historical`, `/docs/MarketData`, `/docs/Instruments`, `/docs/RateLimit`,
  `/docs/WebSocketOrderStatus`). Canonical rate-limit page link given in docs: <https://smartapi.angelone.in/docs/RateLimit>.
- Official Python SDK (GitHub org `angel-one`, branch `main`):
  - <https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py>
  - <https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartApiWebsocket.py> (legacy V1)
  - <https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py>
  - <https://github.com/angel-one/smartapi-python/blob/main/example/smartwebsocketexamplev2.py>
  - PyPI wheel `smartapi_python-1.5.5-py3-none-any.whl` (files.pythonhosted.org) — inspected `SmartApi/*`
- Official Node SDK: <https://github.com/angel-one/smartapi-javascript> (`config/constant.js`, `lib/websocket2.0.js`)
- Official Go SDK: <https://github.com/angel-one/smartapigo> (`websocket/websocket.go`) — implements legacy V1 only
- Live file verified by direct download: <https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json>
  (HTTP 200, `application/json`, 35,019,053 bytes, 149,479 records)

Docs bundle note: the docs portal ships current documentation under the post-Angel/Marg-migration domains
(`*.angelone.in`); REST base host in docs and SDK is `https://apiconnect.angelone.in`
(<https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py> `_rootUrl`).

---

## 1. WebSocket connection (market feed)

Current official (V2, "smart-stream"):

- URL (docs "WebSocket URL"): `wss://smartapisocket.angelone.in/smart-stream`
  (same constant in SDK: `SmartWebSocketV2.ROOT_URI = "wss://smartapisocket.angelone.in/smart-stream"` and Node SDK
  `CONSTANTS.websocketURL`).
- Authentication is via **WebSocket handshake HTTP headers**, not a JSON connect message (docs table "Request Headers
  for Authentication"; SDK `connect()` builds the same headers):
  - `Authorization` — "jwt auth token received from Login API" (mandatory)
  - `x-api-key` — "API key" (mandatory)
  - `x-client-code` — "client code (Angel One trading account id)" (mandatory)
  - `x-feed-token` — "feed token received from Login API" (mandatory)
- Browser clients cannot set headers; docs give a query-param form:
  `wss://smartapisocket.angelone.in/smart-stream?clientCode=&feedToken=&apiKey=` (params `clientCode`, `feedToken`, `apiKey`, all mandatory). Note: `jwtToken` is NOT in this query string per docs — unverified how the JWT is supplied in browser mode.
- Auth errors: docs "Response Headers for Authentication Errors": with HTTP 401 the response header `x-error-message`
  contains e.g. "Invalid Header - Invalid Auth token", "Invalid Header - Invalid Client Code", "Invalid Header -
  Invalid API Key", "Invalid Header - Invalid Feed Token".

Legacy V1 (still shipped in PyPI SDK as `SmartApi.smartApiWebsocket.SmartWebSocket`, but **no market-feed V1 URL
appears anywhere in the current docs bundle**):

- URL: `wss://wsfeeds.angelbroking.com/NestHtml5Mobile/socket/stream`
  (<https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartApiWebsocket.py>).
- Connect request is a JSON **text** frame sent after open (SDK `SmartWebSocket._subscribe_on_open`):
  `{"task":"cn","channel":"NONLM","token":<feedToken>,"user":<clientCode>,"acctid":<clientCode>}`
  (Go SDK sends the same shape: <https://github.com/angel-one/smartapigo/blob/main/websocket/websocket.go> line ~182).

Order-status socket (separate, not market data): `wss://tns.angelone.in/smart-order-update`, header
`Authorization: Bearer AUTHORIZATION_TOKEN`, "Each client code is limited to 3 connections"; errors: 401 invalid
token, 403 expired token, 429 connection-limit breached (docs `/docs/WebSocketOrderStatus`; SDK
`SmartApi/smartWebSocketOrderUpdate.py` `WEBSOCKET_URI`).

## 2. Subscription message (V2 smart-stream)

Exact JSON shape (docs "Request Contract" sample, verbatim structure; matches SDK `subscribe()`):

```json
{
  "correlationID": "abcde12345",
  "action": 1,
  "params": {
    "mode": 1,
    "tokenList": [
      { "exchangeType": 1, "tokens": ["10626", "5290"] },
      { "exchangeType": 5, "tokens": ["234230", "234235", "234219"] }
    ]
  }
}
```

Docs "Field Description" table (verbatim values):

- `correlationID` — String, optional; 10-char alphanumeric echoed back in error responses.
- `action` — Integer, mandatory: `1 (Subscribe)`, `0 (Unsubscribe)`.
- `mode` — Integer, mandatory: `1 (LTP)`, `2 (Quote)`, `3 (Snap Quote)` — **these three only are in the docs table**.
- `tokenList[].exchangeType` — Integer: `1 (nse_cm)`, `2 (nse_fo)`, `3 (bse_cm)`, `4 (bse_fo)`, `5 (mcx_fo)` (docs);
  SDK/Node add `7 (ncx_fo)`, `13 (cde_fo)` as constants
  (`SmartWebSocketV2.NCX_FO=7, CDE_FO=13`; Node `EXCHANGES`).
- `tokenList[].tokens` — List of **strings** (instrument tokens as strings; NO quantity field anywhere in V2).

Mode enum — correction of the question's premise:

- Official Python SDK `SmartWebSocketV2` class constants: `LTP_MODE = 1`, `QUOTE = 2`, `SNAP_QUOTE = 3`, `DEPTH = 4`,
  `SUBSCRIPTION_MODE_MAP = {1:"LTP", 2:"QUOTE", 3:"SNAP_QUOTE", 4:"DEPTH"}`; Node SDK `config/constant.js`
  `MODE = {LTP:1, Quote:2, SnapQuote:3, Depth:4}`.
- **There is no "RealTime", "Candle(1m)", "Candle(3m)" mode anywhere in official docs or any official SDK (Python,
  Go, Node).** `mode 1 = RealTime / mode 2 = Candle(1m)` is NOT DOCUMENTED for SmartAPI — it does not exist in the
  current official sources; the socket cannot deliver candle history. Candle history is REST-only
  (`getCandleData`). (Grep of docs bundle and all SDK sources for `protobuf|FeedResponse|CandleResponse` → 0 hits.)
- Instruments per subscription message: no per-message cap is documented. Server enforces a session-wide quota of
  1000 token-mode subscriptions (see §3); SDK client-side caps DEPTH mode (4) at **50 tokens total** per request
  (`quota_limit = 50`, raises "Quota exceeded: You can subscribe to a maximum of 50 tokens only."), and the SDK
  rejects any DEPTH token whose `exchangeType != 1` ("...it support only 1 exchange type").
- V1 legacy subscribe frame (text JSON): `{"task":"mw","channel":"nse_cm|2885&nse_cm|1594&nse_cm|11536","token":<feedToken>,"user":<clientCode>,"acctid":<clientCode>}`;
  tasks documented in the (legacy) docs table: `cn` (connect), `hb` (heartbeat), `mw` (market watch), `sfi`
  (subscribe index), `dp` (market depth).

## 3. Limits

Subscription / connection limits (docs, verbatim):

- "The total limit/quota of token subscriptions is **1000 per WebSocket session**."
  Counting rule (docs): "If the client subscribes to Infosys NSE with LTP, Quote, and SnapQuote mode then this will be
  counted as 3 subscriptions"; "Duplicate subscriptions to the same token and mode will be gracefully ignored and will
  not be counted towards the quota"; "The client will receive one tick per token_mode combination"; recommendation:
  "subscribe to one mode at a time for a token".
- "Each client code (Angel One trading account id) can have up to **three concurrent WebSocket connection**" (V2 docs
  Features; order-update docs repeat "limited to 3 connections", 429 on breach).
- Error responses on the socket are JSON text frames: `{"correlationID":"abcde12345","errorCode":"E1002","errorMessage":"Invalid Request. Subscription Limit Exceeded"}`; codes `E1001` "Invalid Request Payload.", `E1002` "Invalid Request. Subscription Limit Exceeded." (docs).
- DEPTH mode 50-token cap: SDK-enforced only (Python + Node); not in docs tables.

REST rate limits (official table at docs "Rate Limit" page / <https://smartapi.angelone.in/docs/RateLimit>;
"Rate limiting defines limits on how many API calls can be made within a second. The limit-exceeding requests will
fail and returns 403... The Rate limit is calculated on the basis of client code."):

| Endpoint | /sec | /min | /hour |
|---|---|---|---|
| `/rest/secure/angelbroking/historical/v1/getCandleData` | **3** | **120** | **5000** |
| `/rest/secure/angelbroking/market/v1/quote` | **10** | **500** | **5000** |
| `/rest/secure/angelbroking/order/v1/getLtpData` | 10 | 500 | 5000 |
| `/rest/secure/angelbroking/order/v1/searchScrip` | 1 | NA | NA |
| place/modify/cancelOrder | 9 (cumulative across the three) | 500 | 1000 |
| loginByPassword | 1 | NA | NA |
| jwt generateTokens | 1 | NA | 1000 |

- Quote body text additionally says: "The market data API allows you to fetch data for **50 symbols in just one
  request** with a rate limit of 1 request per second" (docs `/docs/MarketData`) — this "1 req/sec" prose conflicts
  with the rate-limit table's 10/sec for the same endpoint; both are official. Treat 1/sec as the safe bound.
- **BlockMarket endpoint: NOT DOCUMENTED.** The current docs bundle contains zero occurrences of "blockMarket"/"Block Market"; it is not in the Python SDK routes either.
- getCandleData also has per-request range caps (docs "Historical API", "Max Days in one Request"):
  ONE_MINUTE=30 days, THREE_MINUTE=60, FIVE_MINUTE=100, TEN_MINUTE=100, FIFTEEN_MINUTE=200, THIRTY_MINUTE=200,
  ONE_HOUR=400, ONE_DAY=2000.

## 4. Feed payload format

V2 smart-stream (current, official):

- "Simplified and consistent request (JSON) and **response (binary)** structure." (docs Features).
- "Response is in binary format with **Little Endian** byte order." (docs Response Contract; SDK
  `LITTLE_ENDIAN_BYTE_ORDER = "<"`, `struct.unpack`).
- It is **NOT protobuf and NOT gzip**. It is a flat little-endian binary struct. (No protobuf library dependency in
  SDK; `gzip` appears in docs only in the instruments-master sentence; V1 payloads were base64+**zlib**-compressed
  JSON text.)
- Exact response field layout (docs "Section-1) Payload" table, positions verified identical in SDK `_parse_binary_data`):

| # | Field | Type | Pos | Notes (docs) |
|---|---|---|---|---|
| 1 | Subscription Mode | byte/int8 | 0 | `1 (LTP)`, `2 (Quote)`, `3 (Snap Quote)` |
| 2 | Exchange Type | byte/int8 | 1 | `1 nse_cm, 2 nse_fo, 3 bse_cm, 4 bse_fo, 5 mcx_fo` |
| 3 | Token | byte array (25) | 2 | utf-8 chars, null-terminated (`\0`) |
| 4 | Sequence Number | int64 | 27 | docs note: "Sequence number for index feed is not available. please ignore..." |
| 5 | Exchange Timestamp | int64 | 35 | "Exchange feed timestamp in **epoch milliseconds**" |
| 6 | Last Traded Price (LTP) | int32 (8-byte slot) | 43 | "If mode is ltp, the packet ends here. packet size = 51 bytes". "**All prices are in paise.** For currencies divide int32 price by 10000000.0 (4 dp); for everything else divide by 100." |
| 7 | Last traded quantity | int64 | 51 | |
| 8 | Average traded price | int64 | 59 | |
| 9 | Volume traded for the day | int64 | 67 | mFeed name `lVolumeTradedToday` |
| 10 | Total buy quantity | double | 75 | |
| 11 | Total sell quantity | double | 83 | |
| 12 | Open price of the day | int64 | 91 | |
| 13 | High price of the day | int64 | 99 | |
| 14 | Low price of the day | int64 | 107 | |
| 15 | Close price | int64 | 115 | "If mode is quote, the packet ends here. packet size = 123 bytes" |
| 16 | Last traded timestamp | int64 | 123 | |
| 17 | Open Interest | int64 | 131 | |
| 18 | Open Interest change % | double | 139 | docs: "this is a **dummy field. contains garbage value**" |
| 19 | Best Five Data | 10 × 20-byte packets | 147 | "sequence of best five buys data, followed by best five sells" |
| 20 | Upper circuit limit | (int64) | 347 | |
| 21 | Lower circuit limit | (int64) | 355 | |
| 22 | 52 week high price | (int64) | 363 | |
| 23 | 52 week low price | (int64) | 371 | "If mode is snapquote, the packet ends here. packet size = 379 bytes" |

- "Section-2) Best Five Data" per 20-byte packet: `Buy/Sell Flag` int16 pos 0 — docs values `1 (buy)`, `0 (sell)`;
  `Quantity` int64 pos 2; `Price` int64 pos 10; `Number of Orders` int16 pos 18. (NOTE: Python SDK maps `flag==0` to
  buy and also cross-swaps the buy/sell output keys — contradicting docs; see UNVERIFIED.)
- Python SDK parsed key names (`_parse_binary_data`): `subscription_mode`, `exchange_type`, `token`,
  `sequence_number`, `exchange_timestamp`, `last_traded_price`, `last_traded_quantity`, `average_traded_price`,
  `volume_trade_for_the_day`, `total_buy_quantity`, `total_sell_quantity`, `open_price_of_the_day`,
  `high_price_of_the_day`, `low_price_of_the_day`, `closed_price`, `last_traded_timestamp`, `open_interest`,
  `open_interest_change_percentage`, `upper_circuit_limit`, `lower_circuit_limit`, `52_week_high_price`,
  `52_week_low_price`, `best_5_buy_data`, `best_5_sell_data`, `subscription_mode_val`.
- DEPTH mode (4, SDK/Node only, undocumented in docs table): SDK drops `sequence_number`/`last_traded_price`, keeps
  `packet_received_time` at pos 35 (docs NOTE: "Packet Received Time is a dummy value and will be deprecated soon"),
  then 20 levels: per level 10 bytes — quantity int32, price int32, num_of_orders int16; buys at offset 0..199, sells
  at 200..399 from depth block start (`_parse_depth_20_buy_and_sell_data`).
- JSON/text on the socket: yes, control traffic is plain text — subscription **requests** are JSON text frames,
  error responses are JSON text frames, heartbeat is text `ping`/`pong`. The **market data response is binary only**;
  there is no documented JSON payload mode for V2. (V1 legacy feed was the JSON option: text frames, base64 → zlib →
  JSON objects, fields `tk`, `e`, `ltp`, `c`, `nc`, `cng`, `v`, `bq`, `bp`, `bs`, `sp`, `ltq`, `ltt`, `ucl`, `tbq`,
  `mc`, `lo`, `yh`, `op`, `ts`, `h`, `lcl`, `tsq`, `ap` ("Vwap Average Price"), `yl`, `oi`, `isdc`, `to`, `toi`,
  `lter`, `hter`, `setltyp`; heart reply `{"ak":"ok|nk","msg":"heartbeat","task":"hb"}` — legacy docs table, no URL.)

REST quote response fields (docs `/docs/MarketData`, mode LTP/OHLC/FULL; request
`{"mode":"FULL","exchangeTokens":{"NSE":["3045","881"],"NFO":["58662"]}}`): `success`, `message`, `errorCode`,
`data.fetched[]` with `exchange`, `tradingSymbol`, `symbolToken`, `ltp`, `open`, `high`, `low`, `close` (previous
close), `lastTradeQty`, `exchFeedTime`, `exchTradeTime`, `netChange`, `percentChange`, `avgPrice`, `tradeVolume`,
`opnInterest`, circuit limits, 52-week high/low and `depth.buy[n].price|quantity|orders`, `depth.sell[...]`.

## 5. Volume semantics

- Live socket (V2 Quote/SnapQuote): the field is literally named "**Volume traded for the day**" (docs; mFeed
  `lVolumeTradedToday`; SDK key `volume_trade_for_the_day`). Semantics = **cumulative total volume for the trading
  day**, not per-interval and not since-connect. (There are no candle packets on the socket at all — see §2/§4.)
- `last_traded_quantity` (pos 51) is the qty of the last trade only.
- REST `getCandleData`: docs state each record is `[timestamp, open, high, low, close, volume]` — one row per candle,
  so `volume` is the volume **of that candle interval** (per-row), which is the natural reading of "record per
  interval"; the docs do not add an explicit "per-candle volume" sentence, and never state the unit (shares vs lots) →
  unit is UNVERIFIED (see below). Sample record in docs (index):
  `["2023-09-06T11:15:00+05:30", 19571.2, 19573.35, 19534.4, 19552.05, 0]`.
- V1 legacy field `"v"` = "Volume" (docs legacy script-feed table) — no further semantics documented.

## 6. Timestamps in the live feed

- V2 binary: `Exchange Timestamp` (pos 35) — "Exchange feed timestamp in **epoch milliseconds**" (docs). Timezone:
  epoch/UTC by definition; docs give no candle semantics because there are no candles. `last_traded_timestamp`
  (pos 123, SnapQuote only) = exchange trade time. `packet_received_time` (depth) is documented as dummy.
- REST candles: ISO-8601 strings **with explicit IST offset**, e.g. `"2023-09-06T11:15:00+05:30"`; request dates use
  format `"yyyy-MM-dd hh:mm"` (docs NOTE: 'In Get Candle Data Request fromdate and todate format should be
  "yyyy-MM-dd hh:mm"'). Whether the record timestamp is the candle **open** or **close** time is NOT DOCUMENTED
  (community consensus says open time — UNVERIFIED, see below).

## 7. Heartbeat / reconnect rules

- V2 docs "Heartbeat message" (verbatim): "The client must send a heartbeat message for each WebSocket connection
  **every 30 sec** to keep the connection alive." Heartbeat request (text): `ping`. Heartbeat response (text):
  `pong`.
- Python SDK V2: `HEART_BEAT_MESSAGE = "ping"`, `HEART_BEAT_INTERVAL = 10` and it runs
  `websocket-client ... run_forever(ping_interval=HEART_BEAT_INTERVAL)` (protocol-level ping frames, not the literal
  text `"ping"` message the docs describe; SDK `_on_message` special-cases text `"pong"`; commit `2025-02-07 "ping_payload issue fix"` on main — mismatch between docs and SDK ping type is UNVERIFIED-needs-live-testing).
- Node SDK V2: sends the **text** frame `'ping'` every `CONSTANTS.Interval = 10000` ms (10 s) — inside the docs' 30 s
  requirement.
- V1 SDK: heartbeat JSON text frame `{"task":"hb","channel":"","token":...,"user":...,"acctid":...}` every 30 s
  (`smartApiWebsocket.HB_INTERVAL=30`) or every 60 s in the twisted `webSocket.py` variant.
- Reconnect: V2 SDK constructor `max_retry_attempt=1, retry_strategy (0=simple/1=exponential), retry_delay=10 s,
  retry_multiplier=2, retry_duration=60`; on error it sets `RESUBSCRIBE_FLAG` and replays stored subscriptions
  (`resubscribe()` re-sends the recorded `action/mode/tokenList`). V1 Go/Python factory defaults: maxRetries 10 / up
  to 300, exponential delay max 60 s. Documented breach behaviour: 429 on >3 concurrent connections (order-status
  docs); no documented idle timeout for the market socket.
- Static IP: the SmartAPI dashboard API-key registration form requires **"Primary Static IP"** (mandatory; "Primary IP
  can be updated once in a week only") and optional "Secondary Static IP" (docs-portal bundle). Enforcement text
  (docs placeOrder section, verbatim): "For users utilizing the new static IP–based API key, the source IP address of
  **Place order, Modify order, Cancel order** api requests will be validated against the static IP registered by the
  user." **No static-IP requirement is documented for the market-data WebSocket or market-data REST endpoints.**
  Practical caveat: you still need a stable IP because the registered key is bound to your primary/secondary IPs.

## 8. Instrument master (OpenAPIScripMaster.json)

- Official docs URL (verbatim): `https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json`
  — docs also reference the `margincalculator.angelone.in` variant; both live-downloaded today. Docs sentence: "The
  instrument list API returns a gzipped CSV dump... The dump is generated once every day" (actual file served today is
  plain JSON `application/json`, not gzipped, not CSV — docs wording is stale).
- Exact field names (verified from the live file, 149,479 records, keys identical in every row, and from the docs
  sample): `token`, `symbol`, `name`, `expiry`, `strike`, `lotsize`, `instrumenttype`, `exch_seg`, `tick_size`,
  `freeze_qty`, `is_cas_enabled` (all strings except `is_cas_enabled` bool).
- NSE equity identification (verified from live file): the current file has **no `EQ` / `NSE_EQ` instrumenttype
  values** (count = 0). Equities are the rows with `exch_seg == "NSE"` and `symbol` ending in `-EQ`,
  `instrumenttype == ""`, `name == <base symbol>`. Examples:
  `{"token":"2885","symbol":"RELIANCE-EQ","name":"RELIANCE","expiry":"","strike":"-1.000000","lotsize":"1","instrumenttype":"","exch_seg":"NSE","tick_size":"5.000000","freeze_qty":"81195","is_cas_enabled":true}`;
  BSE cash example: `{"token":"500325","symbol":"RELIANCE",...,"exch_seg":"BSE"}`. NSE segment contents today:
  10,053 rows, of which 2,725 end with `-EQ`; the rest include indices (`instrumenttype "AMXIDX"`, e.g. token
  `"99926000"` "Nifty 50") — so filter on the `-EQ` suffix, not just `exch_seg=="NSE"`. Docs sample row shows
  `"exch_seg":"nse_cm"` for RELIANCE-EQ while the live file shows `"NSE"` → casing drift, UNVERIFIED which casing is
  guaranteed.
- The `token` is the websocket `symboltoken` (docs: "tokens to subscribe. Refer Master Scrip for token by stock";
  e.g. RELIANCE `2885`, SBIN `3045` in docs samples).

## 9. smartapi-python SDK status

- PyPI: project name **`smartapi-python`** (import package `SmartApi`), latest release **1.5.5**, uploaded
  **2025-02-07** (<https://pypi.org/pypi/smartapi-python/json>). Prior releases: 1.5.4/1.5.3 2025-01-23, 1.5.2
  2024-12-09, 1.5.1 2024-09-26, ... Author `ab-smartapi <smartapi.sdk@gmail.com>`, homepage
  `https://github.com/angelbroking-github/smartapi-python`. Note: wheel `SmartApi/version.py` still says
  `__version__ = "1.5.3"` (stale metadata); classifiers still claim Python 2.7–3.7 (stale).
- GitHub: `angel-one/smartapi-python` (fork of `L30705/smartapi-python`, itself of `angelbroking-github/smartapi-python`),
  default branch `main`, last commit 2025-05-27 ("Merge PR #60 PlaceOrderFix"); releases and commits roughly track the
  Jan–May 2025 period. **Maintenance verdict: low activity / lagging** (~20 months without a release as of Oct 2026);
  the docs portal + `*.angelone.in` endpoints are the more authoritative moving source. No `angel-one/smartapi-go` or
  `angel-one/smartapi-nodejs` repos exist — the official ones are `angel-one/smartapigo` (V1-style feed only,
  last push 2025-05-26) and `angel-one/smartapi-javascript` (has V2 client).
- Read-only market-data surface (usable without placing anything):
  - `SmartApi.smartWebSocketV2.SmartWebSocketV2(auth_token, api_key, client_code, feed_token)` — `connect()`,
    `subscribe(correlation_id, mode, token_list)`, `unsubscribe(...)`, `close_connection()`, callbacks `on_open`,
    `on_data`, `on_control_message`, `on_message`, `on_error`, `on_close`. Pure data plane; no order calls.
  - `SmartApi.smartConnect.SmartApiSmartAPIRestAPI` read-only methods: `generateSession`/`generateToken` (auth, still
    required), `getCandleData(historicDataParams)`, `getOIData(...)`, `getMarketData(mode, exchangeTokens)`,
    `getLtpData(params)`, `searchScrip(exchange, searchscrip)`, `getProfile(refreshToken)`, `getMarginApi`, RMS
    limits, holdings/positions/tradebook getters.
  - Confirmed the SDK **does** expose trading endpoints we will deliberately never call: `placeOrder`,
    `placeOrderFullResponse`, `modifyOrder`, `cancelOrder`, `getOrderBook`, GTT `createRule/modifyRule/cancelRule`,
    `convertPosition`, plus `SmartApi.smartWebSocketOrderUpdate` (order status socket). Read-only usage is fully
    feasible — none of those are imported/needed by `smartWebSocketV2`.

---

## UNVERIFIED — needs live testing

1. **Candle timestamp meaning in REST `getCandleData`** — whether `[timestamp,...]` is the candle open time or close
   time is not documented anywhere official (samples suggest open-time alignment at `09:00`, `11:15`). Verify against
   NSE timings.
2. **REST candle volume unit** — shares vs lots (lotsize from master) is not documented; verify per symbol (equities
   lotsize=1 usually, but docs sample uses `"lotsize":"1"` only for cash; futures differ).
3. **Best-five buy/sell flag contradiction** — docs Section-2 says flag `1 = buy`, `0 = sell`; the Python SDK treats
   `flag == 0` as buy and additionally cross-swaps `best_5_buy_data`/`best_5_sell_data` on output. Only live capture
   against a known order book (e.g. quote REST `depth.buy` for the same token) can settle this.
4. **Ping mechanism** — docs require a **text** frame `"ping"` every ≤30 s; the Python SDK uses websocket-client's
   **protocol-level** ping (10 s) plus `on_ping/on_pong` handlers instead. Confirm the server tolerates the SDK
   approach / whether the text-frame ping is enforced (expect silent disconnect after ~30-60 s idle otherwise).
5. **Browser query-param auth** (`?clientCode=&feedToken=&apiKey=`) — how `Authorization` JWT is conveyed in that
   mode is not documented (the param list omits the JWT); irrelevant if we use headers, but shows docs gaps.
6. **DEPTH mode (4)** — not in the docs mode table (docs list 1/2/3 only); exists in Python/Node SDKs with a 50-token
   client-side cap, NSE-only validation, 20-level little-endian layout. Wire layout must be confirmed live.
7. **Scrip-master `exch_seg` casing** (`"NSE"` live vs `"nse_cm"` in docs sample) and stability of the `symbol`
   `-EQ` suffix as the equity-delivery discriminator.
8. **Tick rate / throttling on the V2 socket** (ticks per second per subscription, burst behaviour around open/close)
   — NOT DOCUMENTED anywhere.
9. Whether the 1000-subscription quota is per connection or per client code across the 3 connections — docs say "per
   WebSocket session" literally; test with 2 simultaneous sessions.
10. **Quote REST "1 request per second" prose vs "10/sec" in the rate table** — both official; empirically determine
    which applies to `/market/v1/quote`.

---

## CONSEQUENCES FOR OUR WORKER

1. **No candle mode on the socket.** The premise "mode 2 = Candle(1m)" is false for SmartAPI: documented socket modes
   are LTP(1), Quote(2), SnapQuote(3) (+ SDK-only Depth(4)). For 1-minute candles we must either
   (a) subscribe **LTP mode** (51-byte packets, cheapest) and bucket ticks into 1-minute OHLCV client-side, using
   `exchange_timestamp` (epoch ms) as the trade timestamp, or
   (b) poll REST `getCandleData` `ONE_MINUTE` after each minute boundary (3 req/s, 120 req/min per client-code; 30
   days max range per request; 500 instruments at 1 request/token/request ⇒ ~28 req for 500 tokens per minute only if
   batching exists — the documented request shape is **one** `symboltoken` per request, so 500 tokens = 500
   req/min ⇒ exceeds 120/min — REST-only backfill must therefore be limited to a rolling subset or use the socket).
   Recommendation: socket LTP/Quote as primary; REST candles only for catch-up after connect/reconnect.
2. **500 instruments fit comfortably** under the documented 1000 token-mode subscriptions per session — *provided we
   use a single mode* (e.g. LTP only). 500 tokens × {LTP+Quote+SnapQuote} = 1500 > 1000 would breach the quota;
   duplicates in the same mode are ignored, and quota is counted per (token, mode).
3. **Volume deltas:** the socket does not provide per-interval volume. In Quote/SnapQuote, `volume_trade_for_the_day`
   (pos 67) is day-cumulative (`lVolumeTradedToday`) → per-minute candle volume =
   `cum(t_end) - cum(t_start)`, clamped at ≥0, with a reset to 0 at session start (per-day roll) and special handling
   of the first tick after connect (we only know the cumulative value, not the candle start volume → seed the minute
   bucket with the delta from the previous tick, and prefer REST `getCandleData` for the very first partial minute).
   In LTP mode there is no volume field at all — tick-based volume requires Quote mode (which carries
   `last_traded_quantity` and `volume_trade_for_the_day`).
4. **Price scaling:** divide all int prices by 100 (paise) for NSE equities (only currencies use 1e7); `oi change %`
   is garbage per docs; sequence numbers are absent for index feeds.
5. **Ops:** heartbeat must be a text `"ping"` ≤ every 30 s (SDK does protocol pings @10 s — verify live); plan for
   exactly ≤3 concurrent sockets per client code; on quota errors parse JSON `E1002`; the API key is bound to a
   registered **static primary IP** (IP enforcement is documented for order APIs, but the registration field is
   mandatory for all API use) — keep the worker on a fixed egress IP. Use `*.angelone.in` hosts (docs current) not
   legacy `angelbroking.com` websocket URLs; the V1 `wsfeeds.angelbroking.com` endpoint is undeprecated-but-undocumented
   legacy and should be avoided.
