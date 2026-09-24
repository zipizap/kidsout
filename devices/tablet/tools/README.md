# koTabletctl

A small `kubectl`-style Go CLI for [koTabletDaemon](../koTabletDaemon): checks whether the
tablet's screen is on/off, and remotely locks it, over the daemon's self-signed HTTPS API.

## Build

**Docker (no local Go toolchain needed):**

```sh
docker/build.sh                # writes ./koTabletctl
```

**Local Go 1.25+:**

```sh
go build -o koTabletctl .
go test ./...                  # TLS pinning, unreachable classification, config precedence
```

### Version

```sh
./koTabletctl version          # or: ./koTabletctl --version
# koTabletctl 0.1.0 (7d0f910)
```

The version lives in `internal/version/version.go` and tracks koTabletDaemon's
`versionName` (`app/build.gradle.kts`); bump both together. Builds can stamp their own
value with `-ldflags "-X kotabletctl/internal/version.Version=<v>"` — `docker/build.sh`
does this from `git describe --tags` when the checkout has a tag (or from
`$KOTABLETCTL_VERSION`), and a local `go build` inside the git checkout appends the short
commit hash automatically. Requests to the
daemon carry a `User-Agent: koTabletctl/<version>` header.

## Configure

Config is loaded kubeconfig-style: a yaml file, overridable per-field by environment
variables. Generate a template:

```sh
./koTabletctl config init            # writes ~/.kotabletctl/config.yaml
```

Fill in `server`, `username`, and `password` from koTabletDaemon's setup screen. Leave
`certFingerprint` blank — see "Trusting the daemon's certificate" below.

```yaml
server: "https://192.168.1.50:8443"
username: "yourusername"
password: "yourpassword"
certFingerprint: ""
```

Or skip the file entirely and use environment variables:

| Variable | Purpose |
|---|---|
| `KOTABLETCTL_SERVER` | Daemon base URL, e.g. `https://192.168.1.50:8443` |
| `KOTABLETCTL_USERNAME` | Basic auth username |
| `KOTABLETCTL_PASSWORD` | Basic auth password |
| `KOTABLETCTL_CERT_FINGERPRINT` | SHA-256 fingerprint of the daemon's TLS cert (optional — see below) |
| `KOTABLETCTL_INSECURE_SKIP_VERIFY` | `true` to skip cert pinning (debugging only) |
| `KOTABLETCTL_CONFIG` | Path to config file, instead of `--config` |

Env vars take precedence over the yaml file, field by field.

`./koTabletctl config view` prints the effective config (password redacted) so you can
confirm what's actually being used.

## Use

Every subcommand accepts `--config /path/to/config.yaml` to use a config file other than
the default.


### koTabletctl status
```sh
./koTabletctl status
```

Detections:  
- `screen` is  on/off, meaning "screen is lit or off"
- `keyguard` is locked/unlocked, meaning "tablet not-in-use or tablet in-use by someone"
- **Screen on + keyguard locked  => tablet lit but still locked** (e.g. glanced at the clock).
- **Screen on + keyguard unlocked** => tablet lit and unlocked **=> tablet is being used by someone (e.g. playing game or video)**
- `daemon started` is when the daemon process last started. If it moves without you
  rebooting the tablet, HyperOS killed and restarted the service — that's the signal to
  re-check the Xiaomi battery settings in the daemon README.


Outputs and meanings:
- `screen: OFF (tablet unreachable, assumed asleep: connect: no route to host)`  
  => **Tablet poweroff or unreacheable** 

- ```
  screen: OFF (since 2026-09-24T14:59:38.764999Z) 
  keyguard: locked                           
  daemon started: 2026-09-24T14:50:32.679018Z
  ```  
  => **tablet screen off and locked**


- ```
  screen: OFF (since 2026-09-24T14:59:38.764999Z) 
  keyguard: locked                           
  daemon started: 2026-09-24T14:50:32.679018Z
  ```  
  => **tablet screen lit but locked (e.g. clock)**

- ```
  screen: ON (since 2026-09-24T15:12:17.275942Z)
  keyguard: unlocked
  daemon started: 2026-09-24T14:50:32.679018Z

  ```  
  => **tablet in-use by someone (e.g. playing game or video)**


Quick-bash snippet ;) :
```bash
tablet_is_being_used() { ./koTabletctl status | grep -q 'keyguard: unlocked' ; }

if tablet_is_being_used
then
  echo "TABLET IN USE"
else 
  echo "TABLET NOT IN USE"
fi
```


### koTabletctl lock

```
./koTabletctl lock
# lock requested
```

### Others
```
./koTabletctl healthz
# ok
```


### When the tablet is unreachable: status, lock, healthz

The tablet is allowed to drop its Wi-Fi while the screen is off (the daemon deliberately
holds no Wi-Fi lock, to save battery). So `status` treats a *connection-level* failure —
connection refused, no route, dial timeout, DNS failure — as the screen being off:

```
$ koTabletctl status
screen: OFF (tablet unreachable, assumed asleep: i/o timeout)
```

Exit code is 0 in that case. This is a heuristic: "unreachable" also covers a daemon that
HyperOS killed, a tablet that's powered off, or a broken network. Once the screen is back
on, `daemon started` tells the first two apart from a normal sleep.

Only `status` does this. `lock` and `healthz` report an unreachable tablet as an error
(exit 1), because a lock that didn't happen must not look like success — and you can only
lock a tablet whose screen is on anyway, which is exactly when it is reachable. Errors that
prove the daemon *did* answer (certificate fingerprint mismatch, `401`, `409`, `5xx`) are
always errors, for every command. The connect timeout is 5 seconds so the "asleep" answer
arrives quickly.

## Trusting the daemon's certificate

The daemon's certificate is self-signed (there's no CA involved for a single personal
device), so normal TLS chain validation can't succeed. Rather than disabling certificate
checking outright (`KOTABLETCTL_INSECURE_SKIP_VERIFY=true`, which accepts *any* server),
`koTabletctl` pins the exact SHA-256 fingerprint of the daemon's certificate and rejects
any connection presenting a different one.

You don't need to copy that fingerprint by hand. The first time you run any command
against a server with no `certFingerprint` configured yet, `koTabletctl` connects, shows
you the fingerprint it saw, and asks you to confirm it matches the one on koTabletDaemon's
setup screen (both sides show it as colon-separated lowercase hex):

```
$ koTabletctl status
koTabletDaemon at https://192.168.1.50:8443 presented a certificate with SHA-256 fingerprint:

  09:dc:18:c9:29:cf:21:57:47:8a:db:ee:c7:89:50:ad:ec:24:35:51:ba:fc:89:c8:57:48:7c:2c:6f:83:d4:d9

Compare this to the fingerprint shown on koTabletDaemon's setup screen on the tablet.
Trust and save it? [y/N]: y
Saved to /home/you/.kotabletctl/config.yaml
screen: ON (since 2026-09-17T08:12:03Z)
```

Answering `y` writes the confirmed fingerprint into the config file so every later run
pins against it silently — no further prompts, and no manual copy/paste. (Saving rewrites
the file from its parsed fields, so any comments you added to it are dropped.) Answering
anything else aborts without saving, and the run fails (this is the same trust-on-first-use
model SSH uses for host keys). The trust prompt itself never falls back to "assumed asleep":
if the tablet can't be reached during first-time setup you get an error, not `OFF`.

If you ever see this prompt again for a server you already trusted, treat it the same way
you'd treat SSH's "host key changed" warning: something about the daemon changed, don't
blindly accept without checking why (e.g. did you uninstall and reinstall the app, which
regenerates its key?).
