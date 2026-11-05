# nginx in front of Cygnus Loom

The compose stack deliberately contains no TLS proxy. It publishes only:

- `127.0.0.1:21872` — Cygnus Loom
- `127.0.0.1:21873` — Authentik

Your existing nginx terminates TLS and forwards to those loopback ports. Do not expose
21872 or 21873 publicly. To use different host ports, set `CYGNUSLOOM_APP_PORT` and
`AUTHENTIK_PORT` in `.env` and update the two `proxy_pass` lines.

If this host previously ran the bundled Caddy proxy, run `./scripts/update.sh` once. It uses
`docker compose up --remove-orphans`, which removes the old Caddy container and frees ports 80
and 443 for nginx.

## Setup

1. Point both public hostnames at this host in DNS, for example
   `cygnusloom.example.com` and `idp.cygnusloom.example.com`.
2. Copy `cygnusloom.conf` to `/etc/nginx/conf.d/` and replace the two hostnames plus the
   certificate paths.
3. Obtain certificates if you do not already have them. The sample uses a webroot at
   `/var/www/html`:

   ```sh
   sudo certbot certonly --webroot -w /var/www/html \
     -d cygnusloom.example.com -d idp.cygnusloom.example.com
   ```

   If you use separate certificates per hostname, change the `ssl_certificate` lines in
   each server block.
4. Test and reload nginx:

   ```sh
   sudo nginx -t
   sudo systemctl reload nginx
   ```

5. Check the app through nginx:

   ```sh
   curl -fsS https://cygnusloom.example.com/healthz
   ```

Then open `https://<app-host>/login`. Authentik is at `https://<idp-host>/`.

Both hostnames are required: the app redirects OIDC sign-in to Authentik, and Authentik
redirects back to the app. Forwarding only the app hostname will leave sign-in broken.

## nginx running in a container

If nginx is itself a container, attach it to the compose network and proxy directly to
`app:8000` and `authentik-server:9000` instead of `127.0.0.1`. In that case the published
loopback ports are not needed, but leaving them in place is harmless.

## Streaming

The sample config turns off proxy buffering and gzip for the app. Without that, streamed GM
turns arrive in bursts and long turns can time out. Keep those settings when adding your
own TLS options.
