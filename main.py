import os
import re
from datetime import UTC, datetime

import kopf
import logging
from kubernetes import client, config

# Traefik uses Host(`domain`) or HostSNI(`domain`)
HOST_REGEX = re.compile(r"Host(?:SNI)?\(`([^`]+)`\)")

CONFIGMAP_NAME = os.environ.get("CONFIGMAP_NAME", "loko-dynamic-hosts")
CONFIGMAP_KEY = os.environ.get("CONFIGMAP_KEY", "dynamic.hosts")
NAMESPACE = os.environ.get("NAMESPACE")
LOCAL_IP = os.environ.get("LOCAL_IP")
LOCAL_DOMAIN = os.environ.get("LOCAL_DOMAIN")
COREDNS_NAMESPACE = os.environ.get("COREDNS_NAMESPACE", "kube-system")
COREDNS_DEPLOYMENT = os.environ.get("COREDNS_DEPLOYMENT", "coredns")
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
SUBDOMAIN_SKIP_ENABLED = os.environ.get("SUBDOMAIN_SKIP_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
SUBDOMAINS_TO_SKIP = tuple(
    entry.strip().lower() for entry in os.environ.get("SUBDOMAINS_TO_SKIP", "").split(",") if entry.strip()
)


def should_skip_hostname(hostname: str) -> bool:
    """Skip dynamic host management for configured wildcard-served subdomains."""
    if not SUBDOMAIN_SKIP_ENABLED or not SUBDOMAINS_TO_SKIP or not LOCAL_DOMAIN:
        return False

    normalized = hostname.strip().rstrip(".").lower()
    domain = LOCAL_DOMAIN.strip().rstrip(".").lower()
    if not normalized or not domain:
        return False

    for subdomain in SUBDOMAINS_TO_SKIP:
        suffix = f".{subdomain}.{domain}"
        if normalized == f"{subdomain}.{domain}" or normalized.endswith(suffix):
            return True
    return False


def extract_hostnames(resource_type, spec, annotations):
    hostnames = set()

    # 1. Check for explicit loko annotation (priority)
    loko_host = annotations.get("loko.dev/dns-host") or annotations.get("loko.dev/hostname")
    if loko_host:
        if not should_skip_hostname(loko_host):
            hostnames.add(loko_host)
        return hostnames

    # 2. Extract from spec
    if resource_type == "ingress":
        rules = spec.get("rules", [])
        for rule in rules:
            if rule.get("host") and not rule["host"].startswith("*") and not should_skip_hostname(rule["host"]):
                hostnames.add(rule["host"])

    elif resource_type in ["ingressroute", "tcpingressroute", "ingressrouteudp"]:
        routes = spec.get("routes", [])
        for route in routes:
            match = route.get("match", "")
            for host in HOST_REGEX.findall(match):
                if host and not host.startswith("*") and not should_skip_hostname(host):
                    hostnames.add(host)

    return hostnames


def reconcile(logger, exclude_uid=None):
    """Perform a full reconciliation of DNS records.

    exclude_uid: UID of a resource being deleted (not yet gone from API).
    """
    v1_net = client.NetworkingV1Api()
    v1_core = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()

    hostnames = set()

    # Fetch all resources
    try:
        # Standard Ingress
        ingresses = v1_net.list_ingress_for_all_namespaces()
        for item in ingresses.items:
            if exclude_uid and item.metadata.uid == exclude_uid:
                continue
            hostnames.update(extract_hostnames("ingress", item.spec.to_dict(), item.metadata.annotations or {}))

        # IngressRoute
        ingress_routes = custom_api.list_cluster_custom_object(
            group="traefik.io", version="v1alpha1", plural="ingressroutes"
        )
        for item in ingress_routes.get("items", []):
            if exclude_uid and item.get("metadata", {}).get("uid") == exclude_uid:
                continue
            hostnames.update(
                extract_hostnames(
                    "ingressroute",
                    item.get("spec", {}),
                    item.get("metadata", {}).get("annotations", {}),
                )
            )

        # IngressRouteTCP
        tcp_routes = custom_api.list_cluster_custom_object(
            group="traefik.io", version="v1alpha1", plural="ingressroutetcps"
        )
        for item in tcp_routes.get("items", []):
            if exclude_uid and item.get("metadata", {}).get("uid") == exclude_uid:
                continue
            hostnames.update(
                extract_hostnames(
                    "tcpingressroute",
                    item.get("spec", {}),
                    item.get("metadata", {}).get("annotations", {}),
                )
            )

        # IngressRouteUDP
        udp_routes = custom_api.list_cluster_custom_object(
            group="traefik.io", version="v1alpha1", plural="ingressrouteudps"
        )
        for item in udp_routes.get("items", []):
            if exclude_uid and item.get("metadata", {}).get("uid") == exclude_uid:
                continue
            hostnames.update(
                extract_hostnames(
                    "ingressrouteudp",
                    item.get("spec", {}),
                    item.get("metadata", {}).get("annotations", {}),
                )
            )

    except Exception as e:
        logger.error(f"Error fetching resources: {e}")
        return

    # Update ConfigMap
    content = "# Loko Dynamic Hosts - Generated by LoKO DNS Operator\n"
    for host in sorted(hostnames):
        content += f"{LOCAL_IP} {host}\n"

    try:
        existing_configmap = v1_core.read_namespaced_config_map(name=CONFIGMAP_NAME, namespace=NAMESPACE)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            existing_configmap = None
        else:
            logger.error(f"Error reading ConfigMap: {e}")
            return

    existing_content = (existing_configmap.data or {}).get(CONFIGMAP_KEY, "") if existing_configmap else ""
    content_changed = content != existing_content

    body = client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(name=CONFIGMAP_NAME, namespace=NAMESPACE),
        data={CONFIGMAP_KEY: content},
    )

    try:
        if existing_configmap is None:
            v1_core.create_namespaced_config_map(namespace=NAMESPACE, body=body)
            logger.info(f"Created ConfigMap with {len(hostnames)} hosts")
            content_changed = True
        elif content_changed:
            v1_core.replace_namespaced_config_map(name=CONFIGMAP_NAME, namespace=NAMESPACE, body=body)
            logger.info(f"Synchronized {len(hostnames)} hosts to ConfigMap")
        else:
            logger.info("Hosts content unchanged; skipping ConfigMap update")
    except client.exceptions.ApiException as e:
        logger.error(f"Error updating ConfigMap: {e}")
        return

    if not content_changed:
        logger.info("CoreDNS restart skipped; hosts content unchanged")
        return

    restart_timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        RESTART_ANNOTATION: restart_timestamp,
                    }
                }
            }
        }
    }

    try:
        apps_v1.patch_namespaced_deployment(
            name=COREDNS_DEPLOYMENT,
            namespace=COREDNS_NAMESPACE,
            body=patch_body,
        )
        logger.info(f"Triggered CoreDNS rollout restart at {restart_timestamp}")
    except client.exceptions.ApiException as e:
        logger.error(f"Error restarting CoreDNS deployment: {e}")


@kopf.on.startup()
def on_startup(logger, **kwargs):
    missing = [name for name, val in [("NAMESPACE", NAMESPACE), ("LOCAL_IP", LOCAL_IP)] if not val]
    if missing:
        raise kopf.PermanentError(f"Missing required environment variables: {', '.join(missing)}")
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    logger.info("LoKO DNS Operator starting...")
    reconcile(logger)


@kopf.on.create("ingresses")
@kopf.on.update("ingresses")
@kopf.on.create("ingressroutes", group="traefik.io")
@kopf.on.update("ingressroutes", group="traefik.io")
@kopf.on.create("ingressroutetcps", group="traefik.io")
@kopf.on.update("ingressroutetcps", group="traefik.io")
@kopf.on.create("ingressrouteudps", group="traefik.io")
@kopf.on.update("ingressrouteudps", group="traefik.io")
def on_resource_change(logger, **kwargs):
    reconcile(logger)


@kopf.on.delete("ingresses")
@kopf.on.delete("ingressroutes", group="traefik.io")
@kopf.on.delete("ingressroutetcps", group="traefik.io")
@kopf.on.delete("ingressrouteudps", group="traefik.io")
def on_resource_delete(uid, logger, **kwargs):
    reconcile(logger, exclude_uid=uid)


@kopf.timer("ingresses", interval=300)
@kopf.timer("ingressroutes", group="traefik.io", interval=300)
@kopf.timer("ingressroutetcps", group="traefik.io", interval=300)
@kopf.timer("ingressrouteudps", group="traefik.io", interval=300)
def periodic_reconcile(logger, **kwargs):
    reconcile(logger)
