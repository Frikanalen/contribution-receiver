# contribution-receiver

Receives Frikanalen's live contributions: the encoders at member
organisations push to it over SRT, RTMP or RTSP, and playout reads the
contribution back to put it on air. It is [MediaMTX](https://github.com/bluenviron/mediamtx),
configured by the Helm chart in [`chart/`](chart/README.md), with every
connection authorised by Django.

The chart was moved here from Frikanalen/gitops (`helm/mediamtx`), with its
history in that repository. The Kubernetes resources it creates keep the name
`mediamtx`, so switching an environment to this repository does not create a
second media service.

Where and at which version it runs is decided in Frikanalen/clusters
(`clusters/<env>/apps/mediamtx.yaml`); listener addresses and firewall
forwarding are in Frikanalen/infra.
