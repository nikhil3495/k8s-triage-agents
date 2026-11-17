"""Live cluster access through the official ``kubernetes`` Python client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from triage.k8s import normalize
from triage.k8s.base import JSON, ClusterError


def _load_config(context: str | None) -> client.ApiClient:
    """Load in-cluster config when running in a pod, otherwise kubeconfig."""
    loader: Any = config
    try:
        loader.load_incluster_config()
    except config.ConfigException:
        loader.load_kube_config(context=context)
    return client.ApiClient()


class LiveClusterClient:
    """Reads from a real API server. Pair with the read-only ClusterRole in deploy/k8s-rbac."""

    def __init__(
        self, context: str | None = None, api_client: client.ApiClient | None = None
    ) -> None:
        """Connect using in-cluster config, or kubeconfig ``context``."""
        self._api: Any = api_client or _load_config(context)
        self.core = client.CoreV1Api(self._api)
        self.apps = client.AppsV1Api(self._api)
        self.custom = client.CustomObjectsApi(self._api)

    def _ser(self, obj: Any) -> Any:
        return self._api.sanitize_for_serialization(obj)

    def list_pods(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.list_pods`."""
        pods = self._ser(self.core.list_namespaced_pod(namespace))
        return [normalize.pod_summary(p) for p in pods.get("items", [])]

    def describe_pod(self, namespace: str, name: str) -> JSON:
        """See :meth:`ClusterReader.describe_pod`."""
        try:
            return normalize.pod_detail(self._ser(self.core.read_namespaced_pod(name, namespace)))
        except ApiException as exc:
            raise ClusterError(f"pod {namespace}/{name}: {exc.reason}") from exc

    def get_pod_logs(
        self, namespace: str, name: str, *, previous: bool = False, tail_lines: int = 100
    ) -> str:
        """See :meth:`ClusterReader.get_pod_logs`."""
        try:
            logs: str = self.core.read_namespaced_pod_log(
                name, namespace, previous=previous, tail_lines=tail_lines
            )
            return logs
        except ApiException as exc:
            return f"<no logs: {exc.status} {exc.reason}>"

    def get_events(self, namespace: str, involved_object: str | None = None) -> list[JSON]:
        """See :meth:`ClusterReader.get_events`."""
        selector = f"involvedObject.name={involved_object}" if involved_object else None
        evs = self._ser(self.core.list_namespaced_event(namespace, field_selector=selector))
        return [normalize.event_summary(e) for e in evs.get("items", [])]

    def list_deployments(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.list_deployments`."""
        deps = self._ser(self.apps.list_namespaced_deployment(namespace))
        return [normalize.deployment_summary(d) for d in deps.get("items", [])]

    def list_services(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.list_services`."""
        svcs = self._ser(self.core.list_namespaced_service(namespace)).get("items", [])
        eps = self._ser(self.core.list_namespaced_endpoints(namespace)).get("items", [])
        by_name = {e["metadata"]["name"]: e for e in eps}
        return [normalize.service_summary(s, by_name.get(s["metadata"]["name"])) for s in svcs]

    def list_nodes(self) -> list[JSON]:
        """See :meth:`ClusterReader.list_nodes`."""
        return [
            normalize.node_summary(n) for n in self._ser(self.core.list_node()).get("items", [])
        ]

    def get_pod_metrics(self, namespace: str) -> list[JSON]:
        """See :meth:`ClusterReader.get_pod_metrics`. Requires metrics-server."""
        try:
            res: Any = self.custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods"
            )
        except ApiException as exc:
            raise ClusterError(f"metrics-server unavailable: {exc.reason}") from exc
        return [normalize.pod_metrics_summary(i) for i in res.get("items", [])]

    def get_node_metrics(self) -> list[JSON]:
        """See :meth:`ClusterReader.get_node_metrics`. Requires metrics-server."""
        try:
            res: Any = self.custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        except ApiException as exc:
            raise ClusterError(f"metrics-server unavailable: {exc.reason}") from exc
        return [normalize.node_metrics_summary(i) for i in res.get("items", [])]


class LiveClusterWriter:
    """Mutations against a real cluster. Only constructed by the HITL executor."""

    def __init__(
        self, context: str | None = None, api_client: client.ApiClient | None = None
    ) -> None:
        """Connect using in-cluster config, or kubeconfig ``context``."""
        self._api: Any = api_client or _load_config(context)
        self.apps = client.AppsV1Api(self._api)

    def rollout_restart(self, namespace: str, deployment: str) -> JSON:
        """Restart like ``kubectl rollout restart``: bump a pod-template annotation."""
        now = datetime.now(UTC).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                }
            }
        }
        self.apps.patch_namespaced_deployment(deployment, namespace, body)
        return {"ok": True, "restarted_at": now}

    def scale_deployment(self, namespace: str, deployment: str, replicas: int) -> JSON:
        """Patch the deployment's scale subresource."""
        self.apps.patch_namespaced_deployment_scale(
            deployment, namespace, {"spec": {"replicas": replicas}}
        )
        return {"ok": True, "replicas": replicas}

    def rollback_deployment(self, namespace: str, deployment: str) -> JSON:
        """Re-apply the pod template of the previous ReplicaSet revision."""
        ser = self._api.sanitize_for_serialization
        dep: JSON = ser(self.apps.read_namespaced_deployment(deployment, namespace))
        labels = dep["spec"]["selector"].get("matchLabels") or {}
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        found: JSON = ser(self.apps.list_namespaced_replica_set(namespace, label_selector=selector))

        def revision(rs: JSON) -> int:
            ann = rs["metadata"].get("annotations") or {}
            return int(ann.get("deployment.kubernetes.io/revision", 0))

        rss = sorted(found.get("items", []), key=revision)
        if len(rss) < 2:
            raise ClusterError(f"{deployment} has no previous revision to roll back to")
        prev = rss[-2]
        template: JSON = prev["spec"]["template"]
        template["metadata"].get("labels", {}).pop("pod-template-hash", None)
        self.apps.patch_namespaced_deployment(
            deployment, namespace, {"spec": {"template": template}}
        )
        return {
            "ok": True,
            "rolled_back_to": revision(prev),
        }

    def patch_resources(
        self,
        namespace: str,
        deployment: str,
        container: str,
        requests: dict[str, str] | None,
        limits: dict[str, str] | None,
    ) -> JSON:
        """Strategic-merge patch of one container's resources."""
        resources: JSON = {}
        if requests:
            resources["requests"] = requests
        if limits:
            resources["limits"] = limits
        body = {
            "spec": {
                "template": {"spec": {"containers": [{"name": container, "resources": resources}]}}
            }
        }
        self.apps.patch_namespaced_deployment(deployment, namespace, body)
        return {"ok": True, "resources": resources}
