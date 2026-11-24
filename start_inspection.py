import json
import webbrowser
from argparse import ArgumentParser
from threading import Thread
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import uvicorn


def open_site(url: str):
    try:
        if webbrowser.open(url, new=2):
            return
    except (webbrowser.Error, OSError):
        pass
    print(f"Open this address in your browser: {url}", flush=True)


def workshop_routes(port):
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as response:
            schema = json.load(response)
    except (URLError, OSError, ValueError):
        return set()
    if schema.get("info", {}).get("title") != "Vehicle Inspection API":
        return set()
    return set(schema.get("paths", {}))


def current_workshop_build(port):
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            health = json.load(response)
    except (URLError, OSError, ValueError):
        return False
    return health.get("supervisor_overview") == "reporting-handoffs-v4"


def choose_workshop_port(port):
    while port <= 65535:
        routes = workshop_routes(port)
        if not routes:
            return port, bool(routes)
        required = {"/workshop/availability", "/recall-campaigns", "/regional-recall-notices",
                    "/workshop/technician-shift", "/service-advisor/follow-ups",
                    "/repair-cases/{repair_case_id}/test-data",
                    "/repair-cases/{repair_case_id}/approvals/{approval_id}/follow-ups",
                    "/repair-cases/{repair_case_id}/concern-links",
                    "/repair-cases/{repair_case_id}/assignment-acknowledgements",
                    "/repair-cases/{repair_case_id}/attachments",
                    "/repair-case-attachments/{attachment_id}/file"}
        if required <= routes and current_workshop_build(port):
            return port, True
        port += 1
    raise RuntimeError("No port available for the updated workshop app.")


class InspectionServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            Thread(target=open_site, args=(f"http://127.0.0.1:{self.config.port}/?choose-role=1",), daemon=True).start()


def main():
    parser = ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "0.0.0.0"),
        default="127.0.0.1",
        help="Bind locally, or to all network interfaces for a private remote test.",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    port, running = choose_workshop_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    if running:
        open_site(url + "?choose-role=1")
        return

    print(f"Vehicle Inspection URL: {url}", flush=True)
    print("Keep this window open while using the app. Press Ctrl+C to stop.", flush=True)
    if args.host == "0.0.0.0":
        print(
            "Network mode is on. Keep this QA database in use; share only over a private VPN. "
            "For Tailscale, get your address with: tailscale ip -4",
            flush=True,
        )
    config = uvicorn.Config("inspection_api:app", host=args.host, port=port)
    server = InspectionServer(config)
    server.run()
    if not server.started:
        parser.exit(1, "Vehicle Inspection could not start. Check the error above.\n")


if __name__ == "__main__":
    main()
