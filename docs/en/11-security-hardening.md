# Security Hardening (Public-Internet Deployment)

VerveDeck is designed for **single-node self-hosted deployments, possibly exposed to the public
internet**. This document is the security overview: built-in defense matrix, the brute-force
defense chain, and the mandatory/recommended checklist for public deployments.
(Operational commands and troubleshooting live in `07-operations.md`.)

## 1. Defense Matrix

| Layer | Mechanism | Location |
|---|---|---|
| Gateway (nginx) | HTTP 301→HTTPS, TLS 1.2/1.3, HSTS | `platform/frontend/nginx.conf` |
| Gateway | **Rate limiting**: login 5 r/s/IP, API 30 r/s/IP, inference proxy 60 r/s/IP, 50 conns/IP, 429 on excess; **all four parameters configurable from the UI with hot-apply** (Settings → Gateway Rate Limiting; nginx reload without dropping connections, invalid configs roll back) | `platform/frontend/zones.conf` + `app/services/gateway_service.py` |
| Gateway | Security headers: X-Content-Type-Options / X-Frame-Options / Referrer-Policy; version hidden | `nginx.conf` |
| App · login | **Brute-force lockout**: 5 fails/account or 30 fails/IP → 15-min lock (423); thresholds/duration/IP whitelist configurable in UI | `app/api/auth.py` (Settings) |
| App · login | **Lockout alert**: firing the lock raises a `login_locked` event (WeCom/email channels + alert-events page, 5-min cooldown dedupe) | `app/services/alert_service.py` |
| App · passwords | bcrypt; create/reset/change passwords ≥ 8 chars; admin-set passwords force change at first login | `app/api/admin.py`, `app/api/auth.py` |
| App · audit | Every mutating call + every 401/403 recorded (user/**real client IP**/path/status/action code) | `app/audit.py` (Audit Log page) |
| App · sessions | JWT HS256 (72h expiry, secret must be changed); API keys 192-bit random, project-scoped, SHA-256 hash stored only | `app/security.py` |
| Deployment | Port exposure matrix (§2): only the web entry point is public; 3001/3002/3000/3003/9000/9001/9090 bound to 127.0.0.1; 9400/9100 localhost by default, LAN IP in cluster mode; postgres/redis/opensearch/alertmanager/task containers fintune_net-only; Docker API never TCP-exposed | `deploy/docker-compose.yaml` |
| Deployment | First-boot auto-generates strong random secrets (.env mode 600); warns on legacy weak defaults in existing .env | `deploy/deploy.sh` |
| Internal API | Alertmanager webhook requires internal token (`X-Vervedeck-Token`) | `/api/internal/am-webhook` |

## 2. Port Exposure Matrix (public / localhost / LAN)

Policy: **only the web business entry point is public; every other port binds to 127.0.0.1 or
the internal network (cluster mode)**. The Docker API is never TCP-exposed (local socket only).
Verified against a live public server with `ss -tlnp` (2026-11-17).

| Port | Service | Bind | Notes |
|---|---|---|---|
| 80 | web (nginx) | public | 301 → HTTPS only |
| 443 (or custom, e.g. 8433) | web (nginx) | public | **the only business entry**: /api/, /v1/, /grafana(/-light)/, /argilla/ all proxied same-origin |
| 22 | sshd | public (source-restricted recommended) | ops; `ufw allow from <office-IP> to any port 22` |
| 3001 | backend (uvicorn) | 127.0.0.1 | web proxies /api/ and /v1/ through it |
| 3002 | argilla | 127.0.0.1 | web proxies /argilla/ through it |
| 3000 / 3003 | grafana / grafana-light | 127.0.0.1 | web proxies /grafana/ and /grafana-light/ through it |
| 9000 / 9001 | rustfs S3 / console | 127.0.0.1 | host-side mc/clients only |
| 9090 | prometheus | 127.0.0.1 | local debugging; the UI entry is Grafana |
| 9400 / 9100 | dcgm-exporter / node-exporter | 127.0.0.1 (default) / LAN IP (cluster mode) | `.env` `SCRAPE_BIND_IP`: deploy.sh writes the LAN IP on first boot so the master node's Prometheus can scrape this node's GPU/host metrics over the LAN |
| 5432 / 6379 / 9200 / 9093 | postgres / redis / opensearch / alertmanager | fintune_net only | no host port mapping; containers reach each other by service name |
| 8000 | mineru / inference containers (vLLM·SGLang) | fintune_net only | backend reverse-proxies via container IP (external access goes through /v1/ on 443); metrics scraped by prometheus via container IP |
| 2375 / 2376 | Docker API | **never exposed** | only the local /var/run/docker.sock is mounted into the backend |

**Cross-node traffic in cluster mode (multi-node)** — all on the internal network, no new
public ports:

| Direction | Port | Purpose |
|---|---|---|
| node → master | 443/8433 (master entry) | add-node.sh node registration (POST /api/cluster/nodes) |
| master → node | 9400 / 9100 (node LAN IP) | master Prometheus scrapes node GPU/host metrics (`SCRAPE_BIND_IP`) |
| node ↔ node | container network (fintune_net interconnect, L2 or RDMA) | multi-node torchrun MASTER_PORT / NCCL training |

Notes:
- For a remote node's metrics to appear on the master platform's cluster page, add the node's
  LAN IP to the **master's** `deploy/monitoring/prometheus/prometheus.yml` static targets and
  hot-reload (`POST /-/reload`). The node-side 9400/9100 bind to the LAN IP (`SCRAPE_BIND_IP`) —
  reachable from the master, never from the internet (keep these ports closed in the cloud
  security group / firewall).
- Single-node deployments need nothing extra: with `SCRAPE_BIND_IP` unset, the exporters listen
  on 127.0.0.1 only.
- Any other 0.0.0.0 listener on the host (e.g. PCP's pmcd 44321 / pmlogger 4330, which listen on
  all interfaces by default) is unrelated to the platform; deploy.sh stops and disables them.
  Apply the same rule to any newly installed host service.

## 3. Brute-Force Defense Chain (login example)

```
Attacker → public 443
  ① nginx limit_req: >5 req/s per IP → 429 immediately (attacks never reach bcrypt)
  ② backend dual-dimension lockout:
       - 5 consecutive failures on one account → account locked 15 min
       - 30 failures from one IP → IP locked 15 min (stops horizontal spraying)
       - optional IP whitelist (office egress CIDR): outsiders get 403
  ③ lockout firing → login_locked alert (WeCom/email) + visible on alert-events page
  ④ audit log: every 401/423 recorded with real client IP — reconstruct attack source/accounts after the fact
```

All parameters are tunable without code changes:
- **nginx rate limits**: Settings → Gateway Rate Limiting (four knobs: login/API/proxy
  rates + per-IP concurrent connections). Saving re-renders `00-zones.conf`, validates with
  `nginx -t` and hot-reloads via `nginx -s reload` (existing connections survive; invalid
  configs roll back automatically). Values persist in the DB; after a web-container
  recreation the backend self-heals by re-applying on startup. The deploy-side file
  `deploy/nginx/00-zones.conf` is kept in sync (bind-mounted into the web container).
- **Lockout thresholds/whitelist**: Settings UI (DB-backed, effective in ~10s).

## 4. Mandatory Checklist for Public Deployment

1. **Minimal firewall** (ufw example):
   ```bash
   ufw default deny incoming
   ufw allow from <office-egress-IP> to any port 22 proto tcp   # SSH source-restricted
   ufw allow 80,443/tcp
   ufw enable
   ```
2. **Change every secret** in `deploy/.env`: `ADMIN_PASSWORD` / `JWT_SECRET` / `POSTGRES_PASSWORD`
   / `RUSTFS_SECRET_KEY` / `ARGILLA_API_KEY` / `GRAFANA_PASSWORD` (deploy.sh auto-generates random
   values on first boot; hand-made .env with defaults triggers a warning). Then
   `docker compose up -d`.
3. **Change admin password at first login** — enforced by the platform for default-password accounts.
4. **Enable the login IP whitelist** (strongly recommended on public deployments):
   Settings → Login Security → office egress IP/CIDR. Outside the list = 403, the ultimate gate
   against brute force.
5. **Verify S3 ports are not exposed**: rustfs 9000/9001 are bound to 127.0.0.1 in compose
   (older versions exposed them; run `docker compose up -d` after upgrading).
6. **Configure an alert channel** (Settings → Notification Channels: WeCom/email) so lockout and
   failure alerts actually reach you.

## 5. Recommended (defense in depth)

| Item | How |
|---|---|
| SSH hardening | `PermitRootLogin no`, `PasswordAuthentication no` (keys only); install `fail2ban` |
| Real TLS certificate | Replace the self-signed cert with a domain cert (Let's Encrypt/commercial) at `deploy/tls/vervedeck.{crt,key}`, rebuild web |
| Disable API docs | `.env`: `API_DOCS_ENABLED=false` (no /docs or /openapi.json on the internet) |
| Tighten CORS | `.env`: `CORS_ORIGINS=https://<your-domain>` (default `*`; low impact for same-origin deployments) |
| Disable Grafana anonymous access | `.env`: `GRAFANA_ANONYMOUS=false` (monitoring iframe then requires Grafana login; default anonymous read-only Viewer eases dashboard embedding) |
| Audit retention | Periodically archive `audit_logs` by `ts`; security events filter `action=security` |

## 6. Known Boundaries (honest notes)

- Login failure counters live in **backend process memory**: a backend restart clears them
  (attacker progress lost — in our favor). Not shared across replicas (single-instance form).
- `X-Forwarded-For` is client-forgeable and **only trusted because the backend accepts requests
  from the reverse proxy exclusively** (backend does not listen on public interfaces).
- Grafana anonymous read-only Viewer is on by default: metrics (GPU/node/task scale) are visible
  to the internet — disable per the table above if that concerns you.
- Content guardrails are a business-layer feature, out of scope here.

## 7. Change Log

| Date | Change |
|---|---|
| 2026-11-17 | Port exposure matrix added (§2): full `ss -tlnp` audit on a live public server — only the web entry (80/443 or a custom port) and SSH 22 are public; backend/argilla/grafana×2/rustfs×2/prometheus all bind 127.0.0.1; databases/cache/search/AM/task containers are fintune_net-only; the Docker API is never TCP-exposed. dcgm-exporter 9400 and node-exporter 9100 now honor `SCRAPE_BIND_IP` (default 127.0.0.1; deploy.sh writes the LAN IP on first boot so the master Prometheus can scrape cluster nodes over the LAN). deploy.sh stops and disables PCP (pmcd 44321 / pmlogger 4330 listen on 0.0.0.0 by default and are unrelated to the platform). The deploy-completion checklist gains a mandatory "verify port exposure" step |
| 2026-11-16 | Public-internet hardening landed: rustfs bound to 127.0.0.1; deploy.sh auto-generates strong random secrets + weak-default detection + hardening checklist output; nginx rate-limit zones (login 5 r/s / API 30 r/s / proxy 60 r/s / 50 conns) + security headers + version hidden; login lockout alert (login_locked event rule, auto-seeded on existing DBs); audit log records real client IP; unified password policy ≥ 8 chars; CORS origins and API-docs toggle configurable (`CORS_ORIGINS`/`API_DOCS_ENABLED`); Grafana anonymous access configurable (`GRAFANA_ANONYMOUS`) |
| 2026-11-16 | The four nginx rate-limit knobs (login/API/proxy rates + per-IP concurrency) are now UI-configurable: Settings → Gateway Rate Limiting re-renders `00-zones.conf` and hot-reloads nginx (invalid configs roll back). The `limit_conn` value moved into the dynamic zones file; the zones file is bind-mounted from the host (`deploy/nginx/00-zones.conf`, generated on first deploy); the backend self-heals the config on startup |
