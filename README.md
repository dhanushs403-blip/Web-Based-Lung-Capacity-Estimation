# Re-Deployment Guide: After Environment Reset

If your `vishanti-lbpolicy` namespace or the entire cluster is wiped, follow these steps to restore the Dedicated Envoy Proxy configuration.

## Pre-Requisites
1.  **Recreate Infrastructure via UI**: Create your Org, Project, VPC, ALB, and Pods as usual.
2.  **Verify New Details**: Note down the following from your new deployment:
    *   **Namespace**: (e.g., `vishanti-lbpolicy` or new name?)
    *   **Backend Service Name**: (e.g., `backendpod`)
    *   **Backend Label**: (e.g., `app=backendpod`)
    *   **Domain Name**: (e.g., `1albpolicytest.incubera.xyz`)
    *   **TLS Secret Name**: (e.g., `albpolicytest-tls-secret`)

## Step 1: Check Envoy Gateway Installation
If the entire cluster was reset, you must reinstall the controller.
```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.3.0 --namespace envoy-gateway-system --create-namespace
```

## Step 2: Update Configuration Files
Before running `kubectl apply`, you must edit your YAML files to match the new environment.

### 1. `envoy-gateway-tenant.yaml`
*   **Search & Replace**:
    *   Update `namespace: vishanti-lbpolicy` using your new Project namespace.
    *   Update `hostnames:` (Line 45) to your new domain.
    *   Update `backendRefs:` `name:` (Line 52) to your new backend Service name.
    *   Update `certificateRefs:` `name:` (Line 38) to your new TLS secret name.

### 2. `envoy-gateway-policies.yaml`
*   **Check Egress Rules**:
    *   Update `io.kubernetes.pod.namespace: vishanti-lbpolicy` (Line 29) to your new namespace.
    *   Update `app: backendpod` (Line 28) if your new backend pod has different labels.

### 3. `update-default-isolation.yaml`
*   **Check Namespace & Drop Rules**:
    *   Update `namespace: vishanti-lbpolicy`.
    *   Update `endpointSelector`: `matchLabels` `app: backendpod`.

## Step 3: Deploy
Run the standard deployment:
```bash
kubectl apply -f envoy-gateway-tenant.yaml
kubectl apply -f envoy-gateway-policies.yaml
kubectl apply -f update-default-isolation.yaml
```

## Step 4: Configure Network (Critical!)
The Gateway Service name commonly changes (it includes a hash of the Gateway name/namespace). You **must** find the new name.

1.  **Find the new Service name**:
    ```bash
    kubectl get svc -n envoy-gateway-system
    # Look for name starting with envoy-vishanti-...
    ```

2.  **Patch the new Service**:
    *   Replace `YOUR_NEW_SERVICE_NAME` below with the name you found.
    ```bash
    NEW_SVC_NAME="envoy-vishanti-lbpolicy-albpolicytest-4a04c05b"
    
    # 1. Connectivity Fix
    kubectl patch svc -n envoy-gateway-system $NEW_SVC_NAME -p '{"spec":{"externalTrafficPolicy":"Cluster"}}'
    
    # 2. Apply Whitelist
    kubectl patch svc -n envoy-gateway-system $NEW_SVC_NAME --patch-file service-whitelist.yaml
    ```
