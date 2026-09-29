# Registration Proxy Pool

<p><a href="proxy-pool.md">简体中文</a> | <strong>English</strong></p>

The proxy pool is an optional network layer for the main registration flow. By default, `proxy_mode=auto` preserves the legacy single-proxy/direct behavior from older configurations. Account-level `ProxyLease`, node scheduling, probing, and health feedback are enabled only when `single` or `pool` is explicitly selected.

## Core Principles

- **One stable lease per account attempt**: the browser, email requests, registration-stage HTTP traffic, NSFW, and CPA/OIDC unless explicitly overridden all share the same network exit.
- **Safe retries take priority over blind replay**: a Lease may be released and the attempt restarted only before any stateful submission occurs. While waiting for a verification code, but before code submission starts, recovery may switch the email address while staying on the same Lease. Errors after stateful boundaries such as email submission, verification-code entry/submission, or profile submission are marked as outcome uncertain; the full registration flow is not automatically replayed through a different proxy.
- **All managed network components consume a unified HTTP-compatible endpoint**: HTTP/SOCKS/advanced protocols are ultimately presented in a form consumable by Chromium, curl_cffi, urllib, CPA, and probes.
- **Probe and Runtime Health are separate**: active probing answers "is it reachable right now?", while business health answers "how has it actually performed during real registrations?".
- **Fixed and Rotating are handled separately**: fixed nodes use Health/Failure/Cooldown; rotating gateways track exit successes/failures and success rate without cooling the entire gateway because of one bad exit.
- **Source-scoped Last-Known-Good**: file and subscription sources refresh independently; if one source temporarily fails to refresh, its most recent successful node set is retained.
- **Backward compatibility by default**: `auto` / `direct` continue to use the legacy path and do not force managed-proxy behavior.

## Unified Network Exit

In `single` / `pool` mode:

```text
plain HTTP (no auth)
    → original HTTP endpoint

HTTP + auth / HTTPS proxy / SOCKS4 / SOCKS5
    → LocalProxyBridge
    → http://127.0.0.1:<port>

VLESS / VMess / Trojan / Hysteria2 / TUIC / Shadowsocks
    → sing-box / mihomo
    → http://127.0.0.1:<port>
```

All traffic then flows through:

```text
ProxyLease.proxy_url
      ↓
Chromium / curl_cffi / Mail / NSFW / CPA OAuth / CPA Browser / Probe / Preflight
```

`ProxyLease.source_uri` retains the original node URI for logs, WebUI display, and diagnostics.

## Configuration

```json
{
  "proxy_mode": "auto",
  "proxy": "",
  "proxy_fallback": "none",

  "proxy_pool_file": "",
  "proxy_pool_subscription_url": "",
  "proxy_pool_subscription_proxy": "",
  "proxy_pool_subscription_public_only": false,

  "proxy_pool_store_file": "./proxy_pool.json",

  "proxy_pool_endpoint_mode": "auto",
  "proxy_pool_refresh_interval_sec": 900,
  "proxy_pool_probe_interval_sec": 900,
  "proxy_pool_probe_timeout_sec": 15,
  "proxy_pool_probe_provider": "cloudflare",
  "proxy_pool_probe_dual_stack": true,

  "proxy_pool_max_concurrent_per_node": 1,
  "proxy_pool_acquire_timeout_sec": 30,

  "proxy_protocol_backend": "auto",
  "proxy_singbox_path": "",
  "proxy_mihomo_path": "",
  "proxy_protocol_start_timeout_sec": 10,
  "proxy_runtime_idle_ttl_sec": 120,
  "proxy_runtime_cache_max": 32,

  "proxy_pool_persist_health": false,
  "proxy_pool_state_file": "./proxy_pool_state.json",
  "proxy_pool_preflight_enabled": true
}
```

### `proxy_mode`

| Value | Behavior |
| --- | --- |
| `auto` | Default compatibility mode; continues to use the legacy `proxy` behavior. |
| `direct` | Forces the main registration flow to connect directly. |
| `single` | Treats `proxy` as a single node managed by Lease and health logic. |
| `pool` | Loads and schedules multiple nodes from a file, a subscription and the JSON node list. |

### Node list (JSON)

The node list is a standalone JSON file; the nodes added or removed in the WebUI live there, and removals of file/subscription nodes are recorded in a `removed` map:

```json
{
  "version": 1,
  "updated_at": 1759...,
  "nodes": {
    "http://user:pass@127.0.0.1:7890": {
      "uri": "http://user:pass@127.0.0.1:7890",
      "canonical": "http://user:pass@127.0.0.1:7890",
      "node_id": "9f2c...",
      "origin": "user",
      "added_at": 1759...
    }
  },
  "removed": {
    "<canonical_uri>": {"removed_at": 1759...}
  }
}
```

```text
proxy_pool_store_file = ./proxy_pool.json
```

- Identity is the canonical URI: `HTTP://127.0.0.1:8001` and `http://127.0.0.1:8001` are the same node, so re-adding a node never creates a second entry (and clears its deletion record).
- `origin=user` is a node you added (removing it deletes the entry); file/subscription nodes are not owned by the store, so removing one records a deletion so the next source refresh does not re-add it (re-adding the same node clears the record).
- Add / remove take effect **immediately**: no source reload and no Manager rebuild is needed, and a running scheduler sees the new node on its next lease acquisition. Hand-edited (or other-process) changes are picked up on the next read.
- Writes are atomic (temp file + fsync + replace) with mode 0600; read-only usage never creates the file.
- When the file does not exist, `proxy_pool_manual_entries` / `proxy_pool_disabled_nodes` are imported once in memory (the first as nodes you added, the second as deletion records) and persisted on the first change. After that the JSON list is authoritative and the two legacy keys no longer take effect (they stay in the config for compatibility with old files).
- Removing every node never silently falls back to a direct connection: `snapshot().error` reports `代理池没有可用节点` / `代理池节点均已被移除: N 个节点在删除记录中` and `acquire()` raises it right away; an empty pool can still be extended by adding nodes.
- When `proxy_mode` is not `pool`, "Add proxy" in the WebUI switches the mode to `pool` and says so; the pool only participates in scheduling in `single` / `pool` mode.

### `proxy_fallback`

| Value | Behavior |
| --- | --- |
| `none` | No fallback when no usable node is available. |
| `direct` | A new attempt may fall back to a direct connection if Lease acquisition fails or times out. |
| `single` | A new attempt may fall back to `proxy` if Lease acquisition fails or times out. |

Fallback occurs only before a new account attempt begins. It never silently changes IP during an in-progress registration flow.

## Registration Safe-Retry Boundary

The managed registration flow tracks the current stage:

```text
lease_acquire
browser_start
page_open
email_submit
code_wait
code_submit
profile_submit
sso_wait
account_confirmed
postprocess
```

Retry rules:

```text
lease_acquire / browser_start / page_open
→ SAFE_NEW_LEASE
→ release the old Lease and restart is allowed

code_wait
→ SAME_LEASE_RECOVERY
→ no code typed or submitted yet: cool the current exit down, take a new Lease, and continue with another email

email_submit / code_submit / profile_submit / sso_wait
→ OUTCOME_UNCERTAIN
→ do not replay the full registration by switching email or proxy

account_confirmed / postprocess
→ NO_RETRY
→ a confirmed account is not registered again
```

This avoids replaying a registration through a new IP after a submission may already have reached the server but the local client lost the response, reducing duplicate accounts and duplicate submissions. Only when the flow is still at `code_wait` and no usable verification code has been obtained may recovery switch the email address while keeping the same account Lease. Once `code_submit` begins, an unconfirmed submission result is counted as outcome uncertain and email retries no longer bypass the safe-retry boundary.

The WebUI additionally tracks an `uncertain` count.

## Supported Protocols

Proxy sources may contain any mix of:

```text
HTTP / HTTPS
SOCKS / SOCKS4 / SOCKS4A / SOCKS5 / SOCKS5H
VLESS
VMess
Trojan
Hysteria2 / hy2
TUIC
Shadowsocks / ss
```

Advanced protocols are converted on demand into a local HTTP endpoint by sing-box or mihomo. `proxy_protocol_backend` picks the core:

| Value | Behavior |
| --- | --- |
| `auto` | Use `sing-box` first (`proxy_singbox_path` or `PATH`), otherwise `mihomo` (`proxy_mihomo_path` or `PATH`). |
| `sing-box` | sing-box only; a missing binary is an error. |
| `mihomo` | mihomo only; a missing binary is an error. |
| `native-only` | Disables advanced protocols; only native HTTP/HTTPS/SOCKS remain. |

Both cores expose exactly one local HTTP endpoint, so behavior above them is identical; only the generated configuration differs: sing-box uses JSON (`sing-box run -c`) while mihomo uses Clash configuration (`mihomo -d <directory>` with `config.yaml` inside, written as JSON since YAML is a superset of JSON). The configuration is validated before launch (`sing-box check` / `mihomo -t`); a rejection is recorded as that node's backend error and disables the node.

The `mihomo` backend does not support the `quic` / `httpupgrade` transports; such nodes fail with an explicit error telling you to use `proxy_protocol_backend=sing-box` instead of degrading silently. When `proxy_singbox_path` / `proxy_mihomo_path` are empty the binary is resolved from the system `PATH`; the project never downloads or updates it.

Shadowsocks supports common SIP002 / legacy Base64 URIs. The built-in implementation currently supports common AEAD / 2022 methods; unsupported plugins or methods produce explicit errors rather than silently degrading.

## Native URI Normalization

Native HTTP/HTTPS/SOCKS proxies must identify an explicit proxy endpoint:

```text
scheme://[user:password@]host:port
```

Rules:

- A port is required.
- Routing paths or query strings are not accepted.
- `#fragment` is used only as the display name and is excluded from the canonical URI / node identity.
- `{account}` may appear at most once and only in the proxy username.
- `socks://` is normalized to `socks5://`.

## SOCKS DNS Semantics

The shared bridge explicitly distinguishes:

```text
socks5://
→ resolve DNS locally
→ send the IP address to the SOCKS server

socks5h://
→ do not resolve the hostname locally
→ let the SOCKS server resolve the hostname
```

SOCKS4 / SOCKS4A likewise preserve local / remote DNS semantics respectively.

## Runtime Idle Cache

Runtimes are still created lazily; a large subscription does not cause a large number of bridge / sing-box / mihomo runtimes to start all at once.

After the reference count drops to 0, the runtime enters the idle cache by default:

```text
proxy_runtime_idle_ttl_sec = 120
proxy_runtime_cache_max = 32
```

If the same node is acquired again within the TTL, the runtime can be reused directly. Once the TTL expires or the idle-cache limit is exceeded, the least recently used runtime is cleaned up. Set `proxy_runtime_idle_ttl_sec=0` to restore immediate shutdown at zero references. Manager shutdown closes all remaining runtimes.

## Base64, Subscription Refresh, and Last-Known-Good

`proxy_pool_file` and `proxy_pool_subscription_url` support:

- Plain line-by-line URIs.
- Entire documents encoded with standard Base64.
- URL-safe Base64.
- Mixed multi-protocol nodes.

Each source is limited to 2 MiB and 10,000 nodes. Parsing results record total line count, Base64 status, successful node count, skipped count, protocol counts, and errors.

File and subscription sources independently maintain:

```text
last_success_at
last_error
generation
nodes
diagnostics
```

For example, if the file refresh succeeds but the subscription temporarily times out:

```text
file         → use the latest generation
subscription → retain the last successful generation and mark it stale
```

A successful refresh from one source does not clear the most recent successful nodes from another source that temporarily failed.

## Subscription Target Restrictions (Optional)

When `proxy_pool_subscription_public_only=true`, the initial subscription URL and every HTTP redirect are revalidated:

- Only `http` / `https` are allowed.
- The hostname must resolve.
- Private / loopback / link-local / multicast / reserved / unspecified addresses are rejected.
- At most 3 redirects are allowed.
- The response remains subject to the 2 MiB content limit.

This option is disabled by default so local research environments can continue to use LAN or self-hosted subscription services.

## Probe: IPv4 / IPv6 and False-Positive Protection

Supported settings:

```text
proxy_pool_probe_provider = cloudflare | ipinfo
proxy_pool_probe_dual_stack = true | false
```

With dual-stack enabled, IPv4 and IPv6 are probed independently and store:

```text
status
tested_at
latency_ms
exit_ip
error
```

If one family works and the other fails, the node may still be considered usable while retaining the independent results for both families.

**HTTP 2xx no longer automatically means a healthy probe.** A probe must satisfy all of the following:

```text
HTTP 2xx
+ successfully parse a valid exit IP
+ IP family matches the current IPv4/IPv6 probe
```

Otherwise it is marked `unhealthy`, preventing false positives such as "HTTP 200 but malformed response / no IP".

## Probe-Aware Soft Selection

Node scheduling first requires:

```text
enabled
not retired
capacity available
fixed node not cooling
```

It then groups nodes by recent probe status:

```text
Tier 0: recent healthy
Tier 1: unknown / stale
Tier 2: recent unhealthy
```

Affinity / health / inflight selection is performed from the best available tier first. Recent unhealthy status is a **soft deprioritization**, not a permanent hard ban; if it is the only available node, it may still be tried.

## Fixed and Rotating Health Models

### Fixed node

Real registration success:

```text
registration_successes += 1
business_samples += 1
health = min(1.0, health + 0.1)
failure_count = 0
cooldown = none
```

Confirmed transport failure:

```text
transport_failures += 1
business_samples += 1
failure_count += 1
health = max(0.05, health * 0.7)
```

Cooldown:

```text
30s → 60s → 120s → 240s → 480s → max 600s
```

### Success / attempts counters

Every lease granted to a node increments its attempt counter, regardless of the outcome:

```text
attempts += 1
```

The WebUI node table renders `registration_successes / attempts` as "OK / attempts", e.g. `0/0` or `3/7 · 42.9%`. With `proxy_pool_persist_health` enabled, `attempts` is persisted alongside the other health fields. Note that `business_samples` only counts attempts that produced business feedback (a real success or a confirmed transport failure), while `attempts` counts every granted lease, so code-fetch failures and application-level errors also land in the denominator.

### Rotating gateway

A rotating gateway does not display fixed-node Health and does not apply a gateway-wide cooldown because of one bad exit. It records:

```text
exit_successes
exit_failures
gateway_success_rate
```

This prevents a gateway that frequently changes exits from appearing permanently as `Health=1.0` simply because it accumulated successful samples.

### Business sample deduplication

A single account attempt contributes at most one business-health sample. If a suspected failure probe occurs after a successful attempt, that attempt is not counted twice as two business samples.

**Configuration/authentication errors are not business-health samples.** They increment `configuration_failures` and mark the node unavailable, but do not reduce Health, increment `business_samples`, or enter exponential transport cooldown.

## Five Failure Categories

Network feedback is divided into five categories:

1. **compatibility**: an internal component/protocol contract is incompatible; node Health is not penalized.
2. **configuration**: proxy authentication, credentials, or obvious configuration problems; the node is marked unavailable but the event is not counted as a transport Health sample.
3. **hard_transport**: explicit exit-transport failures such as proxy connection failure, SOCKS CONNECT, HTTP CONNECT, or network unreachable; fixed-node Health is reduced and cooldown applies.
4. **suspected_transport**: TLS, EOF, reset, timeout, and similar errors that may come from either the proxy or target path; the node is immediately reprobed and is penalized only if that reprobe also fails.
5. **application**: application-layer conditions such as 401, 429, normal OAuth states, or business parameters; proxy-node Health is not penalized.

## Structured Bridge Diagnostics

LocalProxyBridge no longer swallows internal exceptions as generic EOFs. It records structured failure kinds such as:

```text
upstream_connect
http_proxy_auth
http_connect
socks_auth
socks_connect
https_proxy_tls
local_dns
remote_dns
remote_reset
bridge
```

ProxyPool prioritizes these structured diagnostics for classification; string matching is only a fallback.

## NSFW / CPA Post-Processing

NSFW or CPA failures do not discard or re-register an account that was already registered successfully.

- Explicit proxy transport error → feed back into the corresponding proxy category.
- TLS/EOF/timeout → treat as suspected and reprobe immediately before deciding whether to penalize.
- compatibility/config/application → handle according to the corresponding category.
- Both an explicit CPA `cpa_proxy` and the Registration Lease are first converted to HTTP-compatible endpoints, preventing raw SOCKS URLs from being passed directly to network components that do not support that scheme.

## Registration-Path Preflight (Optional)

A non-destructive node-path preflight is provided for:

```text
accounts.x.ai
grok.com
```

It checks only reachability, HTTP status, latency, and obvious Cloudflare block indications. It does not create mailboxes, create accounts, modify account settings, or count as a Runtime Health sample.

Web API:

```text
POST /api/proxy-pool/preflight?node_id=<node-id>
```

Manual preflight is disabled while a task is running. It can be turned off entirely with:

```text
proxy_pool_preflight_enabled = false
```

## Persistence: Node List and Health State

The WebUI maintains **one proxy pool**: nodes parsed from `proxy_pool_subscription_url` are appended to it, and every node add/remove (table buttons, "Add proxy") happens on that same pool. `proxy_pool_file`, `proxy_pool_store_file` and `proxy_pool_state_file` are advanced/CLI settings and are **not shown in the WebUI** (leave them unset to use the defaults).

The pool uses two JSON files with separate duties:

| File | Content | Written by |
| --- | --- | --- |
| `proxy_pool_store_file` (default `./proxy_pool.json`) | Nodes you added plus deletion records for removed file/subscription nodes | You / the WebUI (hand-editable) |
| `proxy_pool_state_file` (default `./proxy_pool_state.json`) | Node business-health counters, Failure/Cooldown, recent business errors | The pool, automatically |

See "Node list (JSON)" above for the node list. Health persistence is off by default:

```text
proxy_pool_persist_health = false
```

When enabled, node business-health state is atomically written to:

```text
proxy_pool_state_file = ./proxy_pool_state.json
```

After the Manager is rebuilt, nodes with the same stable node ID restore Health, business counters, Failure/Cooldown state, and recent business errors. Both files are in `.gitignore` by default, and the Docker entrypoint links them onto the `/data` volume.

## WebUI

The proxy-pool page displays or stores:

- Full node URI.
- protocol / backend / fixed-or-rotating.
- IPv4 / IPv6 probe results.
- fixed Runtime Health or rotating gateway success rate.
- business / transport / configuration counters.
- inflight / cooldown / recent error.
- subscription LKG / stale diagnostics.
- dual-stack, runtime cache, health persistence, public-only subscription, preflight, and related settings.

Web API:

```text
GET    /api/proxy-pool/status
POST   /api/proxy-pool/reload
POST   /api/proxy-pool/test
POST   /api/proxy-pool/nodes                  {"uri": "http://user:pass@127.0.0.1:7890"}
DELETE /api/proxy-pool/nodes?canonical=<canonical-uri>
POST   /api/proxy-pool/preflight?node_id=<node-id>
```

The node endpoints write the JSON list, update scheduling immediately and return the fresh snapshot; when `proxy_mode` is not `pool`, adding a node switches the mode to `pool` and explains it in the `notice` field. Reload / Test nodes are explicit proxy-pool actions: with a subscription or pool file configured but the mode not `pool`, they switch the mode the same way (`notice` explains it); with no source configured at all they return 400 instead of silently returning an empty pool; and when the configuration changed but proxy leases are still in use they return 409 (the snapshot marks that state as `config_pending`, and the next status poll after the leases drain rebuilds and applies it automatically). While a registration task is running these endpoints return 409, like the other maintenance operations. A node reference may be the raw URI, the canonical URI or the node id (the WebUI table sends the canonical URI; for advanced protocols it looks like `vless://<sha256>` and is not a parseable URI).

Under the project's current local-use model, the WebUI, status API, and related logs continue to display full proxy addresses, including authentication information.

## Compatibility Boundary

The V3 behavior described here is concentrated in managed `single` / `pool` mode. The default `proxy_mode=auto` continues to preserve the legacy GUI/CLI/WebUI, email, result persistence, pending, token sync, and proxy behavior.

Ordinary HTTP/SOCKS does not start sing-box or mihomo merely because advanced-protocol support exists. VLESS/VMess/Trojan/Hysteria2/TUIC/Shadowsocks require a core only when actually acquired, probed, or preflighted.
