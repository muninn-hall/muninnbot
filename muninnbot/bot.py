# muninnbot - A welcome bot for Muninn Hall
# Copyright (C) 2025 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from enum import Enum
import asyncio
import html
import time

from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from maubot.matrix import parse_formatted
from mautrix.client import InternalEventType, MembershipEventDispatcher, SyncStream
from mautrix.types import (
    EventID,
    EventType,
    Format,
    MatrixURI,
    Member,
    Membership,
    MessageType,
    ReactionEvent,
    RelationType,
    StateEvent,
    TextMessageEventContent,
    UserID,
)
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .namemonitor import NameMonitor
from .wellknown import fetch_support_well_known


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("screening_room")
        helper.copy("space_room")
        helper.copy("main_room")
        helper.copy("alerts_room")
        helper.copy("room_via")
        helper.copy("application_pings")
        helper.copy("excluded_members")

        helper.copy("messages.prefix")
        helper.copy("messages.recheck_prefix")
        helper.copy("messages.suffix")
        helper.copy("messages.new_is_listed_support")
        helper.copy("messages.new_not_listed_support")
        helper.copy("messages.new_well_known_missing")
        helper.copy("messages.member_is_listed_support")
        helper.copy("messages.member_not_listed_support")
        helper.copy("messages.name_not_set")


class JoinType(Enum):
    NEW_IS_LISTED_SUPPORT = "new_is_listed_support"
    NEW_NOT_LISTED_SUPPORT = "new_not_listed_support"
    NEW_WELL_KNOWN_MISSING = "new_well_known_missing"
    MEMBER_IS_LISTED_SUPPORT = "member_is_listed_support"
    MEMBER_NOT_LISTED_SUPPORT = "member_not_listed_support"


VERIFIED_APPLICATION_SENDER_KEY = "com.muninn-hall.verified_application_sender"


class MuninnBot(Plugin):
    space_members: dict[UserID, Member]
    pending_applications: dict[EventID, UserID | None]
    welcomed_users: dict[UserID, EventID | None]
    welcomed_servers: set[str]
    join_lock: asyncio.Lock
    join_limiter_count: int
    join_limiter_ts: float
    name_monitor: NameMonitor

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        self.name_monitor = NameMonitor(self)
        self.on_external_config_update()
        await self.name_monitor.start()
        self.register_handler_class(self.name_monitor)
        self.client.add_dispatcher(MembershipEventDispatcher)
        self.pending_applications = {}
        self.welcomed_users = {}
        self.space_members = {}
        self.join_limiter_count = 0
        self.join_limiter_ts = 0
        self.join_lock = asyncio.Lock()
        self.space_members = await self.client.get_joined_members(self.config["space_room"])

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        self.name_monitor.read_config()

    @event.on(InternalEventType.JOIN)
    async def handle_member(self, evt: StateEvent) -> None:
        if not evt.source & SyncStream.TIMELINE:
            return
        if evt.room_id == self.config["screening_room"]:
            if (
                evt.sender in self.space_members
                or evt.sender == self.client.mxid
                or evt.sender in self.welcomed_users
            ):
                return
            async with self.join_lock:
                if self.join_limiter_ts + 60 < time.monotonic():
                    self.join_limiter_count = 0
                elif self.join_limiter_count > 5:
                    # If more than 5 members join in a minute, stop sending welcome messages
                    self.log.warning("Not checking joined member due to rate limiting")
                    return
                await self._check_member(evt, is_join=True)
                self.join_limiter_count += 1
                self.join_limiter_ts = time.monotonic()
        elif evt.room_id == self.config["space_room"]:
            if evt.content.membership == Membership.JOIN:
                self.space_members[evt.sender] = Member(
                    membership=evt.content.membership,
                    avatar_url=evt.content.avatar_url,
                    displayname=evt.content.displayname,
                )
            elif evt.content.membership in (Membership.LEAVE, Membership.BAN):
                self.space_members.pop(evt.sender, None)

    @event.on(InternalEventType.LEAVE)
    @event.on(InternalEventType.BAN)
    async def handle_leave(self, evt: StateEvent) -> None:
        if evt.room_id == self.config["screening_room"]:
            async with self.join_lock:
                if evt.sender not in self.welcomed_users:
                    return
                evt_id = self.welcomed_users[evt.sender]
                self.welcomed_users[evt.sender] = None
            if evt_id:
                await self.client.redact(evt.room_id, evt_id, reason="User left")

    @command.new("recheck")
    async def recheck_member(self, evt: MessageEvent) -> None:
        await self._check_member(evt, is_join=False)

    @command.new("apply")
    @command.argument("message", pass_raw=True, required=False)
    async def manual_application(self, evt: MessageEvent, message: str = "") -> None:
        await evt.reply(await self._make_application_content(evt.sender))

    async def _make_application_content(self, user_id: UserID) -> TextMessageEventContent:
        content = TextMessageEventContent(msgtype=MessageType.TEXT, format=Format.HTML)
        content.body, content.formatted_body = await parse_formatted(
            f'<a href="{MatrixURI.build(user_id).matrix_to_url}">{user_id}</a> '
            "Application received, please wait for manual review. "
            "Feel free to send messages with additional details if necessary.",
            allow_html=True,
            render_markdown=False,
        )
        content["m.mentions"] = {"user_ids": [user_id, *self.config["application_pings"]]}
        return content

    @event.on(EventType.REACTION)
    async def automatic_application(self, evt: ReactionEvent) -> None:
        if (
            evt.sender == self.client.mxid
            or evt.content.relates_to.rel_type != RelationType.ANNOTATION
            or not evt.content.relates_to.key.startswith("\U0001f44d")
        ):
            return
        reaction_target = evt.content.relates_to.event_id
        try:
            user_id = self.pending_applications[reaction_target]
        except KeyError:
            try:
                evt = await self.client.get_event(evt.room_id, reaction_target)
                user_id = (
                    evt.content.get(VERIFIED_APPLICATION_SENDER_KEY)
                    if evt.sender == self.client.mxid
                    else None
                )
                self.pending_applications[reaction_target] = user_id
            except Exception:
                self.log.exception(
                    f"Failed to get event {reaction_target} to check reaction target"
                )
                self.pending_applications[reaction_target] = None
                return
        if user_id == evt.sender:
            await self.client.send_message(
                evt.room_id, await self._make_application_content(evt.sender)
            )
            self.pending_applications[reaction_target] = None

    async def _check_member(self, evt: StateEvent | MessageEvent, is_join: bool) -> None:
        _, server_name = self.client.parse_user_id(evt.sender)
        try:
            wk = await fetch_support_well_known(self.http, server_name)
            if wk.has_contact(evt.sender):
                join_type = JoinType.NEW_IS_LISTED_SUPPORT
            else:
                join_type = JoinType.NEW_NOT_LISTED_SUPPORT
        except Exception:
            self.log.warning(
                f"Failed to fetch support .well-known for {server_name}",
                exc_info=True,
            )
            join_type = JoinType.NEW_WELL_KNOWN_MISSING
        # TODO check if the server is already a member
        user_mention = (
            f'<a href="{MatrixURI.build(evt.sender).matrix_to_url}">{html.escape(evt.sender)}</a>'
        )
        server_name_html = html.escape(server_name)
        if is_join:
            prefix = self.config["messages.prefix"].format(user=user_mention)
        else:
            prefix = self.config["messages.recheck_prefix"].format(user=user_mention)
        message = self.config[f"messages.{join_type.value}"].format(
            user=user_mention, server=server_name_html
        )
        suffix = self.config["messages.suffix"]
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, format=Format.HTML)
        content["m.mentions"] = {"user_ids": [evt.sender]}
        if join_type == JoinType.NEW_IS_LISTED_SUPPORT:
            content[VERIFIED_APPLICATION_SENDER_KEY] = evt.sender
        content.body, content.formatted_body = await parse_formatted(
            f"<p>{prefix}</p><p>{message}</p><p>{suffix}</p>",
            allow_html=True,
            render_markdown=False,
        )
        content.set_reply(evt.event_id)
        evt_id = await self.client.send_message(evt.room_id, content)
        self.welcomed_users.setdefault(evt.sender, evt_id)
        if join_type == JoinType.NEW_IS_LISTED_SUPPORT:
            self.pending_applications[evt_id] = evt.sender
