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

    def test_programme_return_is_off_by_default_and_needs_a_source(self):
        config = yaml.safe_load(find(render(), "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertNotIn("contrib/program", config["paths"])
        render("programReturn.enabled=true", succeeds=False)

    def test_programme_return_converts_on_demand_and_may_reach_only_its_source(self):
        documents = render("programReturn.enabled=true", "networkPolicy.enabled=true",
                           "programReturn.sourceURL=http://cubemap.default.svc:9094/stream.ts",
                           "programReturn.sourceCIDRs[0]=192.168.3.0/24")
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        path = config["paths"]["contrib/program"]
        self.assertIn('-i "http://cubemap.default.svc:9094/stream.ts"', path["runOnDemand"])
        self.assertIn("libvpx", path["runOnDemand"])
        self.assertIn("rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH", path["runOnDemand"])
        container = find(documents, "Deployment", "mediamtx")["spec"]["template"]["spec"]["containers"][0]
        self.assertTrue(container["image"].endswith("-ffmpeg"))
        egress = find(documents, "NetworkPolicy", "mediamtx-egress")["spec"]["egress"]
        [rule] = [r for r in egress if r.get("ports") == [{"protocol": "TCP", "port": 9094}]]
        self.assertEqual(rule["to"], [{"ipBlock": {"cidr": "192.168.3.0/24"}}])

    def test_api_is_off_unless_asked_for(self):
        documents = render()
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertFalse(config["api"])
        self.assertFalse(any(d["metadata"]["name"] == "mediamtx-api" for d in documents))

    def test_api_is_private_and_stays_behind_django_auth(self):
        documents = render("api.enabled=true", "monitoring.enabled=true")
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        self.assertTrue(config["api"])
        self.assertEqual(config["apiAddress"], ":9997")
        self.assertEqual(config["authHTTPExclude"], [{"action": "metrics"}])
        service = find(documents, "Service", "mediamtx-api")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], "api")
        ports = find(documents, "Service", "mediamtx")["spec"]["ports"]
        self.assertNotIn(9997, [p["port"] for p in ports])
        container = find(documents, "Deployment", "mediamtx")["spec"]["template"]["spec"]["containers"][0]
        self.assertIn(9997, [p["containerPort"] for p in container["ports"]])

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

    def test_without_pull_the_live_path_has_no_relay_and_no_policy(self):
        documents = render()
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        [(name, path)] = [(k, v) for k, v in config["paths"].items() if "live" in k]
        self.assertEqual(path, {})
        self.assertFalse(any(d["kind"] == "NetworkPolicy" for d in documents))
        self.assertFalse(any(d["metadata"]["name"] == "mediamtx-pull" for d in documents))

    def test_pull_runs_the_relay_on_demand_on_the_live_path(self):
        documents = render("pull.enabled=true", "pull.credentialSecret=mediamtx-pull")
        config = yaml.safe_load(find(documents, "ConfigMap", "mediamtx")["data"]["mediamtx.yml"])
        [(name, path)] = [(k, v) for k, v in config["paths"].items() if "live" in k]
        self.assertTrue(name.startswith("~^contrib/live/("))
        self.assertEqual(path["runOnDemand"], "/pull/pull-source $G1 $RTSP_PORT $MTX_PATH")
        self.assertTrue(path["runOnDemandRestart"])
        self.assertEqual(path["runOnDemandStartTimeout"], "15s")
        script = find(documents, "ConfigMap", "mediamtx-pull")["data"]["pull-source"]
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("-protocol_whitelist", script)
        self.assertIn('"rtsp://127.0.0.1:$2/$3"', script)
        self.assertNotIn("http://*)", script)
        pod = find(documents, "Deployment", "mediamtx")["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertTrue(container["image"].endswith("-ffmpeg"))
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["FK_PULL_CREDENTIAL"]["valueFrom"]["secretKeyRef"]["name"], "mediamtx-pull")
        self.assertFalse(any(name.startswith("MTX_") for name in env))
        self.assertIn("/pull", [m["mountPath"] for m in container["volumeMounts"]])
        render("pull.enabled=true", succeeds=False)

    def test_network_policy_keeps_mediamtx_off_the_lan_but_lets_it_reach_django(self):
        documents = render("networkPolicy.enabled=true")
        policy = find(documents, "NetworkPolicy", "mediamtx-egress")
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
        internet, django, dns = policy["spec"]["egress"]
        self.assertIn("192.168.0.0/16", internet["to"][0]["ipBlock"]["except"])
        self.assertIn("10.0.0.0/8", internet["to"][0]["ipBlock"]["except"])
        self.assertEqual(django["to"][0]["podSelector"]["matchLabels"], {"app": "django-api"})
        self.assertEqual(django["ports"], [{"protocol": "TCP", "port": 8080}])
        self.assertEqual({p["port"] for p in dns["ports"]}, {53})


if __name__ == "__main__":
    unittest.main()
