# Vishanti — Dedicated Envoy Gateway Deployment Guide

## 1. Architecture

Cilium v1.19's native Gateway API uses a shared Envoy daemonset and does not support dedicated proxies per tenant. To achieve **per-tenant isolation with resource limits**, we deploy **Envoy Gateway** alongside Cilium.

| Layer | Component | Role |
|---|---|---|
| **DNS / CDN** | Cloudflare | DDoS protection, CDN, optional TLS termination |
| **L3 Firewall** | `loadBalancerSourceRanges` | Blocks unauthorized IPs at the cloud LB level |
| **L7 Ingress** | Envoy Gateway | Dedicated Envoy proxy per tenant: TLS termination, HTTP routing, IP filtering by real client IP |
| **L3/L4 Security** | Cilium | Network policies between Envoy pods and backend pods |

**Traffic Flow:**
```
Client → Cloudflare (optional) → OVH LB (L3 filter) → Node (SNAT) → Envoy Proxy (L7 filter) → Cilium → Backend Pod
```

**Namespace Layout:**
- `envoy-gateway-system` — Envoy Gateway controller + all dedicated Envoy proxy pods
- `vishanti-<tenant>` — Backend application pods (created via UI)

---

## 2. Deployment Scenarios

### Scenario A — Cloudflare Proxy ON + Let's Encrypt

```
Client (1.2.3.4)
  │ HTTPS to Cloudflare edge (104.21.x.x)
  ▼
Cloudflare Edge
  │ Adds: X-Forwarded-For: 1.2.3.4
  │ Adds: CF-Connecting-IP: 1.2.3.4
  │ Opens new connection to your server (46.105.x.x)
  ▼
OVH LB — loadBalancerSourceRanges CHECK
  │ Source IP is Cloudflare (e.g., 173.245.48.5) → ALLOWED ✅
  ▼
Kubernetes Node — SNAT
  │ Cloudflare IP → 10.168.0.60 (node's own internal IP)
  ▼
Envoy Pod
  │ Sees source: 10.168.0.60 → in trustedCIDRs → reads XFF header
  │ Extracts real client IP: 1.2.3.4
  │ SecurityPolicy: Is 1.2.3.4 in clientCIDRs? → YES ✅ → 200 OK
```

**Security layers active:** Cloudflare WAF → L3 firewall → L7 SecurityPolicy (real IP enforced)

---

### Scenario B — Cloudflare Proxy OFF + Let's Encrypt

```
Client (1.2.3.4)
  │ HTTPS directly to server IP (46.105.x.x)
  │ DNS resolves to your actual IP (not Cloudflare)
  ▼
OVH LB — loadBalancerSourceRanges CHECK
  │ Source IP is 1.2.3.4 → must be in the L3 whitelist → ALLOWED ✅
  ▼
Kubernetes Node — SNAT
  │ Client IP (1.2.3.4) → 10.168.0.60 (node's own internal IP)
  │ Real client IP is LOST here (no Cloudflare to preserve it via XFF)
  ▼
Envoy Pod
  │ Sees source: 10.168.0.60 → in trustedCIDRs → looks for XFF → none found
  │ Falls back to: client IP = 10.168.0.60
  │ SecurityPolicy: Is 10.168.0.60 in 10.168.0.0/16? → YES ✅ → 200 OK
```

**Security layers active:** L3 firewall only (L7 sees node IP due to SNAT — node subnet is allowed by design)

> **Why is this safe?** `10.168.0.0/16` is a private range unreachable from the internet. Any packet that arrives via SNAT has already been pre-screened by `loadBalancerSourceRanges`. The SecurityPolicy node-subnet entry simply acknowledges what L3 already approved.

---

### Scenario C — Cloudflare Proxy ON (Origin Certificate)

Identical flow to Scenario A. The only difference is the TLS connection **between Cloudflare and your server**:

| | Scenario A (LE) | Scenario C (Origin Cert) |
|---|---|---|
| Client → Cloudflare TLS | Cloudflare's own cert | Cloudflare's own cert |
| Cloudflare → Your Server TLS | Let's Encrypt cert (publicly trusted) | Cloudflare Origin cert (only CF trusts it) |
| Bypass protection | L3 only | TLS handshake fails if attacker bypasses CF |

Origin certificates add defense-in-depth: even if someone discovers your server's real IP and bypasses Cloudflare, their TLS handshake fails because the Origin cert is not trusted by any browser or `curl`.

---

## 3. Generated Manifest Files

Each tenant's config is stored in `tenants/<tenant-short>/` (e.g., `tenants/lbpolicy3/`).

```
tenants/
├── lbpolicy3/
│   ├── envoy-gateway-tenant.yaml       # Core infra: EnvoyProxy, GatewayClass, Gateway, HTTPRoute
│   ├── client-ip-whitelist.yaml        # L7 filtering: ClientTrafficPolicy + SecurityPolicy
│   ├── envoy-gateway-policies.yaml     # Cilium: Envoy pod network policies
│   └── update-default-isolation.yaml   # Cilium: Backend pod ingress from Envoy
```

### `envoy-gateway-tenant.yaml`

Contains 4 resources applied to the cluster:

#### 1. `EnvoyProxy` — Envoy pod configuration
```yaml
kind: EnvoyProxy
spec:
  provider:
    kubernetes:
      envoyDeployment:
        replicas: 2              # Two Envoy pods for HA
        container:
          resources:             # Per-tenant CPU/memory limits
      envoyService:
        externalTrafficPolicy: Cluster   # MUST be Cluster for multi-node compatibility
        loadBalancerSourceRanges:        # L3 FIREWALL — OVH LB drops everything else
          - 173.245.48.0/20             # Cloudflare IPs (always included)
          - ...                         # Your whitelisted custom IPs
  telemetry:
    accessLog:                   # Logs: method, path, code, XFF, CF-Connecting-IP
```
> `externalTrafficPolicy: Cluster` allows traffic to reach an Envoy pod on any node, but causes SNAT — which is why `loadBalancerSourceRanges` (not Cilium `fromCIDR`) is used for L3 filtering.

#### 2. `GatewayClass` — Links to Envoy Gateway controller
```yaml
kind: GatewayClass
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
  parametersRef:
    name: albpolicytest-proxy    # Points to the EnvoyProxy above
```

#### 3. `Gateway` — Listens on ports 80/443, configures TLS
```yaml
kind: Gateway
spec:
  listeners:
    - name: http                 # Port 80 — HTTP
    - name: https                # Port 443 — TLS termination with your cert
      tls:
        certificateRefs:
          - name: <tls-secret>   # Let's Encrypt or Cloudflare Origin cert secret
```

#### 4. `HTTPRoute` — Routes traffic to backend service
```yaml
kind: HTTPRoute
spec:
  hostnames: ["yourdomain.incubera.xyz"]
  rules:
    - backendRefs:
        - name: <backend-service>
          port: 80
```

---

### `client-ip-whitelist.yaml`

Contains 2 resources for L7 IP filtering:

#### 5. `ClientTrafficPolicy` — Real client IP extraction
```yaml
kind: ClientTrafficPolicy
spec:
  clientIPDetection:
    xForwardedFor:
      trustedCIDRs:
        - 173.245.48.0/20   # All Cloudflare IPs — trusted to set XFF (proxy ON)
        - ...
        - 10.168.0.0/16     # Internal node subnet — trusted for SNAT path (proxy OFF)
```
Tells Envoy: *"If a packet's source IP is in these ranges, trust the `X-Forwarded-For` header to find the real client IP."* When there is no XFF (proxy OFF), Envoy falls back to the source IP (the node IP).

#### 6. `SecurityPolicy` — Enforce IP whitelist at L7
```yaml
kind: SecurityPolicy
spec:
  authorization:
    defaultAction: Deny          # Default deny everything
    rules:
      - action: Allow
        principal:
          clientCIDRs:
            - 51.77.216.8/32    # Your whitelisted public IPs (checked via XFF when CF proxy ON)
            - ...
            - 10.168.0.0/16     # Node subnet (CF proxy OFF — L3 already screened this)
```
Enforces who can actually reach the backend. When Cloudflare proxy is ON, the `clientCIDRs` are checked against the **real client IP** (extracted from XFF). When proxy is OFF, they are checked against the **node IP** (hence the node subnet entry).

---

### `envoy-gateway-policies.yaml`

Cilium network policy for the **Envoy pod** in `envoy-gateway-system`:

```yaml
# Ingress: Envoy accepts traffic from its owning Gateway (the LB service)
# Egress:
#   - Port 18000/18001 → Envoy Gateway controller (xDS config stream, CRITICAL)
#   - Port 80 → Backend pods in tenant namespace
#   - Port 53 → kube-dns (DNS resolution)
```
Without the xDS egress rule, Envoy has no route/listener configuration and refuses all connections.

---

### `update-default-isolation.yaml`

Cilium network policy for **backend pods** in the tenant namespace:

```yaml
# Ingress: Allow traffic from Envoy pods (identified by gateway label + envoy-gateway-system namespace)
# Egress: Allow to internet + kube-dns
```
Ensures backend pods only accept HTTP traffic from Envoy — not from any other pod or external source.

---

## 4. First-Time Installation

> **Automated:** Run `bash deploy-envoy-tenant.sh` after creating the tenant via the UI. The script handles Steps 3–8 automatically: prompts for inputs, auto-detects IPs, generates all YAMLs into `tenants/<name>/`, deletes the UI Cilium gateway, deploys, and verifies.

### Step 1: Install Envoy Gateway Controller
```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.3.0 \
  --namespace envoy-gateway-system --create-namespace
```

### Step 2: Create Tenant via UI
Create Org, Project, VPC, ALB, and Pods as usual.

### Step 3: Delete UI-Created Gateway and HTTPRoute

The Vishanti UI creates a Cilium `Gateway` and `HTTPRoute` that hold the LoadBalancer IP. Delete them before applying the Envoy manifests.

```bash
TENANT_NS="vishanti-<tenant>"   # e.g., vishanti-lbpolicy3

# List existing gateways and routes
kubectl get gateway,httproute -n $TENANT_NS

# Delete the UI-created Gateway (class: cilium) and its HTTPRoute
kubectl delete gateway <CILIUM_GATEWAY_NAME> -n $TENANT_NS
kubectl delete httproute <HTTPROUTE_NAME> -n $TENANT_NS
```

> Deleting the Gateway releases the LoadBalancer IP so Envoy's Gateway can claim it.

### Step 4: Apply Manifests

```bash
TENANT_SHORT="<tenant-short>"   # e.g., lbpolicy3

# Apply in order: core infra first, then policies
kubectl apply -f tenants/$TENANT_SHORT/envoy-gateway-tenant.yaml
kubectl apply -f tenants/$TENANT_SHORT/client-ip-whitelist.yaml
kubectl apply -f tenants/$TENANT_SHORT/envoy-gateway-policies.yaml
kubectl apply -f tenants/$TENANT_SHORT/update-default-isolation.yaml
```

### Step 5: Verify

```bash
# Check Envoy service claimed the IP
kubectl get svc -n envoy-gateway-system | grep envoy-vishanti

# Check Gateway is programmed
kubectl get gateway -n vishanti-$TENANT_SHORT

# Test connectivity
curl -k https://<YOUR_DOMAIN>/
```

---

## 5. Re-Deployment (After Namespace/Cluster Reset)

Follow Steps 2–3 from Section 4. If the entire cluster was reset, also do Step 1 first.
Existing tenant configs are preserved in `tenants/<name>/` — the script re-applies them.

---

## 6. Operational Commands

**View Envoy access logs** (shows real client IPs, XFF, CF-Connecting-IP):
```bash
POD=$(kubectl get pod -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=albpolicytest \
  -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n envoy-gateway-system $POD -c envoy -f
```

**Check current L3 whitelist (loadBalancerSourceRanges):**
```bash
kubectl get svc -n envoy-gateway-system <SVC_NAME> \
  -o jsonpath='{.spec.loadBalancerSourceRanges}'
```

**Check current L7 whitelist (SecurityPolicy):**
```bash
kubectl get securitypolicy <TENANT>-ip-whitelist -n vishanti-<TENANT> -o yaml
```

**Add an IP to SecurityPolicy (L7) on the fly:**
```bash
kubectl patch securitypolicy <TENANT>-ip-whitelist -n vishanti-<TENANT> \
  --type=merge -p '{"spec":{"authorization":{"rules":[{"name":"allow-whitelisted-client-ips","action":"Allow","principal":{"clientCIDRs":["<IP1>/32","<IP2>/32","10.168.0.0/16"]}}]}}}'
```

**Check Cloudflare IP detected by Envoy:**
```bash
# Real client IP seen per request
kubectl logs -n envoy-gateway-system $POD -c envoy | grep -v "10.168" | tail -20
```

---

## 7. Troubleshooting

### `RBAC: access denied` (HTTP 403)

Your real client IP is not in the SecurityPolicy `clientCIDRs`.

1. Check what IP Envoy sees for your requests:
   ```bash
   kubectl logs -n envoy-gateway-system $POD -c envoy --tail=10
   ```
2. The log format is: `"METHOD PATH PROTO" CODE FLAGS BYTES_IN BYTES_OUT MS "XFF" "CF-CONNECTING-IP"`
3. Patch the SecurityPolicy to add your IP (see Operational Commands above).

### HTTP 522 (Cloudflare cannot connect to your server)

Cloudflare's IPs are not in `loadBalancerSourceRanges`. The OVH LB is dropping Cloudflare's connections.

```bash
# Check L3 whitelist
kubectl get svc -n envoy-gateway-system <SVC_NAME> -o jsonpath='{.spec.loadBalancerSourceRanges}'
# Cloudflare IPs (173.245.48.0/20 etc.) must be present
```

### Connection Reset by Peer

Envoy is expecting PROXY protocol headers but the LB is not sending them. Ensure `enableProxyProtocol` is **not** set in `ClientTrafficPolicy` (OVH does not support PROXY protocol via the standard annotation).

### Timeout / Cannot Connect

1. Check IP is in L3 whitelist:
   ```bash
   kubectl get svc -n envoy-gateway-system <SVC_NAME> -o jsonpath='{.spec.externalTrafficPolicy}'
   # Should be: Cluster
   ```
2. Check `externalTrafficPolicy` is `Cluster` — if `Local`, packets to nodes without Envoy are dropped.

### 503 Service Unavailable

Envoy is running but can't reach the backend.
- Check Cilium policies are applied: `kubectl get cnp -n envoy-gateway-system`
- Check backend pod labels match: `kubectl get pods -n vishanti-<TENANT> --show-labels`

### xDS timeout / No listeners (Connection Refused)

```bash
kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=albpolicytest \
  -c envoy --tail=30 | grep -i "xds\|timeout\|grpc"
```
If you see `gRPC config stream closed`: re-apply `envoy-gateway-policies.yaml` — the egress rule to ports `18000`/`18001` is missing.

### LoadBalancer IP Pending

Another Gateway (the UI-created Cilium one) is still holding the IP.
```bash
kubectl get gateway -n vishanti-<TENANT>
kubectl delete gateway <CILIUM_GATEWAY_NAME> -n vishanti-<TENANT>
```

### UI Shows "Failed" Status

**Expected.** The UI tracks the Cilium Gateway it created, which was deleted. The Envoy Gateway works independently. This is cosmetic only.
