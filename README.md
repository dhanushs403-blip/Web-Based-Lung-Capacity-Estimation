# Vishanti — Dedicated Envoy Gateway Deployment Guide

## 1. Architecture

Cilium v1.19's native Gateway API uses a shared Envoy daemonset and does not support dedicated proxies per tenant. To achieve **per-tenant isolation with resource limits**, we deploy **Envoy Gateway** alongside Cilium.

| Layer | Component | Role |
|---|---|---|
| **L7 Ingress** | Envoy Gateway | Dedicated Envoy Proxy per tenant: TLS termination, HTTP routing, resource limits |
| **L3/L4 Security** | Cilium | Network policies between Envoy pods and backend pods |

**Traffic Flow:**
```
Client → LoadBalancer (IP Whitelist) → Envoy Proxy Pod → Cilium Policy → Backend Pod
```

**Namespace Layout:**
- `envoy-gateway-system` — Envoy Gateway controller + all dedicated Envoy proxy pods
- `vishanti-<tenant>` — Backend application pods (created via UI)

---

## 2. Configuration Files

| File | Purpose |
|---|---|
| `envoy-gateway-tenant.yaml` | **Core infrastructure.** `EnvoyProxy` (resource limits, `externalTrafficPolicy: Cluster`, IP whitelist), `GatewayClass`, `Gateway`, `HTTPRoute`. |
| `envoy-gateway-policies.yaml` | **Envoy pod security.** Cilium policies allowing Envoy → xDS controller, Envoy → backend, Envoy → DNS. |
| `update-default-isolation.yaml` | **Backend pod security.** Cilium policy allowing ingress from Envoy pods into the tenant namespace. |

> **Note:** `service-whitelist.yaml` is **no longer needed**. IP whitelisting is now managed permanently in the `EnvoyProxy` CRD inside `envoy-gateway-tenant.yaml`.

---

## 3. Critical Configuration Details

### externalTrafficPolicy: Cluster
- **Must be `Cluster`**, not `Local`. With `Local`, traffic hitting a node without an Envoy pod is silently dropped.
- Configured permanently in `EnvoyProxy` → `envoyService.externalTrafficPolicy` so the controller does **not** revert it.

### IP Whitelisting via loadBalancerSourceRanges
- Since `Cluster` policy performs SNAT, Cilium's `fromCIDR` cannot see client IPs. Instead, we use `loadBalancerSourceRanges` on the Service itself.
- Configured permanently in `EnvoyProxy` → `envoyService.loadBalancerSourceRanges`.

### Envoy → xDS Controller Egress
- Envoy pods **must** be able to reach the Envoy Gateway controller on ports `18000`/`18001` (xDS gRPC). Without this, Envoy has no listener config and refuses all connections.
- Configured in `envoy-gateway-policies.yaml` → `gateway-allow-egress`.

---

## 4. First-Time Installation

### Step 1: Install Envoy Gateway Controller
```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.3.0 \
  --namespace envoy-gateway-system --create-namespace
```

### Step 2: Create Tenant via UI
Create Org, Project, VPC, ALB, and Pods as usual via the Vishanti UI.

### Step 3: Gather New Tenant Details
Note down:
- **Namespace**: e.g., `vishanti-lbpolicy3`
- **Backend Service Name**: e.g., `backendpod3`
- **Backend Label**: e.g., `app=backendpod3`
- **Domain Name**: e.g., `lbpolicy3.incubera.xyz`
- **TLS Secret Name**: e.g., `lbpolicy3-tls-secret`

### Step 4: Update YAML Files

**`envoy-gateway-tenant.yaml`:**
- `loadBalancerSourceRanges` → your allowed IPs
- `Gateway.metadata.namespace` → tenant namespace
- `certificateRefs.name` → TLS secret name (matches the one created by cert-manager)
- `HTTPRoute.hostnames` → domain name
- `HTTPRoute.backendRefs.name` → backend service name

**`envoy-gateway-policies.yaml`:**
- `app:` label → backend pod label
- `io.kubernetes.pod.namespace:` → tenant namespace

**`update-default-isolation.yaml`:**
- `metadata.namespace` → tenant namespace
- `endpointSelector.matchLabels.app:` → backend pod label

### Step 5: Delete UI-Created Cilium Gateway
The UI creates a Cilium Gateway that holds the LoadBalancer IP. Delete it to free the IP for Envoy.
```bash
kubectl get gateway -n <TENANT_NAMESPACE>
# Identify the one with class: cilium
kubectl delete gateway <CILIUM_GATEWAY_NAME> -n <TENANT_NAMESPACE>
```

### Step 6: Deploy
```bash
kubectl apply -f envoy-gateway-tenant.yaml
kubectl apply -f envoy-gateway-policies.yaml
kubectl apply -f update-default-isolation.yaml
```

### Step 7: Verify
```bash
# Check Envoy service got the IP and correct config
kubectl get svc -n envoy-gateway-system | grep envoy-vishanti
kubectl get svc -n envoy-gateway-system <SVC_NAME> -o jsonpath='{.spec.externalTrafficPolicy}'

# Test connectivity
curl -k https://<YOUR_DOMAIN>/
```

---

## 5. Re-Deployment (After Namespace/Cluster Reset)

Follow **Steps 2–7** from Section 4 above. If the entire cluster was reset, also do **Step 1** first.

---

## 6. Operational Commands

**Add/Remove an IP from Whitelist:**
1.  Edit `loadBalancerSourceRanges` in `envoy-gateway-tenant.yaml`.
2.  Run: `kubectl apply -f envoy-gateway-tenant.yaml`

**Check Current Whitelist:**
```bash
kubectl get svc -n envoy-gateway-system <SVC_NAME> -o jsonpath='{.spec.loadBalancerSourceRanges}'
```

**Check Access:**
```bash
curl -k https://<YOUR_DOMAIN>/
```

---

## 7. Troubleshooting

### Timeout / Cannot Connect
1.  **Check IP whitelist**: Is your current public IP in `loadBalancerSourceRanges`?
    ```bash
    kubectl get svc -n envoy-gateway-system <SVC_NAME> -o jsonpath='{.spec.loadBalancerSourceRanges}'
    ```
2.  **Check `externalTrafficPolicy`**: Must be `Cluster`, not `Local`.
    ```bash
    kubectl get svc -n envoy-gateway-system <SVC_NAME> -o jsonpath='{.spec.externalTrafficPolicy}'
    ```

### Connection Refused
1.  **Check Envoy pod logs** for xDS timeout errors:
    ```bash
    kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=albpolicytest -c envoy --tail=20
    ```
2.  If you see `gRPC config stream to xds_cluster closed: connection timeout`:
    - The `gateway-allow-egress` policy is missing the rule allowing Envoy → controller (port 18000).
    - Re-apply `envoy-gateway-policies.yaml` and restart Envoy pods.

### 503 Service Unavailable
- Envoy is running but can't reach the backend.
- **Check policies**: Ensure `gateway-allow-egress` and `default-isolation` are applied.
- **Check labels**: Verify backend pod labels match the policies (`app: backendpod3`).

### LoadBalancer IP Pending
- Another Gateway (usually the UI-created Cilium one) is holding the IP.
- Delete the conflicting gateway: `kubectl delete gateway <NAME> -n <NAMESPACE>`

### Gateway Not Programmed
- Check controller: `kubectl get pods -n envoy-gateway-system`
- Check logs: `kubectl logs -n envoy-gateway-system deployment/envoy-gateway`
- Check TLS secret name matches: the Gateway `certificateRefs.name` must match the actual secret in the tenant namespace.

### UI Shows "Failed" Status
- **Expected.** The UI tracks the Cilium Gateway it created, which was deleted. The Envoy Gateway works independently. This is cosmetic only.
