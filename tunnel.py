"""
    tunnel.pyjam.as
    ~~~~~~~~~~~~~~~

    tunnel.pyjam.as provides an SSL-terminated, ephemeral HTTP tunnel to a
    machine on your local network without any custom software on your machine
    thanks to Wireguard.
"""


# TODO: can we use IPv6?
from ipaddress import IPv4Address, IPv4Network
import os
import random
import string
import subprocess
import textwrap

from flask import Flask
import requests


SLUG_ALPHABET: str = string.ascii_lowercase + string.digits

HOSTNAME = os.getenv("TUNNEL_HOSTNAME", "tunnel.pyjam.as")
CADDY_HOSTNAME = os.getenv("TUNNEL_CADDY_HOSTNAME", "localhost")
WG_NAME = HOSTNAME
WG_NETWORK = IPv4Network(os.getenv("TUNNEL_WG_NETWORK", "10.101.10.1/24"))
WG_PORT = int(os.getenv("TUNNEL_WG_PORT", "54321"))


def _gen_private_wg_key() -> str:
    """Generate a Wireguard private key."""
    p = subprocess.run(["wg", "genkey"], stdout=subprocess.PIPE)
    if p.returncode != 0:
        raise ChildProcessError("Failed to generate wireguard key")
    return p.stdout.decode().strip()


class Client:
    def __init__(self, vpn_ip: IPv4Address, port: int, slug: str):
        self.ip = vpn_ip
        self.port = port
        self.slug = slug
        self.private_key = _gen_private_wg_key()

    @property
    def public_key(self) -> str:
        p = subprocess.Popen(
            ["wg", "genkey"], stdout=subprocess.PIPE, stdin=subprocess.PIPE
        )
        out, _ = p.communicate(input=self.private_key.encode())
        return out.decode().strip()

    def config(
        self, server_hostname: str, server_ip: IPv4Address, server_wg_port: int
    ) -> str:
        return textwrap.dedent(
            f"""
            [Interface]
            Address = {self.ip}/32
            PrivateKey = {self.private_key}

            [Peer]
            PublicKey = {self.public_key}
            AllowedIPs = {server_ip}/32
            Endpoint = {server_hostname}:{server_wg_port}
            PersistentKeepalive = 21
        """
        )


class WireguardServerInterface:
    def __init__(self, name: str, network: IPv4Network, port: int):
        self.name = name
        self.network = network
        self.port = port

        self.peers: list[Client] = []
        self.private_key = _gen_private_wg_key()
        self._file_handle = open(self.full_path, "w+")
        self.hosts = network.hosts()
        self.ip = self.next_ip()

        self.write()
        self._up()

    @property
    def full_path(self) -> str:
        return f"/etc/wireguard/{self.name}.conf"

    def next_ip(self) -> IPv4Address:
        """Return the next ip in the Wireguard network."""
        return next(self.hosts)

    def add_peer(self, peer: Client) -> None:
        self.peers.append(peer)

    def _up(self) -> None:
        """Start the servers Wireguard interface."""
        p = subprocess.run(["wg-quick", "up", self.name])
        if p.returncode != 0:
            raise ChildProcessError("Failed to `up` interface")

    def _generate_config(self) -> str:
        # TODO: add peers here
        return textwrap.dedent(
            f"""
            [Interface]
            Address = {self.network.with_prefixlen}
            SaveConfig = true
            ListenPort = {self.port}
            PrivateKey = {self.private_key}
            PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -t nat -A POSTROUTING -o ens12 -j MASQUERADE
            PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -t nat -D POSTROUTING -o ens12 -j MASQUERADE
        """
        )

    def write(self) -> None:
        """Write Wireguard server configuration to disk."""
        self._file_handle.seek(0)
        self._file_handle.write(self._generate_config())
        self._file_handle.flush()

    def reload_interface(self) -> None:
        p = subprocess.run(
            f"wg addconf {self.name} <(wg-quick strip {self.name})", shell=True
        )
        if p.returncode != 0:
            raise ChildProcessError("Failed to reload interface")

    def __enter__(self) -> None:
        pass

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        self.write()
        self.reload_interface()


def update_reverse_proxy(
    server_hostname: str, caddy_hostname: str, client: Client
) -> None:
    """Update Caddy to act as a reverse proxy for *client*."""
    payload = {
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": f"{client.ip}:{client.port}"}],
                            }
                        ]
                    }
                ],
            }
        ],
        "match": [{"host": [f"{client.slug}.{server_hostname}"]}],
        "terminal": True,
    }
    r = requests.post(
        f"http://{caddy_hostname}:2019/config/apps/http/servers/srv0/routes/",
        json=payload,
    )
    r.raise_for_status()


def make_slug(length: int = 8) -> str:
    """Generate a slug usable as a subdomain and interface name."""
    return "".join(random.choices(SLUG_ALPHABET, k=length))


wg = WireguardServerInterface(WG_NAME, WG_NETWORK, WG_PORT)
app = Flask(__name__)


@app.route("/<int:port>")
def new_tunnel(port: int) -> str:
    """Create a new tunnel.

    *port* is the port that the client wants requests forwarded to.
    """
    slug = make_slug()

    client = Client(wg.next_ip(), port, slug)

    with wg:
        wg.add_peer(client)

    update_reverse_proxy(HOSTNAME, CADDY_HOSTNAME, client)

    return client.config(HOSTNAME, wg.ip, wg.port)
