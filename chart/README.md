# MediaMTX contribution and preview chart

This chart lives in Frikanalen/contribution-receiver (moved from gitops `helm/mediamtx`). The clusters repository owns each environment's Argo Application, and infra owns the
listener addresses, MetalLB reservations and PF
forwarding. The existing Application name and Kubernetes resource names remain
`mediamtx`, so switching repositories does not create a second media service.

## Streams and authentication

- Contributions: `contrib/live/<source UUID>`, published over SRT, RTMP or RTSP.
- Derived browser previews: `contrib/preview/<source UUID>`, H.264 plus Opus.
- Django authorizes every media connection at `/api/mediamtx/auth`.
- Publish credentials can publish only the contribution. Read credentials can
  read the contribution and derived preview for their own source.
- With `pull.enabled`, the pull relay fills pulled sources' contribution
  paths (see "Pulled sources").
- The on-demand preview converter has no credential. From loopback, it may only
  read an enabled source's contribution and publish that source's preview over
  RTSP (see below).

SRT uses UDP 8890, RTMP TCP 1935 and RTSP interleaved TCP 8554. Infra advertises
those public URLs in Django admin. Anonymous media reads are not enabled.

## WebRTC with standard Ingress

Set `webrtc.enabled`, `webrtc.hostname` and `webrtc.additionalHosts` to enable.
`webrtc.hostname` is the site's own hostname; MediaMTX gets `/contrib` on it:

- Player: `https://<site>/contrib/preview/<source UUID>/`
- WHEP: `https://<site>/contrib/preview/<source UUID>/whep`
- Direct original player/WHEP/WHIP: the same native paths under
  `/contrib/live/<UUID>`.

Every MediaMTX path starts with `contrib/`, so the standard
`networking.k8s.io/v1` Ingress forwards the `/contrib` prefix without rewriting.
Paths and Location headers are preserved, so WHEP PATCH/DELETE session URLs work
without a proxy sidecar, rewrite middleware or IngressRoute CRD. Traefik
terminates HTTPS using the site's existing hostname/certificate; its
certificate-resolver annotation is configurable. The frontend must not use
`/contrib` for ordinary pages.

CORS allows the site's own origin. Add `webrtc.allowOrigins` for a player
embedded in another site.

Members preview from the site's `/organization/<id>/live` page. Django issues
the page a short-lived WHEP bearer token for its own sources. Outside the site,
use a Read credential's `username:password` as the bearer token. Source preview proves
reception, not that a feed is on air. A TX return requires publishing the channel
output as another source.

ICE media uses UDP 8189 and TCP 8189 fallback, forwarded by PF to the MediaMTX
LoadBalancer. Only the public hostname and reserved LAN address are advertised;
pod-interface addresses are not. No TURN relay is included. Networks blocking
both direct transports need a later TURN deployment.

## Low-latency VP8 and Opus conversion

Browsers cannot play the contribution's AAC audio, and Firefox plays H.264 only
with its OpenH264 plugin, so MediaMTX converts the feed to VP8 and Opus on
demand. When a reader opens `contrib/preview/<source UUID>`, MediaMTX
starts FFmpeg in its own container. FFmpeg reads `contrib/live/<source UUID>` and
publishes the preview back over local RTSP. MediaMTX stops it 10 seconds after
the last reader leaves. New sources need no configuration or restart:

```yaml
preview:
  enabled: true
```

FFmpeg connects over loopback without a credential. Django's callback accepts
an RTSP request with no password from `127.0.0.1` or `::1`, but only to read an
enabled source's original or publish that source's preview. Only a process in
the MediaMTX pod can connect from loopback, because outside clients arrive
through the LoadBalancer. Enabling previews swaps the MediaMTX image for its
`-ffmpeg` build.

The converter scales video to 360 lines of VP8 at 1 Mbit/s (`preview.videoHeight`,
`preview.videoBitrate`) in libvpx's realtime mode, with no lookahead and a
keyframe every second so a new viewer gets a picture quickly. Audio becomes
48kHz stereo Opus in restricted low-delay mode with 10ms frames, and packets are
flushed promptly. The preview shows whether the feed arrives and how it sounds;
it is not a check of the contribution's picture quality, which the encode
replaces. Decoding a 1080p contribution and encoding the preview takes about
one core per watched source, so enabling previews also swaps `resources` for
`preview.resources` (a 4-core limit). Both decoder and encoder use two threads,
and the preview is capped at `preview.videoFps` (25). FFmpeg's warnings go to the
MediaMTX log, and MediaMTX path metrics show whether a preview is receiving data.

## Pulled sources

A live source can be pulled from an address (SRT caller, RTMP(S), RTSP(S),
or HLS over HTTPS) instead of pushed to. With `pull.enabled`, the
`contrib/live/<UUID>` path gets a `runOnDemand` pull relay
(`templates/pull-configmap.yaml`): when someone reads the path and nobody
publishes, MediaMTX runs it, and it asks Django
(`/api/mediamtx/pull/<UUID>`, with the relay credential in
`pull.credentialSecret`) for the source's address.

- A pulled source: FFmpeg reads the address and publishes it back over
  loopback RTSP, video copied and audio re-encoded to AAC 48 kHz stereo.
  Django lets loopback publish a pulled source's contribution path only.
- A pushed source: Django answers 204 and the relay idles until MediaMTX
  stops it. Readers waiting for an encoder now wait up to
  `pull.startTimeout` instead of being refused at once.
- MediaMTX stops the relay `pull.closeAfter` after the last reader leaves,
  and restarts it if the remote end drops.

The address is a secret, so the relay fetches it on each start and scrubs it
from FFmpeg's log lines. For HLS, give the media playlist of the variant to
pull; with a master playlist FFmpeg takes the first variant's video.

`networkPolicy.enabled` keeps MediaMTX from opening connections to private
addresses (the LAN, pods, services) other than Django and DNS, since the relay
fetches addresses members type in. Django also refuses non-public addresses
when they are saved, but only the policy covers a name that later resolves
somewhere else.

## Optional Prometheus Operator support

No monitoring resources are enabled by default. These switches are independent:

```yaml
monitoring:
  enabled: true                 # internal metrics endpoint and ClusterIP Service
  serviceMonitor:
    enabled: false              # no Operator or monitoring.coreos.com CRDs needed
```

Set `monitoring.serviceMonitor.enabled: true` only where Prometheus Operator is
installed. It supports `interval`, `scrapeTimeout` and `additionalLabels`, such
as `release: kube-prometheus-stack`. Enabling a ServiceMonitor while metrics are
disabled is rejected. Metrics port 9998 is not on the public LoadBalancer or
Ingress. Only the internal metrics action is excluded from Django HTTP auth;
media publish/read checks remain enforced.

## Signal reports for Django

```yaml
api:
  enabled: true
```

turns on MediaMTX's control API on port 9997, behind the ClusterIP Service
`mediamtx-api`. Django reads it to tell members what signal a source is
receiving: point Django's `FK_MEDIAMTX_API_URL` at
`http://mediamtx-api.<namespace>.svc.cluster.local:9997`. The port is not on
the public LoadBalancer or Ingress, and the API is not excluded from Django's
HTTP auth: Django accepts only a short-lived token it signs for itself, so no
other client in the cluster can use the API. It is off by default.

## Deployment and validation

The infra app definition provides callback address, environment hostname, LAN
address, source worker declarations and the cluster's Operator capability flag.
An initial staging trial may track this chart's feature branch; after merge,
reapply infra's app definitions to restore main tracking. Production requires a
released Django image containing the callback and converter role first.

```sh
helm lint helm/mediamtx \
  --set authURL=http://django-api.default.svc.cluster.local:8080/api/mediamtx/auth \
  --set service.loadBalancerIP=192.168.3.93
python helm/mediamtx/tests/check_render.py
```

CI exercises monitoring-off, metrics-without-Operator, optional ServiceMonitor,
standard WebRTC Ingress, private HTTP/metrics listeners and preview-worker
rendering. Missing callback/IP/candidate/worker-secret settings fail closed.

The initial public staging SRT/RTMP/RTSP test matrix passed with H.264/AAC media:
SRT to SRT/RTMP/RTSP, and RTMP/RTSP back to SRT. The infrastructure PR records the
staging rollout and all later WebRTC/conversion verification results. No source
is placed on air by this chart.

Recording is disabled. MediaMTX forbids always-available fallback on regex paths;
explicit per-source paths and capture/finalization remain controller work.
