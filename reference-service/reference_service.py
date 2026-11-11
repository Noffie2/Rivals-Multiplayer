"""Host Rivals sessions over Hamachi using the pinned reference service."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit


# Win32 car-pack tags from the patched PCDLCEntitlements asset. The last two
# are the Loaded Garage vehicle licenses from DayOnePDLC.
STEAM_CAR_PACKS = (
    "NFS14_DLC_JAGCOP", "NFS14_DLC_JAGRACER", "NFS14_DLC_FCOP", "NFS14_DLC_FRACER",
    "NFS14_DLC_LAMBOCOPS", "NFS14_DLC_LAMBORACERS", "NFS14_DLC_MOVIECOPS", "NFS14_DLC_MOVIERACERS",
    "NFS14_DLC_JAGUAR", "NFS14_DLC_FERRARI", "NFS14_DLC_LAMBOPACK", "NFS14_DLC_MOVIEPACK",
    "NFS14_DLC_KOENIGSEGG", "NFS14_PROMO_COP", "NFS14_PROMO_RACER",
)


def read_launcher_profile(server, head, body):
    try:
        method, target, _ = head.split(b"\r\n", 1)[0].split(b" ")
        url = urlsplit(target.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if url.path != "/rivals/profile" or method != b"POST":
        return None
    options = parse_qs(url.query, max_num_fields=2)
    name = server._clean_name(options.get("name", [""])[0])
    if body is None or len(body) > server.GAME_PICTURE_MAX or (
            body and (not body.startswith(server.JPEG_MAGIC) or not body.endswith(b"\xff\xd9"))):
        raise ValueError("Invalid profile picture")
    return name, body, options.get("edition", [""])[0] == "steam-complete"


def steam_entitlements(server, frame, args, session):
    request = server._req_fields(frame)
    if "NFS14PC" not in (request.get("GNLS") or []):
        return None
    if request.get("BUID"):
        server._note_persona_id(session, int(request["BUID"]))
    blaze = server.blaze
    entries = [dict(id=1000, group="NFS14PC", type=blaze.ENTITLEMENT_TYPE["ONLINE_ACCESS"],
                    tag="ONLINE_ACCESS", persona_id=session.persona_id,
                    product_id="Origin.OFR.50.0000676", grant_date="2013-11-19T00:00Z")]
    entries.extend(dict(id=index, group="NFS14PC", type=blaze.ENTITLEMENT_TYPE["DEFAULT"],
                        tag=tag, persona_id=session.persona_id, grant_date="2013-11-19T00:00Z")
                   for index, tag in enumerate(STEAM_CAR_PACKS, 2000))
    return blaze.build_list_entitlements_response(frame.seq, entries, msg_type=args.reply_msgtype)


class HostControls:
    """Local session files shared with the launcher that owns this service."""

    def __init__(self, reports, server, admission_lock):
        self.reports = reports
        self.server = server
        self.admission_lock = admission_lock
        self.locked = False
        self.published = None

    def read_lock(self):
        # Call under admission_lock so an applied change and a login have a single order.
        try:
            with (self.reports / "host-admission.txt").open("rb") as stream:
                command = stream.read(16)
        except OSError:
            return self.locked
        if command in (b"open\n", b"locked\n"):
            self.locked = command == b"locked\n"
        return self.locked

    def publish(self, lobby):
        with self.admission_lock:
            admission = "locked" if self.read_lock() else "open"
            with lobby.lock:
                active = {s.ip: s for s in lobby.sessions.values() if s.alive}
                host = active.get("127.0.0.1") or active.get("127.0.0.2")
                guest = active.get("127.0.0.3")
                phase = "waiting"
                if guest:
                    phase = "signed-in"
                    for game_id in guest.games:
                        game = lobby.games.get(game_id)
                        if not host or not game or game.host_uid != host.uid or guest.uid not in game.players:
                            continue
                        connected = self.server.blaze.PLAYER_STATE["ACTIVE_CONNECTED"]
                        phase = "connected" if game.players[guest.uid]["state"] == connected else "joining"
                        break
                host_name = self.server._clean_name(host.persona) if host else ""
                guest_name = self.server._clean_name(guest.persona) if guest else ""
        snapshot = f"{admission}\n{phase}\n{host_name}\n{guest_name}\n"
        if snapshot != self.published:
            pending = self.reports / "host-lobby.pending"
            try:
                pending.write_bytes(snapshot.encode("ascii"))
                pending.replace(self.reports / "host-lobby.txt")
                self.published = snapshot
            except OSError:
                pass

    def watch(self, lobby):
        while True:
            self.publish(lobby)
            time.sleep(0.2)


def peer_machine_id(address):
    if not isinstance(address, tuple) or len(address) != 3 or address[1] != 2:
        return None
    member = address[2]
    if not isinstance(member, tuple) or len(member) != 3 or not isinstance(member[2], list):
        return None
    fields = {tag.strip(): value for tag, _, value in member[2]}
    machine_id = fields.get("MACI")
    return machine_id if type(machine_id) is int else None


def run(reports, hamachi_host):
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or later is required")
    service_dir = Path(__file__).resolve().parent
    sys.path[:0] = [str(service_dir), str(service_dir / "turborivals_reference")]
    import make_stub_cert
    import tls_terminator as server
    from pair_join import PairJoin

    main_thread = threading.current_thread()
    dispatch = server._dispatch_blaze
    after_reply = server._after_reply
    accept = server._accept_forever
    bind = server._bind_exclusive
    create_game = server.lobby.Lobby.create_game
    join_game = server.lobby.Lobby.join
    game_setup = server._game_setup
    roles = {"127.0.0.1": "host", "127.0.0.3": "guest"}
    peer = threading.local()
    handle = server.handle
    host_login_lock = threading.Lock()
    host_controls = HostControls(reports, server, host_login_lock)
    active_sessions = {}
    launcher_profiles = {}
    profiles_lock = threading.Lock()

    def track_connection(connection, address, *args):
        peer.address = address
        if not address[0].startswith("25."):
            connection.close()
            return
        # Separate the reference service's session identities from the Hamachi transport.
        session_ip = "127.0.0.1" if address[0] == hamachi_host else "127.0.0.3"
        handle(connection, (session_ip, address[1]), *args)

    server.handle = track_connection

    def local_endpoint(session, machine_id):
        address = int(ipaddress.IPv4Address(session.hamachi_address))
        return (address, 3659, address, 3659, machine_id)

    def pair_endpoint(session):
        if session.ip in roles:
            session.addr = local_endpoint(session, session.addr[4])

    class PairSession(server.lobby.Session):
        reported_endpoint = None

        def __init__(self, ip, send_raw):
            super().__init__(ip, send_raw)
            self.hamachi_address = peer.address[0]
            with profiles_lock:
                self.launcher_profile = launcher_profiles.get(self.hamachi_address, ("", b"", False))

        @property
        def addr(self):
            return self.reported_endpoint or self._addr

        @addr.setter
        def addr(self, value):
            self._addr = value

    server.lobby.Session = PairSession

    def session_role(session):
        return roles.get(session.ip, "unknown") if session is not None else "unknown"

    pair = PairJoin(server, session_role)

    default_login_data = server.blaze.f_extended_data("DATA").encode()

    def pair_login_notices(request, args, session):
        login = (request.component, request.command) == (1, 152)
        resumed = (request.component, request.command) == (0x7802, 0x23) and session.resumed
        notes = after_reply(request, args, session)
        if not (login or resumed) or session_role(session) == "unknown":
            return notes

        pair_endpoint(session)
        role_data = server.blaze.f_extended_data("DATA", ip=session.hamachi_address, port=session.addr[1]).encode()
        if role_data == default_login_data:
            return notes
        for index, (target, packet) in enumerate(notes):
            if target is not session:
                continue
            frame = server.blaze.Fire2.decode(packet)
            # These pinned builders put default DATA first in both rich self
            # notices. Keep the remaining fields, message type and order.
            if (frame.component == args.notify_comp and frame.command in (2, 1) and
                    frame.payload.startswith(default_login_data)):
                frame.payload = role_data + frame.payload[len(default_login_data):]
                notes[index] = (target, frame.encode())
        return notes

    def create_pair_game(lobby, session, params, state):
        pair_endpoint(session)
        return create_game(lobby, session, params, state)

    def join_pair_game(lobby, session, game):
        pair_endpoint(session)
        join_game(lobby, session, game)

    def prepare_game_setup(lobby, game, context):
        with lobby.lock:
            return game_setup(lobby, game, context)

    server._game_setup = prepare_game_setup
    server.lobby.Lobby.create_game = create_pair_game
    server.lobby.Lobby.join = join_pair_game

    def dispatch_session(frame, args, session):
        if frame.msg_type == 0 and session.ip in roles:
            tag = {(0x7802, 20): "ADDR", (4, 13): "PNET"}.get((frame.component, frame.command))
            if tag:
                address = server._req_fields(frame).get(tag)
                machine_id = peer_machine_id(address)
                if machine_id:
                    # Later address updates may supply zero or omit the machine ID.
                    session.reported_endpoint = local_endpoint(session, machine_id)

        if (frame.msg_type == 0 and (frame.component, frame.command) == (1, 29) and
                session.uid and session.launcher_profile[2] and
                (reply := steam_entitlements(server, frame, args, session)) is not None):
            return reply
        if frame.msg_type == 0 and (frame.component, frame.command) == (9, 7):
            return server.blaze.build_empty_reply(9, 7, frame.seq, msg_type=args.reply_msgtype)
        if (frame.component, frame.command) in ((1, 152), (0x7802, 0x23)) and session.ip in roles:
            role = session_role(session)
            with host_login_lock:
                active = active_sessions.get(role)
                if role == "guest" and active is not session and host_controls.read_lock():
                    raise ConnectionAbortedError("The Host locked admission to this lobby")
                if active is not None and active is not session and active.alive:
                    raise ConnectionAbortedError("This role already has a live session")
                lobby = server._lobby(args)
                with lobby.lock:
                    name = session.launcher_profile[0]
                    if name:
                        lobby.names[session.ip] = name
                    else:
                        lobby.names.pop(session.ip, None)
                reply = dispatch(frame, args, session)
                if session.uid:
                    active_sessions[role] = session
                return reply

        options = pair.matchmaking_options(args, session) if frame.msg_type == 0 and (
            frame.component, frame.command) == (4, 13) else args
        reply = dispatch(frame, options, session)
        if frame.msg_type == 0 and (frame.component, frame.command) == (0x7802, 20):
            pair_endpoint(session)
        return reply

    server._dispatch_blaze = dispatch_session
    server._after_reply = pair_login_notices
    server._deliver = pair.deliver
    server._arm_matchmaking = pair.arm
    server._cancel_matchmaking = pair.cancel
    server.ea_identity.token_suffix = lambda _token: None
    server.lobby.Lobby._save_players = lambda _lobby: None
    def launcher_request(head, body, args, peer_address):
        if not peer_address[0].startswith("25."):
            return None
        try:
            profile = read_launcher_profile(server, head, body)
        except ValueError:
            return b"400 Bad Request", b"text/plain", b"Invalid profile"
        if profile is None:
            return None
        if peer_address[0] != hamachi_host:
            with host_login_lock:
                if host_controls.read_lock():
                    return b"423 Locked", b"text/plain", b"The Host locked the lobby"
        with profiles_lock:
            if peer_address[0] not in launcher_profiles and len(launcher_profiles) >= 64:
                return b"503 Service Unavailable", b"text/plain", b"Profile capacity reached"
            launcher_profiles[peer_address[0]] = profile
        return b"200 OK", b"text/plain", b"ok"

    def player_picture(method, path, body, args, headers):
        if method != "GET":
            return None
        match = server.BYTEVAULT_RECORD_RE.match(path)
        if not match or match[2] != "Pictures" or not match[3].isdigit() or len(match[3]) > 20:
            return None
        lobby = server._lobby(args)
        with lobby.lock:
            session = lobby.sessions.get(int(match[3]))
            if session and session.alive and session.launcher_profile[1]:
                return "200 OK", "image/jpeg", session.launcher_profile[1]
        return None

    server._launcher_request = launcher_request
    server._bytevault_record = player_picture

    def bind_listener(listener, ip, port, purpose):
        try:
            bind(listener, ip, port, purpose)
        except SystemExit:
            (reports / "service-error.json").write_text(json.dumps({
                "errorType": "BindFailed", "port": port, "listener": ip,
            }) + "\n", encoding="ascii")
            raise

    server._bind_exclusive = bind_listener

    with tempfile.TemporaryDirectory(prefix="reference-identity-", dir=reports.parent) as identity:
        paths = make_stub_cert.generate(Path(identity), bits=2048, days=1)
        fingerprint = hashlib.sha256(paths["der"].read_bytes()).hexdigest()

        def publish_ready(listener, on_connection, name):
            if threading.current_thread() is main_thread:
                # main() has loaded the key and successfully bound all listeners.
                for path in paths.values():
                    path.unlink()
                lobby = server._LOBBY["lobby"]
                host_controls.publish(lobby)
                threading.Thread(target=host_controls.watch, args=(lobby,), daemon=True).start()
                pending = reports / "service-ready.pending"
                pending.write_text(fingerprint, encoding="ascii")
                pending.replace(reports / "service-ready.txt")
            accept(listener, on_connection, name)

        server._accept_forever = publish_ready
        sys.argv = ["reference_service", "--bind-ip", hamachi_host, "--public-ip", hamachi_host,
                    "--redirect-ip", hamachi_host, "--port", "42127", "--blaze-port", "14219",
                    "--qos-port", "17502", "--handshake-timeout", "5", "--no-capture", "--no-store",
                    "--entitlements", "online", "--local-id", "1000000000001", "--local-id-chosen",
                    "--local-persona", "Rivals Host",
                    "--data-dir", str(reports.parent / "service-data"),
                    "--player", "127.0.0.2=Rivals Host", "--player", "127.0.0.3=Rivals Guest",
                    "--out", str(reports.parent / "disabled-captures"),
                    "--cert", str(paths["der"]), "--key", str(paths["key"])]
        return server.main()


if __name__ == "__main__":
    report_directory = Path(sys.argv[1]).resolve(strict=True)
    with open(os.devnull, "w", encoding="utf-8") as sink:
        sys.stdout = sys.stderr = sink
        try:
            host = str(ipaddress.IPv4Address(sys.argv[2]))
            if not host.startswith("25."):
                raise ValueError("Expected a Hamachi IPv4")
            raise SystemExit(run(report_directory, host))
        except Exception as error:
            (report_directory / "service-error.json").write_text(json.dumps({
                "errorType": type(error).__name__, "windowsError": getattr(error, "winerror", None),
            }) + "\n", encoding="ascii")
            raise SystemExit(1)
