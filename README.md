# DNS Operator

A lightweight Kubernetes operator that syncs standard Kubernetes Ingress and Traefik `IngressRoute` hostnames to a specific ConfigMap in `/etc/hosts` format. This is primarily used for local development environments where a local DNS server (like CoreDNS) can consume this ConfigMap to provide local resolution for cluster services.

## Features

- Watches standard Kubernetes `Ingress` resources.
- Watches Traefik `IngressRoute`, `IngressRouteTCP`, and `IngressRouteUDP` custom resources.
- Supports an explicit `loko.dev/hostname` annotation for custom overrides.
- Generates a ConfigMap formatted for DNS consumption (e.g., CoreDNS `hosts` plugin).

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `LOCAL_IP` | None | The IP address hostnames should resolve to. |
| `NAMESPACE` | `loko-system` | The namespace where the output ConfigMap is stored. |

## Deployment (bjw-s app-template)

To deploy using the [bjw-s app-template](https://github.com/bjw-s/helm-charts/tree/main/charts/library/common), you can use the following configuration in your `values.yaml`:

```yaml
controllers:
  main:
    containers:
      main:
        image:
          repository: ghcr.io/getloko/dns-operator
          tag: latest
        env:
          LOCAL_IP: "192.168.1.50" # Your cluster/LB IP
          NAMESPACE: "loko-system"

serviceAccount:
  create: true

# The controller needs RBAC to list resources and manage the ConfigMap
persistence:
  config:
    enabled: false # Not needed, it uses K8s API
```

### Required RBAC

The controller requires a ClusterRole with the following permissions:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: dns-operator
rules:
  - apiGroups: [""]
    resources: ["services", "configmaps", "namespaces", "events", "nodes"]
    verbs: ["get", "list", "watch", "update", "patch", "create"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch", "update", "patch"]
  - apiGroups: ["traefik.io"]
    resources: ["ingressroutes", "ingressroutetcps", "ingressrouteudps"]
    verbs: ["get", "list", "watch", "update", "patch"]
```
