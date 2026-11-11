"""Render real chart output, including the no-Operator installation mode."""
import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1]


def render(*values, succeeds=True):
    command = ["helm", "template", "mediamtx", str(CHART)]
    for value in (
        "authURL=http://django-api.default.svc.cluster.local:8080/api/mediamtx/auth",
        "service.loadBalancerIP=192.168.3.93", *values,
    ):
        command += ["--set", value]
    result = subprocess.run(command, capture_output=True, text=True)
    if not succeeds:
        if result.returncode == 0:
            raise AssertionError("Chart accepted invalid configuration")
        return []
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def find(documents, kind, name):
    return next(d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name)


class RenderTests(unittest.TestCase):
    def test_default_install_needs_no_prometheus_operator(self):
        documents = render()
        self.assertFalse(any(d["apiVersion"].startswith("monitoring.coreos.com/") for d in documents))
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertFalse(config["metrics"])
        self.assertEqual(config["authHTTPExclude"], [])

    def test_metrics_without_operator_are_private(self):
        documents = render("monitoring.enabled=true")
        self.assertFalse(any(d["kind"] == "ServiceMonitor" for d in documents))
        self.assertEqual(find(documents, "Service", "mediamtx-metrics")["spec"]["type"], "ClusterIP")
        ports = find(documents, "Service", "mediamtx")["spec"]["ports"]
        self.assertNotIn(9998, [p["port"] for p in ports])
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertEqual(config["authHTTPExclude"], [{"action": "metrics"}])
        self.assertFalse(config["api"])

    def test_operator_resources_are_explicitly_opted_in(self):
        documents = render("monitoring.enabled=true", "monitoring.serviceMonitor.enabled=true",
                           "monitoring.serviceMonitor.additionalLabels.release=kube-prometheus-stack")
        monitor = find(documents, "ServiceMonitor", "mediamtx")
        self.assertEqual(monitor["metadata"]["labels"]["release"], "kube-prometheus-stack")
        self.assertEqual(monitor["spec"]["endpoints"][0]["port"], "metrics")
        render("monitoring.serviceMonitor.enabled=true", succeeds=False)

    def test_webrtc_has_https_signaling_and_public_ice_but_no_public_http_port(self):
        documents = render("webrtc.enabled=true", "webrtc.hostname=staging.frikanalen.tv",
                           "webrtc.additionalHosts[0]=staging.frikanalen.tv",
                           "webrtc.allowOrigins[0]=https://example.org")
        configmap = find(documents, "ConfigMap", "mediamtx")["data"]
        config = yaml.safe_load(configmap["mediamtx.yml"])
        self.assertFalse(config["webrtcIPsFromInterfaces"])
        self.assertEqual(config["webrtcAdditionalHosts"], ["staging.frikanalen.tv"])
        self.assertEqual(config["webrtcAllowOrigins"],
                         ["https://staging.frikanalen.tv", "https://example.org"])
        self.assertNotIn("nginx.conf", configmap)
        ports = find(documents, "Service", "mediamtx")["spec"]["ports"]
        self.assertEqual({p["protocol"] for p in ports if p["port"] == 8189}, {"UDP", "TCP"})
        self.assertFalse(any(p["port"] in (8889, 8080, 9998) for p in ports))
        ingress = find(documents, "Ingress", "mediamtx-webrtc")
        self.assertEqual(ingress["spec"]["tls"][0]["hosts"], ["staging.frikanalen.tv"])
        rule = ingress["spec"]["rules"][0]
        self.assertEqual(rule["host"], "staging.frikanalen.tv")
        self.assertEqual([(p["path"], p["pathType"]) for p in rule["http"]["paths"]],
                         [("/contrib", "Prefix")])
        config_paths = set(config["paths"])
        self.assertTrue(all(p.startswith("~^contrib/") for p in config_paths))
        self.assertFalse(any(d["apiVersion"].startswith("traefik.io/") for d in documents))
        pod = find(documents, "Deployment", "mediamtx")["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in pod["containers"]], ["mediamtx"])
        render("webrtc.enabled=true", "webrtc.hostname=staging.frikanalen.tv", succeeds=False)

    def test_preview_converts_on_demand_inside_mediamtx_without_credentials(self):
        documents = render("preview.enabled=true")
        self.assertFalse(any(d["kind"] == "Deployment" and d["metadata"]["name"] != "mediamtx"
                             for d in documents))
        container = find(documents, "Deployment", "mediamtx")["spec"]["template"]["spec"]["containers"][0]
        self.assertTrue(container["image"].endswith("-ffmpeg"))
        self.assertNotIn("env", container)
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        [(name, path)] = [(k, v) for k, v in config["paths"].items() if "preview" in k]
        self.assertTrue(name.startswith("~^contrib/preview/("))
        command = path["runOnDemand"]
        self.assertIn("scale=-2:360,fps=25", command)
        self.assertIn("-c:v libvpx -threads 2", command)
        limits = container["resources"]["limits"]
        self.assertEqual(limits["cpu"], "4")
        self.assertIn("-c:a libopus -application lowdelay", command)
        self.assertIn("-flush_packets 1 -muxdelay 0", command)
        # Loopback, so Django can recognise the converter without a credential.
        self.assertIn("rtsp://127.0.0.1:$RTSP_PORT/contrib/live/$G1", command)
        self.assertIn("rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH", command)
        self.assertTrue(path["runOnDemandRestart"])

    def test_without_preview_the_plain_image_runs_and_no_preview_path_exists(self):
        documents = render()
        container = find(documents, "Deployment", "mediamtx")["spec"]["template"]["spec"]["containers"][0]
        self.assertFalse(container["image"].endswith("-ffmpeg"))
        self.assertNotIn("env", container)
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertFalse(any("preview" in k for k in config["paths"]))

    def test_write_queue_size_is_set_only_when_given(self):
        config = yaml.safe_load(find(render(), "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertNotIn("writeQueueSize", config)
        documents = render("writeQueueSize=8192")
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertEqual(config["writeQueueSize"], 8192)

if __name__ == "__main__":
    unittest.main()
