"""Coordinate the two owned clients' join without changing the reference server."""

from copy import copy
import threading


class PairJoin:
    def __init__(self, server, role):
        self.server = server
        self.blaze = server.blaze
        self.role = role
        self.deliver_original = server._deliver
        self.arm_original = server._arm_matchmaking
        self.cancel_original = server._cancel_matchmaking
        self.lobby = None
        self.lock = threading.RLock()

    def failure(self, guest, match_id, result):
        return [(guest, self.blaze.build_notify_matchmaking_failed(
            match_id, guest.uid, self.blaze.MATCHMAKING_RESULT[result]))]

    def validate(self, guest, host, game_id, player, host_notes, setup):
        users = [self.server._req_fields(frame) for _, _, frame in host_notes
                 if (frame.component, frame.command) == (0x7802, 2)]
        if len(users) != 1 or setup is None:
            return False
        fields = users[0]
        user = {tag.strip(): value for tag, _, value in fields.get("USER", [])}
        data = {tag.strip(): value for tag, _, value in fields.get("DATA", [])}
        with self.lobby.lock:
            game = self.lobby.games.get(game_id)
            if not game or not guest.alive or not host.alive or game.host_uid != host.uid:
                return False
            member = game.players.get(guest.uid)
            return bool(member and host.uid in game.players and
                        self.lobby.sessions.get(guest.uid) is guest and
                        self.lobby.sessions.get(host.uid) is host and
                        player.get("GID") == game_id and
                        player.get("PID") == player.get("CONG") == player.get("UID") ==
                        user.get("ID") == guest.uid and
                        player.get("CSID") == player.get("SID") == member["slot"] and
                        player.get("STAT") == member["state"] == self.blaze.PLAYER_STATE["ACTIVE_CONNECTING"] and
                        self.server._union_ip_pair(player.get("PNET")) ==
                        self.server._union_ip_pair(data.get("ADDR")) == guest.addr)

    def deliver(self, guest, notes):
        if self.role(guest) != "guest" or not notes:
            return self.deliver_original(guest, notes)
        decoded = [(target, packet, self.blaze.Fire2.decode(packet)) for target, packet in notes]
        joining = [(target, frame) for target, _, frame in decoded if self.role(target) == "host" and
                   (frame.component, frame.command) == (4, 21)]
        if len(joining) != 1:
            return self.deliver_original(guest, notes)

        host, joining_frame = joining[0]
        join = self.server._req_fields(joining_frame)
        game_id = join.get("GID", 0)
        player = {tag.strip(): value for tag, _, value in join.get("PDAT", [])}
        host_notes = [(target, packet, frame) for target, packet, frame in decoded if target is host]
        setup = next((frame for target, _, frame in decoded if target is guest and
                      (frame.component, frame.command) == (4, 20)), None)
        match_id = self.server._raw_int(setup.payload, "MSID", 0) if setup is not None else 0
        host_prepared = False
        host_join_notified = False
        if self.lobby is not None and self.validate(guest, host, game_id, player, host_notes, setup) and match_id:
            try:
                for target, packet, frame in host_notes:
                    target.notify(packet)
                    if frame is joining_frame:
                        host_join_notified = True
                host_prepared = True
            except OSError:
                pass
        if host_prepared:
            return self.deliver_original(guest, [(target, packet) for target, packet in notes if target is not host])

        rollback = []
        if self.lobby is not None:
            with self.lobby.lock:
                game = self.lobby.games.get(game_id)
                if game and guest.uid in game.players and self.lobby.sessions.get(guest.uid) is guest:
                    rollback = self.lobby.remove_player(game, guest.uid, self.blaze.PLAYER_REMOVED_REASON["PLAYER_JOIN_TIMEOUT"])
        if not host_join_notified:
            rollback = []
        if match_id:
            rollback += self.failure(guest, match_id, "SESSION_ERROR_GAME_SETUP_FAILED")
        return self.deliver_original(guest, rollback)

    def pending_game(self, lobby, guest):
        with lobby.lock:
            for game_id in guest.games:
                game = lobby.games.get(game_id)
                if game is None or game.host_uid == guest.uid or self.role(lobby.sessions.get(game.host_uid)) != "host":
                    continue
                player = game.players.get(guest.uid)
                if player and player["state"] == self.blaze.PLAYER_STATE["ACTIVE_CONNECTING"]:
                    return game_id
        return 0

    def matchmaking_options(self, args, session):
        self.lobby = self.server._lobby(args)
        self.cancel(session)
        if self.role(session) != "guest":
            return args
        game_id = self.pending_game(self.lobby, session)
        if not game_id:
            return args
        options = copy(args)
        options.mm_delay = 250
        options.strict_avoid = True
        options.pair_previous_game = game_id
        return options

    def cancel(self, session):
        with self.lock:
            self.cancel_original(session)

    def arm(self, lobby, args, guest, match_id, request, delay_ms):
        if not getattr(args, "pair_previous_game", 0):
            return self.arm_original(lobby, args, guest, match_id, request, delay_ms)

        def decide():
            with self.lock:
                if guest.mm_timer is not timer or not guest.alive:
                    return
                guest.mm_timer = None
            if self.pending_game(lobby, guest):
                self.deliver_original(guest, self.failure(guest, match_id, "SESSION_TIMED_OUT"))
            else:
                self.deliver(guest, self.server._resolve_matchmaking(lobby, args, guest, match_id, request))

        with self.lock:
            self.cancel(guest)
            timer = threading.Timer(delay_ms / 1000.0, decide)
            timer.daemon = True
            guest.mm_timer = timer
            timer.start()
