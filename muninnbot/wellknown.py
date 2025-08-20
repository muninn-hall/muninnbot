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
from aiohttp import ClientSession
from attr import dataclass
import attr

from mautrix.types import ExtensibleEnum, SerializableAttrs, UserID


class SupportRole(ExtensibleEnum):
    ADMIN = "m.role.admin"
    SECURITY = "m.role.security"


@dataclass
class SupportContact(SerializableAttrs):
    role: SupportRole
    email_address: str = ""
    matrix_id: UserID = ""


@dataclass
class SupportWellKnown(SerializableAttrs):
    contacts: list[SupportContact] = attr.ib(factory=list)
    support_page: str = ""

    def has_contact(self, user_id: UserID) -> bool:
        """
        Check if the support well-known record contains a contact for the given user ID.

        Args:
            user_id: The user ID to check for.

        Returns:
            True if a contact exists for the user ID, False otherwise.
        """
        return any(contact.matrix_id == user_id for contact in self.contacts)


async def fetch_support_well_known(sess: ClientSession, server_name: str) -> SupportWellKnown:
    """
    Fetch the support well-known record for a given server name.

    Args:
        sess: The aiohttp ClientSession to use for the request.
        server_name: The server name to fetch the support well-known record for.

    Returns:
        A SupportWellKnown object containing the support contacts and support page.
    """
    url = f"https://{server_name}/.well-known/matrix/support"
    async with sess.get(url) as resp:
        resp.raise_for_status()
        return SupportWellKnown.deserialize(await resp.json(content_type=None))
