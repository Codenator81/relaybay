# Phone and LAN access

relaybay binds to `127.0.0.1` by default, so only this machine can reach it. To open the board on a
phone in the same network:

1. Start the server on a LAN address and allow the name you will use:

    ```toml
    # config.toml in the data folder
    host = "0.0.0.0"
    allowed_hosts = ["my-laptop.lan"]
    ```

    `relaybay serve` warns that the board is reachable from other machines.

2. Open `http://my-laptop.lan:8765/login?token=…` on the phone once (the token from
   `relaybay token`); the phone keeps the cookie.

relaybay has no built-in TLS: on a LAN the token and the cookie travel unencrypted. Use it only on a
network you trust. For access from outside the home network use a private overlay network or a
reverse proxy with TLS in front of a loopback-bound relaybay; never expose the port to the internet.
See the [security model](../SPEC.md#spec-security).
