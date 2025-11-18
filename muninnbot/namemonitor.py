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
from typing import TYPE_CHECKING
import html
import re

from maubot import MessageEvent
from maubot.handlers import command, event
from mautrix.types import EventType, MatrixURI, Membership, StateEvent, UserID
from mautrix.util import background_task

if TYPE_CHECKING:
    from .bot import MuninnBot


bracket_regex = re.compile(r"\[(.+?)]")
separator_regex = re.compile(r"[, /]")


class NameMonitor:
    bot: "MuninnBot"
    tlds: set[str]
    member_names: dict[UserID, str]
    mxid_to_servers: dict[UserID, set[str]]
    server_to_mxids: dict[str, set[UserID]]
    excluded_members: set[UserID]

    def __init__(self, bot: "MuninnBot") -> None:
        self.bot = bot
        self.tlds = set()
        self.member_names = {}
        self.mxid_to_servers = {}
        self.server_to_mxids = {}

    def read_config(self) -> None:
        self.excluded_members = set(self.bot.config["excluded_members"])

    async def start(self) -> None:
        self.tlds = {
            tld.decode("utf-8").lower()
            for tld in (await self.bot.loader.read_file("tlds-alpha-by-domain.txt")).split(b"\n")
            if tld and not tld.startswith(b"#")
        }
        background_task.create(self.load_members())

    async def load_members(self) -> None:
        members = await self.bot.client.get_joined_members(self.bot.config["main_room"])
        for user_id, member in members.items():
            if user_id in self.excluded_members:
                continue
            self.member_names[user_id] = member.displayname or user_id
            self._update_member(user_id, self.parse_name(member.displayname))

    @command.new("member-directory")
    async def get_member_directory(self, evt: MessageEvent) -> None:
        await evt.reply(
            "<details><summary>Member Directory</summary><ul>"
            + "".join(
                f"<li><a href='{MatrixURI.build(user_id).matrix_to_url}'>{self.member_names[user_id]}</a>: "
                + (
                    f"<code>{"</code>, <code>".join(servers)}</code>"
                    if servers
                    else "<em>none found</em>"
                )
                + "</li>"
                for user_id, servers in self.mxid_to_servers.items()
            )
            + "</ul></details>",
            extra_content={
                "body": "Member Directory - plaintext body not available",
                "m.mentions": {},
                "com.muninn-hall.member_directory": {
                    server: list(user_ids) for server, user_ids in self.server_to_mxids.items()
                },
            },
            allow_html=True,
            markdown=False,
        )

    @command.new("ping-users-without-server-in-name")
    async def ping_users_without_server_in_name(self, evt: MessageEvent) -> None:
        user_ids = []
        htmls = []
        for user_id, servers in self.mxid_to_servers.items():
            if servers:
                continue
            user_ids.append(user_id)
            htmls.append(
                f'<a href="{MatrixURI.build(user_id).matrix_to_url}">'
                f"{html.escape(self.member_names[user_id])}"
                "</a>"
            )
        if not user_ids:
            await evt.react("✅️")
            return
        alerts_link = MatrixURI.build(
            self.bot.config["alerts_room"],
            via=self.bot.config["room_via"],
        ).matrix_to_url
        await evt.reply(
            self.bot.config["messages.name_not_set"].format(
                mentions_html=", ".join(htmls), alerts_link=alerts_link
            ),
            extra_content={"m.mentions": {"user_ids": user_ids}},
            allow_html=True,
            markdown=False,
        )

    @event.on(EventType.ROOM_MEMBER)
    async def handle_member(self, evt: StateEvent) -> None:
        if evt.room_id != self.bot.config["main_room"]:
            return
        user_id = UserID(evt.state_key)
        if user_id in self.excluded_members:
            return
        if evt.content.membership != Membership.JOIN:
            self._remove_member(user_id)
            return
        self.member_names[user_id] = evt.content.displayname or user_id
        self._update_member(user_id, self.parse_name(evt.content.displayname))

    def parse_name(self, name: str) -> set[str]:
        output = set()
        for chunk in bracket_regex.finditer(name or ""):
            for word in separator_regex.split(chunk.group(1)):
                if "." not in word:
                    continue
                word = word.lower()
                domain, tld = word.rsplit(".", 1)
                if not domain or tld not in self.tlds:
                    continue
                output.add(word)
        return output

    def _update_member(self, user_id: UserID, new_servers: set[str]) -> None:
        old_servers = self.mxid_to_servers.get(user_id, set())
        self.mxid_to_servers[user_id] = new_servers
        if new_servers == old_servers:
            return
        for server in old_servers - new_servers:
            self._remove_member_from_server(server, user_id)
        for server in new_servers - old_servers:
            self._add_member_to_server(server, user_id)

    def _remove_member(self, user_id: UserID) -> None:
        servers = self.mxid_to_servers.pop(user_id, set())
        for server in servers:
            self._remove_member_from_server(server, user_id)

    def _remove_member_from_server(self, server: str, user_id: UserID) -> None:
        server_mxids = self.server_to_mxids.get(server)
        if server_mxids:
            server_mxids.remove(user_id)
            if not server_mxids:
                del self.server_to_mxids[server]

    def _add_member_to_server(self, server: str, user_id: UserID) -> None:
        self.server_to_mxids.setdefault(server, set()).add(user_id)
