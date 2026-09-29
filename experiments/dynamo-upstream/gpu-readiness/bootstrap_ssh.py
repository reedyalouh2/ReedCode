"""Temporary authenticated SSH access to the disposable study container."""

import asyncio
import os
from pathlib import Path

import asyncssh


class Server(asyncssh.SSHServer):
    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return dest_host in {"localhost", "127.0.0.1"} and dest_port in {8000, 8081, 8082, 9099}


async def execute(process):
    if not process.command:
        process.exit(1)
        return
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    child = await asyncio.create_subprocess_shell(
        process.command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    stdout, stderr = await child.communicate()
    process.stdout.write(stdout)
    process.stderr.write(stderr)
    process.exit(child.returncode)


async def main():
    root = Path("/tmp/reedcode-study-control")
    root.mkdir(mode=0o700, exist_ok=True)
    host = root / "ssh-host-key"
    if not host.exists():
        host.write_bytes(asyncssh.generate_private_key("ssh-ed25519").export_private_key())
        host.chmod(0o600)
    authorized = root / "authorized_keys"
    authorized.write_text(os.environ["PUBLIC_KEY"])
    authorized.chmod(0o600)
    await asyncssh.listen("", 22, server_factory=Server, server_host_keys=[str(host)],
                          authorized_client_keys=str(authorized), process_factory=execute,
                          sftp_factory=asyncssh.SFTPServer, encoding=None)
    print("SSH_READY", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
